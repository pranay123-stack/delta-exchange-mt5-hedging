"""Ports the execution engine depends on.

Defined as protocols so the coordinator can be unit-tested with in-memory
implementations and run in production against PostgreSQL, without either
knowing about the other.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..domain.orders import Fill, Order
from .state_machine import CycleEvent, HedgeCycle


@runtime_checkable
class CycleRecorder(Protocol):
    """Durable record of everything a hedge cycle did."""

    async def save_cycle(self, cycle: HedgeCycle) -> None: ...

    async def record_event(self, event: CycleEvent) -> None: ...

    async def record_order(self, order: Order, cycle_id: str | None) -> None: ...

    async def record_fill(self, fill: Fill, order: Order) -> None: ...

    async def record_system_event(
        self, kind: str, severity: str, component: str, message: str, payload: dict[str, Any]
    ) -> None: ...


class NullRecorder:
    """No-op recorder.  Used by pure calculation tests and the CLI dry-run."""

    def __init__(self) -> None:
        self.cycles: list[HedgeCycle] = []
        self.events: list[CycleEvent] = []
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.system_events: list[dict[str, Any]] = []

    async def save_cycle(self, cycle: HedgeCycle) -> None:
        self.cycles.append(cycle)

    async def record_event(self, event: CycleEvent) -> None:
        self.events.append(event)

    async def record_order(self, order: Order, cycle_id: str | None) -> None:
        self.orders.append(order)

    async def record_fill(self, fill: Fill, order: Order) -> None:
        self.fills.append(fill)

    async def record_system_event(
        self, kind: str, severity: str, component: str, message: str, payload: dict[str, Any]
    ) -> None:
        self.system_events.append(
            {"kind": kind, "severity": severity, "component": component,
             "message": message, "payload": payload}
        )


@runtime_checkable
class EventPublisher(Protocol):
    """Fan-out to WebSocket subscribers."""

    async def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


class NullPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))
