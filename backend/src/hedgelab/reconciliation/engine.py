"""Three-way position reconciliation and restart recovery.

The database says one thing, the perpetual venue says another, the broker says
a third.  Any disagreement means the platform's view of its own exposure is
wrong, which is the precondition for every bad outcome in this system.

Two entry points:

* :meth:`ReconciliationEngine.reconcile` -- compare all three sources and
  classify every discrepancy.  Safe to run on a timer.
* :meth:`ReconciliationEngine.recover` -- rebuild in-memory state after a
  restart, decide whether it is safe to resume, and say what must happen first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..domain.enums import CycleState, ReconciliationIssue
from ..domain.market import utcnow
from ..domain.numeric import ZERO, normalize, quantize, safe_div
from ..domain.orders import Position
from ..instruments.registry import InstrumentRegistry
from ..logging_setup import get_logger
from ..venues.base import TradingVenue, VenueError

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One disagreement between two sources of truth."""

    issue: ReconciliationIssue
    venue: str
    symbol: str
    database_value: Decimal | None
    venue_value: Decimal | None
    difference: Decimal
    severity: str  # INFO | WARNING | CRITICAL
    message: str
    suggested_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue.value,
            "venue": self.venue,
            "symbol": self.symbol,
            "database_value": str(self.database_value) if self.database_value is not None else None,
            "venue_value": str(self.venue_value) if self.venue_value is not None else None,
            "difference": str(quantize(self.difference, 12)),
            "severity": self.severity,
            "message": self.message,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    timestamp: datetime
    checked_venues: tuple[str, ...]
    database_positions: int
    venue_positions: int
    discrepancies: tuple[Discrepancy, ...]
    unreachable_venues: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies and not self.unreachable_venues

    @property
    def has_critical(self) -> bool:
        return any(d.severity == "CRITICAL" for d in self.discrepancies)

    def by_issue(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.discrepancies:
            counts[d.issue.value] = counts.get(d.issue.value, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "checked_venues": list(self.checked_venues),
            "unreachable_venues": list(self.unreachable_venues),
            "database_positions": self.database_positions,
            "venue_positions": self.venue_positions,
            "is_clean": self.is_clean,
            "has_critical": self.has_critical,
            "issue_counts": self.by_issue(),
            "discrepancies": [d.to_dict() for d in self.discrepancies],
        }


@dataclass
class RecoveryPlan:
    """What restart recovery found and what it wants to do about it."""

    resumable: bool
    reconciliation: ReconciliationReport
    reconstructed_cycles: list[dict[str, Any]] = field(default_factory=list)
    cycles_needing_attention: list[dict[str, Any]] = field(default_factory=list)
    orphaned_orders: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resumable": self.resumable,
            "reconciliation": self.reconciliation.to_dict(),
            "reconstructed_cycles": self.reconstructed_cycles,
            "cycles_needing_attention": self.cycles_needing_attention,
            "orphaned_orders": self.orphaned_orders,
            "required_actions": self.required_actions,
            "notes": self.notes,
        }


class ReconciliationEngine:
    """Compares database state against live venue state."""

    #: Quantity differences below this are treated as clean.  Venues round;
    #: a difference smaller than the smallest tradable increment is noise.
    TOLERANCE_FACTOR = Decimal("0.5")

    def __init__(
        self,
        *,
        registry: InstrumentRegistry,
        venues: dict[str, TradingVenue],
        price_tolerance_bps: Decimal = Decimal(10),
    ) -> None:
        self.registry = registry
        self.venues = venues
        self.price_tolerance_bps = price_tolerance_bps

    # ------------------------------------------------------------------
    async def reconcile(
        self,
        database_positions: list[Position],
        database_open_orders: list[str] | None = None,
    ) -> ReconciliationReport:
        """Compare database positions with live venue positions."""
        discrepancies: list[Discrepancy] = []
        unreachable: list[str] = []
        venue_positions: dict[str, Position] = {}

        for name, venue in self.venues.items():
            try:
                for position in await venue.get_positions():
                    venue_positions[position.key] = position
            except VenueError as exc:
                unreachable.append(name)
                log.error(
                    "venue unreachable during reconciliation",
                    extra={"venue": name, "error": str(exc)},
                )
                discrepancies.append(Discrepancy(
                    issue=ReconciliationIssue.STALE_STATE,
                    venue=name, symbol="*",
                    database_value=None, venue_value=None, difference=ZERO,
                    severity="CRITICAL",
                    message=f"{name} could not be queried: {exc}",
                    suggested_action="retry once the venue reconnects; do not trade this venue",
                ))

        db_by_key = {p.key: p for p in database_positions if not p.is_flat}

        # Positions the venue has that the database does not know about.
        for key, position in venue_positions.items():
            if position.is_flat:
                continue
            if key in db_by_key:
                continue
            spec = self.registry.find(key)
            discrepancies.append(Discrepancy(
                issue=ReconciliationIssue.UNKNOWN_POSITION,
                venue=position.venue, symbol=position.symbol,
                database_value=None, venue_value=position.quantity,
                difference=position.quantity,
                severity="CRITICAL",
                message=(
                    f"{key} holds {normalize(position.quantity)} on the venue but the database "
                    f"has no record of it"
                ),
                suggested_action=(
                    "adopt the venue position into the database and recompute the hedge, "
                    "or flatten it if it was not intended"
                ),
            ))
            if spec is None:
                log.warning("unknown position in an unregistered instrument", extra={"key": key})

        # Positions the database has that the venue does not.
        for key, position in db_by_key.items():
            venue_position = venue_positions.get(key)
            if position.venue in unreachable:
                continue
            if venue_position is None or venue_position.is_flat:
                discrepancies.append(Discrepancy(
                    issue=ReconciliationIssue.MISSING_POSITION,
                    venue=position.venue, symbol=position.symbol,
                    database_value=position.quantity, venue_value=ZERO,
                    difference=-position.quantity,
                    severity="CRITICAL",
                    message=(
                        f"database records {normalize(position.quantity)} of {key} but the venue "
                        f"reports flat"
                    ),
                    suggested_action=(
                        "the position was closed outside the platform (stop-out or manual); "
                        "clear it from the database and re-evaluate the pair"
                    ),
                ))
                continue

            spec = self.registry.find(key)
            tolerance = (
                spec.quantity_step * self.TOLERANCE_FACTOR if spec else Decimal("0.0000001")
            )
            difference = venue_position.quantity - position.quantity
            if abs(difference) > tolerance:
                discrepancies.append(Discrepancy(
                    issue=ReconciliationIssue.QUANTITY_MISMATCH,
                    venue=position.venue, symbol=position.symbol,
                    database_value=position.quantity, venue_value=venue_position.quantity,
                    difference=difference,
                    severity="CRITICAL",
                    message=(
                        f"{key} quantity mismatch: database {normalize(position.quantity)}, "
                        f"venue {normalize(venue_position.quantity)} "
                        f"(difference {normalize(difference)})"
                    ),
                    suggested_action="trust the venue, update the database, then rebalance the hedge",
                ))
                continue

            if position.average_entry > ZERO and venue_position.average_entry > ZERO:
                drift_bps = abs(
                    safe_div(
                        venue_position.average_entry - position.average_entry,
                        position.average_entry,
                    )
                ) * Decimal(10000)
                if drift_bps > self.price_tolerance_bps:
                    discrepancies.append(Discrepancy(
                        issue=ReconciliationIssue.PRICE_MISMATCH,
                        venue=position.venue, symbol=position.symbol,
                        database_value=position.average_entry,
                        venue_value=venue_position.average_entry,
                        difference=venue_position.average_entry - position.average_entry,
                        severity="WARNING",
                        message=(
                            f"{key} average entry differs by {quantize(drift_bps, 2)} bps: "
                            f"database {position.average_entry}, venue {venue_position.average_entry}"
                        ),
                        suggested_action=(
                            "adopt the venue's entry price; P&L and inverse-contract delta "
                            "both depend on it"
                        ),
                    ))

        # Orders the database thinks are working but the venue has closed.
        if database_open_orders:
            live_ids: set[str] = set()
            for name, venue in self.venues.items():
                if name in unreachable:
                    continue
                try:
                    live_ids.update(o.order_id for o in await venue.get_open_orders())
                except VenueError:
                    unreachable.append(name)
            for order_id in database_open_orders:
                if order_id not in live_ids:
                    discrepancies.append(Discrepancy(
                        issue=ReconciliationIssue.ORDER_MISMATCH,
                        venue="*", symbol="*",
                        database_value=None, venue_value=None, difference=ZERO,
                        severity="WARNING",
                        message=f"order {order_id} is open in the database but not on any venue",
                        suggested_action="query the venue for its final state and close it out",
                    ))

        report = ReconciliationReport(
            timestamp=utcnow(),
            checked_venues=tuple(sorted(self.venues)),
            database_positions=len(db_by_key),
            venue_positions=len([p for p in venue_positions.values() if not p.is_flat]),
            discrepancies=tuple(discrepancies),
            unreachable_venues=tuple(sorted(set(unreachable))),
        )
        if not report.is_clean:
            log.warning(
                "reconciliation found discrepancies",
                extra={"counts": report.by_issue(), "critical": report.has_critical,
                       "unreachable": list(report.unreachable_venues)},
            )
        else:
            log.info(
                "reconciliation clean",
                extra={"positions": report.database_positions,
                       "venues": list(report.checked_venues)},
            )
        return report

    # ------------------------------------------------------------------
    async def recover(
        self,
        *,
        database_positions: list[Position],
        unfinished_cycles: list[dict[str, Any]],
        database_open_orders: list[str] | None = None,
    ) -> RecoveryPlan:
        """Rebuild state after a restart and decide whether it is safe to resume.

        The rule: **resume only if nothing is ambiguous.**  A cycle that was
        mid-flight, a position the venue disagrees about, or an unreachable
        venue all block automatic resumption, because in every one of those
        cases the platform does not know its own exposure.
        """
        report = await self.reconcile(database_positions, database_open_orders)
        plan = RecoveryPlan(resumable=True, reconciliation=report)

        for cycle in unfinished_cycles:
            state = CycleState(cycle["state"])
            entry = {
                "cycle_id": cycle.get("cycle_id"),
                "pair": cycle.get("pair_name"),
                "state": state.value,
                "source_filled": cycle.get("source_filled_quantity"),
                "hedge_filled": cycle.get("hedge_filled_quantity"),
                "has_exposure": state.has_exposure,
            }
            plan.reconstructed_cycles.append(entry)
            if state.has_exposure:
                plan.cycles_needing_attention.append(entry)
                plan.required_actions.append(
                    f"cycle {cycle.get('cycle_id')} stopped in {state.value} with exposure on "
                    f"the book: re-assess the pair and rebalance before resuming"
                )
            else:
                plan.notes.append(
                    f"cycle {cycle.get('cycle_id')} stopped in {state.value} before taking "
                    f"exposure; it can be abandoned safely"
                )

        for discrepancy in report.discrepancies:
            if discrepancy.severity == "CRITICAL":
                plan.required_actions.append(
                    f"{discrepancy.issue.value} on {discrepancy.venue}:{discrepancy.symbol} -- "
                    f"{discrepancy.suggested_action}"
                )

        if report.unreachable_venues:
            plan.required_actions.append(
                f"venues unreachable: {', '.join(report.unreachable_venues)}; "
                f"exposure there cannot be confirmed"
            )

        plan.resumable = not plan.required_actions
        if plan.resumable:
            plan.notes.append(
                "state reconstructed cleanly: database, perpetual venue and broker agree"
            )
        log.info(
            "restart recovery evaluated",
            extra={
                "resumable": plan.resumable,
                "unfinished_cycles": len(unfinished_cycles),
                "needing_attention": len(plan.cycles_needing_attention),
                "required_actions": len(plan.required_actions),
            },
        )
        return plan

    # ------------------------------------------------------------------
    async def adopt_venue_state(self) -> list[Position]:
        """Return live venue positions, to be written over the database.

        The venue is authoritative: it holds the money.  Adoption is an
        explicit, audited action, never an automatic side effect of a read.
        """
        positions: list[Position] = []
        for venue in self.venues.values():
            try:
                positions.extend(await venue.get_positions())
            except VenueError as exc:
                log.error(
                    "cannot adopt state from an unreachable venue",
                    extra={"venue": venue.name, "error": str(exc)},
                )
                raise
        return positions
