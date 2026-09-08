"""Database-backed :class:`CycleRecorder`.

Bridges the execution engine's port to the repositories.  Each call opens its
own short transaction: a hedge cycle spans network round-trips to two venues,
and holding one transaction open across those would pin a connection for the
whole cycle and lose everything already recorded if the last step failed.
"""

from __future__ import annotations

from typing import Any

from ..domain.orders import Fill, Order
from ..execution.state_machine import CycleEvent, HedgeCycle
from ..logging_setup import get_logger
from .repositories import CycleRepository, ObservabilityRepository, OrderRepository
from .session import Database

log = get_logger(__name__)


class DatabaseRecorder:
    """Durable implementation of the ``CycleRecorder`` protocol."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def save_cycle(self, cycle: HedgeCycle) -> None:
        async with self.database.session() as session:
            await CycleRepository(session).save(cycle)

    async def record_event(self, event: CycleEvent) -> None:
        async with self.database.session() as session:
            await CycleRepository(session).record_event(event)

    async def record_order(self, order: Order, cycle_id: str | None) -> None:
        async with self.database.session() as session:
            repo = OrderRepository(session)
            await repo.save(order, cycle_id)
            for fill in order.fills:
                await repo.save_fill(fill, order)

    async def record_fill(self, fill: Fill, order: Order) -> None:
        async with self.database.session() as session:
            await OrderRepository(session).save_fill(fill, order)

    async def record_system_event(
        self, kind: str, severity: str, component: str, message: str, payload: dict[str, Any]
    ) -> None:
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_system_event(
                kind, severity, component, message, payload
            )
