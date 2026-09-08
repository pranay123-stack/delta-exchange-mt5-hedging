"""Fault-injection framework.

Faults are *armed* rather than triggered: the operator arms a fault with a
scope and a count, and it fires the next time the matching code path runs.
That makes failures reproducible in tests and demonstrable from the dashboard
without racing the engine.

Every fault site in the codebase calls :meth:`FaultInjector.should_fire`; the
injector never reaches into the engine itself, so there is exactly one way for
a fault to influence behaviour and it is greppable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.enums import FaultKind
from ..domain.market import utcnow
from ..domain.numeric import ONE, ZERO, dec
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class ArmedFault:
    """One armed fault and its firing conditions."""

    kind: FaultKind
    #: Restrict to a venue / symbol / leg.  ``None`` matches anything.
    venue: str | None = None
    symbol: str | None = None
    leg: str | None = None
    #: Number of remaining firings; ``None`` means unlimited.
    remaining: int | None = 1
    #: Probability of firing when the scope matches.
    probability: Decimal = ONE
    #: Kind-specific parameter, e.g. the fraction to fill for a partial fill.
    magnitude: Decimal = Decimal("0.5")
    reason: str = "operator-injected fault"
    armed_at: datetime = field(default_factory=utcnow)
    fired_count: int = 0
    last_fired_at: datetime | None = None

    @property
    def is_exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    def matches(self, venue: str | None, symbol: str | None, leg: str | None) -> bool:
        if self.venue is not None and venue is not None and self.venue != venue:
            return False
        if self.symbol is not None and symbol is not None and self.symbol != symbol:
            return False
        return not (self.leg is not None and leg is not None and self.leg != leg)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "venue": self.venue,
            "symbol": self.symbol,
            "leg": self.leg,
            "remaining": self.remaining,
            "probability": str(self.probability),
            "magnitude": str(self.magnitude),
            "reason": self.reason,
            "armed_at": self.armed_at.isoformat(),
            "fired_count": self.fired_count,
            "last_fired_at": self.last_fired_at.isoformat() if self.last_fired_at else None,
        }


class FaultInjector:
    """Holds armed faults and decides whether one fires."""

    def __init__(self, seed: int = 7) -> None:
        self._faults: list[ArmedFault] = []
        self._rng = random.Random(seed)
        self._history: list[dict[str, object]] = []
        self.enabled = True

    # ------------------------------------------------------------------
    # arming
    # ------------------------------------------------------------------
    def arm(
        self,
        kind: FaultKind,
        *,
        venue: str | None = None,
        symbol: str | None = None,
        leg: str | None = None,
        count: int | None = 1,
        probability: Decimal | float | str = 1,
        magnitude: Decimal | float | str = "0.5",
        reason: str = "operator-injected fault",
    ) -> ArmedFault:
        fault = ArmedFault(
            kind=kind,
            venue=venue,
            symbol=symbol,
            leg=leg,
            remaining=count,
            probability=dec(probability),
            magnitude=dec(magnitude),
            reason=reason,
        )
        self._faults.append(fault)
        log.warning("fault armed", extra={"fault": fault.to_dict()})
        return fault

    def disarm(self, kind: FaultKind | None = None) -> int:
        """Disarm faults of ``kind`` (or everything).  Returns how many."""
        before = len(self._faults)
        if kind is None:
            self._faults.clear()
        else:
            self._faults = [f for f in self._faults if f.kind is not kind]
        removed = before - len(self._faults)
        if removed:
            log.info("faults disarmed", extra={"count": removed, "kind": kind.value if kind else "ALL"})
        return removed

    # ------------------------------------------------------------------
    # firing
    # ------------------------------------------------------------------
    def should_fire(
        self,
        kind: FaultKind,
        *,
        venue: str | None = None,
        symbol: str | None = None,
        leg: str | None = None,
    ) -> ArmedFault | None:
        """Return the fault that fires for this call site, or ``None``.

        Fires at most one fault per call and decrements its counter.
        """
        if not self.enabled:
            return None
        for fault in self._faults:
            if fault.kind is not kind or fault.is_exhausted:
                continue
            if not fault.matches(venue, symbol, leg):
                continue
            if fault.probability < ONE and dec(self._rng.random()) >= fault.probability:
                continue
            if fault.remaining is not None:
                fault.remaining -= 1
            fault.fired_count += 1
            fault.last_fired_at = utcnow()
            record: dict[str, object] = {
                "kind": kind.value,
                "venue": venue,
                "symbol": symbol,
                "leg": leg,
                "magnitude": str(fault.magnitude),
                "fired_at": fault.last_fired_at.isoformat(),
            }
            self._history.append(record)
            log.warning("fault fired", extra=record)
            self._prune()
            return fault
        return None

    def _prune(self) -> None:
        self._faults = [f for f in self._faults if not f.is_exhausted]

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def armed(self) -> list[ArmedFault]:
        return list(self._faults)

    def history(self, limit: int = 100) -> list[dict[str, object]]:
        return self._history[-limit:]

    def clear_history(self) -> None:
        self._history.clear()

    def magnitude_for(
        self,
        kind: FaultKind,
        default: Decimal,
        *,
        venue: str | None = None,
        symbol: str | None = None,
        leg: str | None = None,
    ) -> Decimal:
        """Fire ``kind`` and return its magnitude, or ``default`` if it did not."""
        fault = self.should_fire(kind, venue=venue, symbol=symbol, leg=leg)
        return fault.magnitude if fault is not None else default


#: A permanently disabled injector, for tests that must not see faults.
class NullFaultInjector(FaultInjector):
    def __init__(self) -> None:
        super().__init__()
        self.enabled = False

    def should_fire(
        self,
        kind: FaultKind,
        *,
        venue: str | None = None,
        symbol: str | None = None,
        leg: str | None = None,
    ) -> ArmedFault | None:
        return None


__all__ = ["ZERO", "ArmedFault", "FaultInjector", "NullFaultInjector"]
