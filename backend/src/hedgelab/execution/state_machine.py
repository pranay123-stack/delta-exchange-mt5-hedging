"""Hedge-cycle state machine.

Transitions are declared as data and validated on every move.  An illegal
transition raises and is logged; it is never silently applied.  That matters
because the state is what restart recovery reads to decide whether exposure
might be on the book, and a state that got there by an undeclared path cannot
be reasoned about.

Every transition is recorded as a :class:`CycleEvent` *before* the side effect
it describes is attempted (write-ahead).  So a crash between "submitted" and
"filled" leaves a record saying an order was submitted with unknown outcome --
which is exactly what reconciliation needs to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..domain.enums import CycleState, Leg
from ..domain.market import utcnow
from ..domain.numeric import quantize
from ..domain.orders import new_id
from ..logging_setup import get_logger

log = get_logger(__name__)


class IllegalTransition(RuntimeError):
    def __init__(self, from_state: CycleState, to_state: CycleState) -> None:
        super().__init__(
            f"illegal hedge-cycle transition {from_state.value} -> {to_state.value}"
        )
        self.from_state = from_state
        self.to_state = to_state


#: States reachable from any non-terminal state.  Failure and escalation are
#: always available -- a risk system that cannot abort is not a risk system.
UNIVERSAL_TARGETS: frozenset[CycleState] = frozenset({
    CycleState.FAILED,
    CycleState.EMERGENCY,
    CycleState.RECOVERY_REQUIRED,
})

TRANSITIONS: dict[CycleState, frozenset[CycleState]] = {
    CycleState.CREATED: frozenset({CycleState.VALIDATED}),
    CycleState.VALIDATED: frozenset({CycleState.AWAITING_MARKET_DATA, CycleState.CALCULATED}),
    CycleState.AWAITING_MARKET_DATA: frozenset({CycleState.CALCULATED}),
    CycleState.CALCULATED: frozenset({CycleState.RISK_APPROVED}),
    CycleState.RISK_APPROVED: frozenset({CycleState.LEG_1_SUBMITTED}),
    CycleState.LEG_1_SUBMITTED: frozenset({
        CycleState.LEG_1_PARTIAL, CycleState.LEG_1_FILLED,
    }),
    CycleState.LEG_1_PARTIAL: frozenset({
        CycleState.LEG_1_FILLED, CycleState.LEG_1_PARTIAL, CycleState.LEG_2_SUBMITTED,
    }),
    CycleState.LEG_1_FILLED: frozenset({CycleState.LEG_2_SUBMITTED}),
    CycleState.LEG_2_SUBMITTED: frozenset({
        CycleState.LEG_2_PARTIAL, CycleState.BOTH_FILLED,
    }),
    CycleState.LEG_2_PARTIAL: frozenset({
        CycleState.BOTH_FILLED, CycleState.LEG_2_PARTIAL, CycleState.REBALANCING,
    }),
    CycleState.BOTH_FILLED: frozenset({
        CycleState.COMPLETED, CycleState.REBALANCING, CycleState.RISK_REDUCTION,
    }),
    CycleState.REBALANCING: frozenset({
        CycleState.COMPLETED, CycleState.BOTH_FILLED, CycleState.RISK_REDUCTION,
    }),
    CycleState.RISK_REDUCTION: frozenset({CycleState.COMPLETED, CycleState.BOTH_FILLED}),
    CycleState.RECOVERY_REQUIRED: frozenset({
        CycleState.REBALANCING, CycleState.RISK_REDUCTION, CycleState.COMPLETED,
        CycleState.LEG_2_SUBMITTED,
    }),
    CycleState.EMERGENCY: frozenset({CycleState.RISK_REDUCTION, CycleState.COMPLETED}),
    CycleState.COMPLETED: frozenset(),
    CycleState.FAILED: frozenset(),
}


def allowed_targets(state: CycleState) -> frozenset[CycleState]:
    """Every state reachable from ``state``."""
    declared = TRANSITIONS.get(state, frozenset())
    if state.is_terminal:
        return declared
    return declared | UNIVERSAL_TARGETS


def can_transition(from_state: CycleState, to_state: CycleState) -> bool:
    return to_state in allowed_targets(from_state)


@dataclass(frozen=True, slots=True)
class CycleEvent:
    """One persisted transition."""

    cycle_id: str
    sequence: int
    from_state: CycleState | None
    to_state: CycleState
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utcnow)
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "sequence": self.sequence,
            "from_state": self.from_state.value if self.from_state else None,
            "to_state": self.to_state.value,
            "event": self.event,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "correlation_id": self.correlation_id,
        }


@dataclass
class HedgeCycle:
    """A supervised two-leg hedge execution."""

    cycle_id: str = field(default_factory=lambda: new_id("cyc"))
    pair_name: str = ""
    source_key: str = ""
    hedge_key: str = ""
    state: CycleState = CycleState.CREATED
    objective: str = ""
    correlation_id: str | None = None

    source_target_quantity: Decimal = Decimal(0)
    hedge_target_quantity: Decimal = Decimal(0)
    source_filled_quantity: Decimal = Decimal(0)
    hedge_filled_quantity: Decimal = Decimal(0)
    hedge_ratio: Decimal = Decimal(0)
    residual_exposure: Decimal = Decimal(0)

    source_order_id: str | None = None
    hedge_order_id: str | None = None
    error: str | None = None

    events: list[CycleEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    # ------------------------------------------------------------------
    def transition(
        self,
        to_state: CycleState,
        event: str,
        payload: dict[str, Any] | None = None,
    ) -> CycleEvent:
        """Move to ``to_state``, recording the event.

        Raises :class:`IllegalTransition` when the move is not declared -- the
        caller has a bug, and continuing would corrupt the recovery record.
        """
        if not can_transition(self.state, to_state):
            log.error(
                "illegal hedge-cycle transition rejected",
                extra={"cycle_id": self.cycle_id, "from": self.state.value,
                       "to": to_state.value, "event": event},
            )
            raise IllegalTransition(self.state, to_state)

        record = CycleEvent(
            cycle_id=self.cycle_id,
            sequence=len(self.events) + 1,
            from_state=self.state,
            to_state=to_state,
            event=event,
            payload=payload or {},
            correlation_id=self.correlation_id,
        )
        self.events.append(record)
        self.state = to_state
        self.updated_at = record.timestamp
        log.info(
            "hedge cycle transition",
            extra={"cycle_id": self.cycle_id, "from": record.from_state.value if record.from_state else None,
                   "to": to_state.value, "event": event, "sequence": record.sequence},
        )
        return record

    def fail(self, reason: str, payload: dict[str, Any] | None = None) -> CycleEvent:
        """Terminate the cycle.

        Chooses FAILED or RECOVERY_REQUIRED based on whether leg 1 may have put
        exposure on the book.  Failing a cycle that already traded would leave
        an unhedged position marked "finished".
        """
        self.error = reason
        target = CycleState.RECOVERY_REQUIRED if self.state.has_exposure else CycleState.FAILED
        detail = {"reason": reason, **(payload or {})}
        return self.transition(target, "cycle_failed", detail)

    def escalate(self, reason: str, payload: dict[str, Any] | None = None) -> CycleEvent:
        self.error = reason
        return self.transition(CycleState.EMERGENCY, "emergency", {"reason": reason, **(payload or {})})

    # ------------------------------------------------------------------
    @property
    def is_complete(self) -> bool:
        return self.state.is_terminal

    @property
    def needs_attention(self) -> bool:
        return self.state in (CycleState.RECOVERY_REQUIRED, CycleState.EMERGENCY)

    @property
    def leg_imbalance(self) -> Decimal:
        """Hedge shortfall against target.  Non-zero means the book is exposed."""
        return self.hedge_target_quantity - self.hedge_filled_quantity

    def leg_state(self, leg: Leg) -> str:
        if leg is Leg.SOURCE:
            if self.source_filled_quantity == Decimal(0):
                return "PENDING"
            if abs(self.source_filled_quantity) >= abs(self.source_target_quantity):
                return "FILLED"
            return "PARTIAL"
        if self.hedge_filled_quantity == Decimal(0):
            return "PENDING"
        if abs(self.hedge_filled_quantity) >= abs(self.hedge_target_quantity):
            return "FILLED"
        return "PARTIAL"

    def history(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "pair": self.pair_name,
            "source": self.source_key,
            "hedge": self.hedge_key,
            "state": self.state.value,
            "objective": self.objective,
            "correlation_id": self.correlation_id,
            "source_target_quantity": str(self.source_target_quantity),
            "hedge_target_quantity": str(self.hedge_target_quantity),
            "source_filled_quantity": str(self.source_filled_quantity),
            "hedge_filled_quantity": str(self.hedge_filled_quantity),
            # Quantize for transport: a ratio is a derived quantity and its
            # full Decimal expansion (28 significant digits) is noise in an API
            # response. The stored value keeps its precision.
            "hedge_ratio": str(quantize(self.hedge_ratio, 10)),
            "residual_exposure": str(quantize(self.residual_exposure, 12)),
            "source_order_id": self.source_order_id,
            "hedge_order_id": self.hedge_order_id,
            "error": self.error,
            "leg_imbalance": str(self.leg_imbalance),
            "source_leg_state": self.leg_state(Leg.SOURCE),
            "hedge_leg_state": self.leg_state(Leg.HEDGE),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "event_count": len(self.events),
        }


def reachable_states(start: CycleState = CycleState.CREATED) -> set[CycleState]:
    """Breadth-first closure of the transition graph.  Used by tests and docs."""
    seen: set[CycleState] = {start}
    frontier: list[CycleState] = [start]
    while frontier:
        current = frontier.pop()
        for target in allowed_targets(current):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def describe_graph() -> list[tuple[str, list[str]]]:
    """Transition table, for documentation generation."""
    return [
        (state.value, sorted(t.value for t in allowed_targets(state)))
        for state in CycleState
    ]


def validate_graph() -> list[str]:
    """Structural checks run by the test suite.

    Every non-terminal state must be able to reach COMPLETED or FAILED,
    otherwise a cycle could get stuck forever with exposure on the book.
    """
    problems: list[str] = []
    for state in CycleState:
        if state.is_terminal:
            continue
        reachable = reachable_states(state)
        if not (CycleState.COMPLETED in reachable or CycleState.FAILED in reachable):
            problems.append(f"{state.value} cannot reach a terminal state")
    unreachable = set(CycleState) - reachable_states(CycleState.CREATED)
    for state in sorted(unreachable, key=lambda s: s.value):
        problems.append(f"{state.value} is unreachable from CREATED")
    return problems
