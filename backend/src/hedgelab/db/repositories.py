"""Repositories.

One class per aggregate.  Every write that matters also writes an audit row,
so the audit trail cannot drift from the data it describes -- they are written
in the same transaction.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.account import AccountSnapshot
from ..domain.enums import CycleState
from ..domain.instrument import InstrumentSpec
from ..domain.market import Ticker
from ..domain.numeric import ZERO
from ..domain.orders import Fill, Order, Position
from ..execution.state_machine import CycleEvent, HedgeCycle
from ..logging_setup import get_correlation_id, get_logger
from .base import utcnow
from .models import (
    AuditLogRow,
    ConfigurationChangeRow,
    EmergencyActionRow,
    FeeRow,
    FillRow,
    FundingRow,
    FxRateRow,
    HedgeCycleEvent,
    HedgeCycleRow,
    Instrument,
    InstrumentMapping,
    MarginSnapshotRow,
    MarketDataRow,
    OrderRow,
    PnLRecordRow,
    PositionRow,
    RiskEventRow,
    ScenarioRunRow,
    SystemEventRow,
)

log = get_logger(__name__)


# ======================================================================
class InstrumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, spec: InstrumentSpec) -> Instrument:
        existing = await self.get(spec.venue, spec.symbol)
        payload = self._to_columns(spec)
        if existing is None:
            row = Instrument(**payload)
            self.session.add(row)
            await self.session.flush()
            return row
        for key, value in payload.items():
            setattr(existing, key, value)
        await self.session.flush()
        return existing

    async def get(self, venue: str, symbol: str) -> Instrument | None:
        stmt = select(Instrument).where(Instrument.venue == venue, Instrument.symbol == symbol)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, *, active_only: bool = False) -> Sequence[Instrument]:
        stmt = select(Instrument).order_by(Instrument.venue, Instrument.symbol)
        if active_only:
            stmt = stmt.where(Instrument.active.is_(True))
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count(Instrument.id)))).scalar_one())

    @staticmethod
    def _to_columns(spec: InstrumentSpec) -> dict[str, Any]:
        return {
            "venue": spec.venue, "symbol": spec.symbol,
            "venue_kind": spec.venue_kind.value,
            "instrument_type": spec.instrument_type.value,
            "display_name": spec.display_name,
            "base_asset": spec.base_asset, "quote_asset": spec.quote_asset,
            "settlement_asset": spec.settlement_asset,
            "underlying_key": spec.effective_underlying_key,
            "quantity_unit": spec.quantity_unit.value,
            "settlement_style": spec.settlement_style.value,
            "contract_size": spec.contract_size,
            "contract_multiplier": spec.contract_multiplier,
            "units_per_contract": spec.units_per_contract,
            "units_per_lot": spec.units_per_lot,
            "tick_size": spec.tick_size, "tick_value": spec.tick_value,
            "min_quantity": spec.min_quantity, "max_quantity": spec.max_quantity,
            "quantity_step": spec.quantity_step,
            "price_precision": spec.price_precision,
            "quantity_precision": spec.quantity_precision,
            "max_leverage": spec.max_leverage,
            "margin_model": spec.margin_model.value,
            "initial_margin_rate": spec.initial_margin_rate,
            "maintenance_margin_rate": spec.maintenance_margin_rate,
            "maker_fee_bps": spec.maker_fee_bps, "taker_fee_bps": spec.taker_fee_bps,
            "fee_currency": spec.fee_currency,
            "typical_spread_bps": spec.typical_spread_bps,
            "slippage_bps_per_unit_liquidity": spec.slippage_bps_per_unit_liquidity,
            "funding_model": spec.funding_model.value,
            "funding_interval_hours": spec.funding_interval_hours,
            "baseline_funding_rate": spec.baseline_funding_rate,
            "swap_long_points": spec.swap_long_points,
            "swap_short_points": spec.swap_short_points,
            "swap_triple_weekday": spec.swap_triple_weekday,
            "trading_hours": spec.trading_hours.model_dump(mode="json"),
            "allow_long": spec.allow_long, "allow_short": spec.allow_short,
            "price_source": spec.price_source, "active": spec.active,
        }


# ======================================================================
class MappingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, name: str, source: Instrument, hedge: Instrument, enabled: bool = True
    ) -> InstrumentMapping:
        stmt = select(InstrumentMapping).where(InstrumentMapping.name == name)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            row = InstrumentMapping(
                name=name, source_instrument_id=source.id,
                hedge_instrument_id=hedge.id, enabled=enabled,
            )
            self.session.add(row)
            await self.session.flush()
            return row
        existing.source_instrument_id = source.id
        existing.hedge_instrument_id = hedge.id
        existing.enabled = enabled
        await self.session.flush()
        return existing

    async def list(self) -> Sequence[InstrumentMapping]:
        return (
            await self.session.execute(select(InstrumentMapping).order_by(InstrumentMapping.name))
        ).scalars().all()


# ======================================================================
class CycleRepository:
    """Persists hedge cycles and their transition events."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, cycle: HedgeCycle) -> HedgeCycleRow:
        row = await self.get_row(cycle.cycle_id)
        if row is None:
            row = HedgeCycleRow(cycle_id=cycle.cycle_id)
            self.session.add(row)
        row.pair_name = cycle.pair_name
        row.source_key = cycle.source_key
        row.hedge_key = cycle.hedge_key
        row.state = cycle.state.value
        row.objective = cycle.objective
        row.source_target_quantity = cycle.source_target_quantity
        row.hedge_target_quantity = cycle.hedge_target_quantity
        row.source_filled_quantity = cycle.source_filled_quantity
        row.hedge_filled_quantity = cycle.hedge_filled_quantity
        row.hedge_ratio = cycle.hedge_ratio
        row.residual_exposure = cycle.residual_exposure
        row.source_order_id = cycle.source_order_id
        row.hedge_order_id = cycle.hedge_order_id
        row.correlation_id = cycle.correlation_id
        row.error = cycle.error
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def record_event(self, event: CycleEvent) -> HedgeCycleEvent | None:
        row = await self.get_row(event.cycle_id)
        if row is None:
            # The cycle is saved before its first transition; if it is missing
            # the caller has a bug, and dropping the event silently would
            # destroy the recovery record.
            log.error(
                "cannot record cycle event: parent cycle row not found",
                extra={"cycle_id": event.cycle_id, "event": event.event},
            )
            return None
        existing = await self.session.execute(
            select(HedgeCycleEvent).where(
                HedgeCycleEvent.cycle_row_id == row.id,
                HedgeCycleEvent.sequence == event.sequence,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None  # idempotent replay
        db_event = HedgeCycleEvent(
            cycle_row_id=row.id,
            cycle_id=event.cycle_id,
            sequence=event.sequence,
            from_state=event.from_state.value if event.from_state else None,
            to_state=event.to_state.value,
            event=event.event,
            payload=event.payload,
            correlation_id=event.correlation_id,
            timestamp=event.timestamp,
        )
        self.session.add(db_event)
        await self.session.flush()
        return db_event

    async def get_row(self, cycle_id: str) -> HedgeCycleRow | None:
        stmt = select(HedgeCycleRow).where(HedgeCycleRow.cycle_id == cycle_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def events(self, cycle_id: str) -> Sequence[HedgeCycleEvent]:
        stmt = (
            select(HedgeCycleEvent)
            .where(HedgeCycleEvent.cycle_id == cycle_id)
            .order_by(HedgeCycleEvent.sequence)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list(
        self, *, limit: int = 100, states: Sequence[str] | None = None
    ) -> Sequence[HedgeCycleRow]:
        stmt = select(HedgeCycleRow).order_by(desc(HedgeCycleRow.created_at)).limit(limit)
        if states:
            stmt = stmt.where(HedgeCycleRow.state.in_(states))
        return (await self.session.execute(stmt)).scalars().all()

    async def unfinished(self) -> Sequence[HedgeCycleRow]:
        """Cycles that were mid-flight when the process stopped.

        These are the ones restart recovery must reconstruct: a cycle in
        LEG_1_FILLED has exposure and no complete hedge.
        """
        terminal = [CycleState.COMPLETED.value, CycleState.FAILED.value]
        stmt = (
            select(HedgeCycleRow)
            .where(HedgeCycleRow.state.notin_(terminal))
            .order_by(HedgeCycleRow.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()


# ======================================================================
class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, order: Order, cycle_id: str | None = None) -> OrderRow:
        stmt = select(OrderRow).where(OrderRow.order_id == order.order_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        request = order.request
        if row is None:
            row = OrderRow(
                order_id=order.order_id,
                client_order_id=request.client_order_id,
                venue=request.venue,
                symbol=request.symbol,
                side=request.side.value,
                order_type=request.order_type.value,
                time_in_force=request.time_in_force.value,
                quantity=request.quantity,
                price=request.price,
                is_paper=request.is_paper,
                created_at=order.created_at,
            )
            self.session.add(row)
        row.cycle_id = cycle_id or request.cycle_id
        row.leg = request.leg.value if request.leg else None
        row.status = order.status.value
        row.filled_quantity = order.filled_quantity
        row.average_price = order.average_price
        row.reference_price = order.reference_price
        row.fees_paid = order.fees_paid
        row.reject_reason = order.reject_reason
        row.correlation_id = request.correlation_id or get_correlation_id()
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def save_fill(self, fill: Fill, order: Order) -> FillRow | None:
        stmt = select(FillRow).where(FillRow.exec_id == fill.exec_id)
        if (await self.session.execute(stmt)).scalar_one_or_none() is not None:
            return None  # duplicate execution report
        order_row = (
            await self.session.execute(select(OrderRow).where(OrderRow.order_id == order.order_id))
        ).scalar_one_or_none()
        if order_row is None:
            order_row = await self.save(order)
        row = FillRow(
            exec_id=fill.exec_id,
            order_row_id=order_row.id,
            order_id=order.order_id,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            slippage=fill.slippage,
            is_maker=fill.is_maker,
            timestamp=fill.timestamp,
        )
        self.session.add(row)
        if fill.fee != ZERO:
            self.session.add(FeeRow(
                order_id=order.order_id, fill_exec_id=fill.exec_id,
                kind="MAKER" if fill.is_maker else "TAKER", amount=fill.fee,
            ))
        await self.session.flush()
        return row

    async def list(
        self, *, limit: int = 200, venue: str | None = None, cycle_id: str | None = None
    ) -> Sequence[OrderRow]:
        stmt = select(OrderRow).order_by(desc(OrderRow.created_at)).limit(limit)
        if venue:
            stmt = stmt.where(OrderRow.venue == venue)
        if cycle_id:
            stmt = stmt.where(OrderRow.cycle_id == cycle_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def fills(self, *, limit: int = 200, order_id: str | None = None) -> Sequence[FillRow]:
        stmt = select(FillRow).order_by(desc(FillRow.timestamp)).limit(limit)
        if order_id:
            stmt = stmt.where(FillRow.order_id == order_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def open_orders(self) -> Sequence[OrderRow]:
        working = ["PENDING", "SUBMITTED", "PARTIALLY_FILLED"]
        stmt = select(OrderRow).where(OrderRow.status.in_(working))
        return (await self.session.execute(stmt)).scalars().all()


# ======================================================================
class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, position: Position) -> PositionRow:
        stmt = select(PositionRow).where(
            PositionRow.venue == position.venue, PositionRow.symbol == position.symbol
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = PositionRow(venue=position.venue, symbol=position.symbol)
            self.session.add(row)
        row.quantity = position.quantity
        row.average_entry = position.average_entry
        row.realized_pnl = position.realized_pnl
        row.funding_paid = position.funding_paid
        row.funding_received = position.funding_received
        row.fees_paid = position.fees_paid
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def list(self, *, non_flat_only: bool = False) -> Sequence[PositionRow]:
        stmt = select(PositionRow).order_by(PositionRow.venue, PositionRow.symbol)
        if non_flat_only:
            stmt = stmt.where(PositionRow.quantity != Decimal(0))
        return (await self.session.execute(stmt)).scalars().all()

    # ``list`` is a method on this class, so the builtin is shadowed in the
    # class body and must be named explicitly in the annotation.
    async def to_domain(self) -> builtins.list[Position]:
        return [
            Position(
                venue=r.venue, symbol=r.symbol, quantity=r.quantity,
                average_entry=r.average_entry, realized_pnl=r.realized_pnl,
                funding_paid=r.funding_paid, funding_received=r.funding_received,
                fees_paid=r.fees_paid, updated_at=r.updated_at,
            )
            for r in await self.list()
        ]

    async def clear(self) -> None:
        await self.session.execute(delete(PositionRow))


# ======================================================================
class ObservabilityRepository:
    """Append-only writes: risk, margin, P&L, system events, audit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_risk_event(
        self, *, level: str, scope: str, metric: str, value: Decimal,
        threshold: Decimal, message: str, pair_name: str | None = None,
        cycle_id: str | None = None,
    ) -> RiskEventRow:
        row = RiskEventRow(
            level=level, scope=scope, metric=metric, value=value, threshold=threshold,
            message=message, pair_name=pair_name, cycle_id=cycle_id,
            correlation_id=get_correlation_id(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_margin_snapshot(self, snapshot: AccountSnapshot) -> MarginSnapshotRow:
        row = MarginSnapshotRow(
            venue=snapshot.venue, currency=snapshot.currency, balance=snapshot.balance,
            equity=snapshot.equity, used_margin=snapshot.used_margin,
            free_margin=snapshot.free_margin, margin_level=snapshot.margin_level,
            maintenance_margin=snapshot.maintenance_margin,
            unrealized_pnl=snapshot.unrealized_pnl, open_positions=snapshot.open_positions,
            timestamp=snapshot.timestamp,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_pnl(
        self, scope: str, breakdown: dict[str, Any], pair_name: str | None = None
    ) -> PnLRecordRow:
        def d(key: str) -> Decimal:
            return Decimal(str(breakdown.get(key, "0")))

        row = PnLRecordRow(
            scope=scope, pair_name=pair_name, currency=str(breakdown.get("currency", "USD")),
            gross_pnl=d("gross_pnl"), trading_fees=d("trading_fees"),
            funding_received=d("funding_received"), funding_paid=d("funding_paid"),
            swap_financing=d("swap_financing"), spread_cost=d("spread_cost"),
            slippage_cost=d("slippage_cost"), fx_impact=d("fx_conversion_impact"),
            net_pnl=d("net_pnl"), realized_pnl=d("realized_pnl"),
            unrealized_pnl=d("unrealized_pnl"), breakdown=breakdown,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_system_event(
        self, kind: str, severity: str, component: str, message: str,
        payload: dict[str, Any] | None = None,
    ) -> SystemEventRow:
        row = SystemEventRow(
            kind=kind, severity=severity, component=component, message=message,
            payload=payload or {}, correlation_id=get_correlation_id(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_audit(
        self, *, actor: str, action: str, entity_type: str,
        entity_id: str | None = None, before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> AuditLogRow:
        row = AuditLogRow(
            actor=actor, action=action, entity_type=entity_type, entity_id=entity_id,
            before=before, after=after, correlation_id=get_correlation_id(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_emergency_action(
        self, *, trigger: str, level: str, action: str, scope: str | None,
        executed: bool, result: str, actor: str = "risk_engine",
    ) -> EmergencyActionRow:
        row = EmergencyActionRow(
            trigger=trigger, level=level, action=action, scope=scope,
            executed=executed, result=result, actor=actor,
            correlation_id=get_correlation_id(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_configuration_change(
        self, *, actor: str, entity: str, entity_id: str | None,
        before: dict[str, Any] | None, after: dict[str, Any] | None, note: str = "",
    ) -> ConfigurationChangeRow:
        row = ConfigurationChangeRow(
            actor=actor, entity=entity, entity_id=entity_id,
            before=before, after=after, note=note,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_ticker(self, ticker: Ticker, source: str = "simulator") -> MarketDataRow:
        row = MarketDataRow(
            venue=ticker.venue, symbol=ticker.symbol, bid=ticker.bid, ask=ticker.ask,
            mid=ticker.mid, last=ticker.last, spread=ticker.spread, volume=ticker.volume,
            funding_rate=ticker.funding_rate, is_stale=ticker.is_stale, source=source,
            timestamp=ticker.timestamp,
        )
        self.session.add(row)
        return row

    async def record_funding(
        self, *, venue: str, symbol: str, kind: str, rate: Decimal,
        amount: Decimal, currency: str, interval_hours: Decimal = Decimal(8),
    ) -> FundingRow:
        row = FundingRow(
            venue=venue, symbol=symbol, kind=kind, rate=rate, amount=amount,
            currency=currency, interval_hours=interval_hours,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_fx(self, base: str, quote: str, rate: Decimal, source: str) -> FxRateRow:
        row = FxRateRow(base_ccy=base, quote_ccy=quote, rate=rate, source=source)
        self.session.add(row)
        return row

    # --- reads ---------------------------------------------------------
    async def audit_trail(
        self, *, limit: int = 200, entity_type: str | None = None,
        correlation_id: str | None = None,
    ) -> Sequence[AuditLogRow]:
        stmt = select(AuditLogRow).order_by(desc(AuditLogRow.timestamp)).limit(limit)
        if entity_type:
            stmt = stmt.where(AuditLogRow.entity_type == entity_type)
        if correlation_id:
            stmt = stmt.where(AuditLogRow.correlation_id == correlation_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def risk_events(self, *, limit: int = 100, level: str | None = None) -> Sequence[RiskEventRow]:
        stmt = select(RiskEventRow).order_by(desc(RiskEventRow.timestamp)).limit(limit)
        if level:
            stmt = stmt.where(RiskEventRow.level == level)
        return (await self.session.execute(stmt)).scalars().all()

    async def system_events(self, *, limit: int = 100) -> Sequence[SystemEventRow]:
        stmt = select(SystemEventRow).order_by(desc(SystemEventRow.timestamp)).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def emergency_actions(self, *, limit: int = 100) -> Sequence[EmergencyActionRow]:
        stmt = select(EmergencyActionRow).order_by(desc(EmergencyActionRow.timestamp)).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def margin_snapshots(
        self, *, venue: str | None = None, limit: int = 200
    ) -> Sequence[MarginSnapshotRow]:
        stmt = select(MarginSnapshotRow).order_by(desc(MarginSnapshotRow.timestamp)).limit(limit)
        if venue:
            stmt = stmt.where(MarginSnapshotRow.venue == venue)
        return (await self.session.execute(stmt)).scalars().all()

    async def pnl_records(self, *, limit: int = 200) -> Sequence[PnLRecordRow]:
        stmt = select(PnLRecordRow).order_by(desc(PnLRecordRow.timestamp)).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def funding_history(self, *, limit: int = 200) -> Sequence[FundingRow]:
        stmt = select(FundingRow).order_by(desc(FundingRow.timestamp)).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def daily_pnl(self, since: datetime | None = None) -> Decimal:
        """Net P&L recorded since ``since`` (default: the last 24 hours)."""
        cutoff = since or (utcnow() - timedelta(days=1))
        stmt = select(func.sum(PnLRecordRow.net_pnl)).where(PnLRecordRow.timestamp >= cutoff)
        total = (await self.session.execute(stmt)).scalar_one_or_none()
        return Decimal(str(total)) if total is not None else ZERO

    async def start_scenario(self, scenario: str, seed: int) -> ScenarioRunRow:
        row = ScenarioRunRow(scenario=scenario, seed=seed, status="RUNNING")
        self.session.add(row)
        await self.session.flush()
        return row

    async def finish_scenario(
        self, row: ScenarioRunRow, status: str, summary: dict[str, Any]
    ) -> ScenarioRunRow:
        row.status = status
        row.summary = summary
        row.finished_at = utcnow()
        await self.session.flush()
        return row

    async def scenario_runs(self, limit: int = 50) -> Sequence[ScenarioRunRow]:
        stmt = select(ScenarioRunRow).order_by(desc(ScenarioRunRow.started_at)).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()
