"""Persistence model.

Twenty-one tables in four groups:

* **reference**   -- users, instruments, instrument_mappings, hedge_configs
* **trading**     -- orders, fills, positions, hedge_cycles, hedge_cycle_events
* **market**      -- market_data, funding, fees, fx_rates
* **observability** -- risk_events, margin_snapshots, pnl_records,
  system_events, audit_logs, emergency_actions, configuration_changes,
  scenario_runs

The observability group is append-only.  Nothing updates or deletes from it,
which is what makes "why did the system do that?" answerable after the fact.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, Money, UTCDateTime, utcnow


# ======================================================================
# reference data
# ======================================================================
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    #: VIEWER | TRADER | ADMIN -- see ``api/security.py``.
    role: Mapped[str] = mapped_column(String(32), default="VIEWER", nullable=False)
    #: Hash only.  No plaintext secret is ever stored, and the platform has no
    #: live venue credentials to store in the first place.
    api_key_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Instrument(Base):
    """Persisted contract specification.

    Mirrors ``domain.instrument.InstrumentSpec`` field for field so a spec can
    round-trip between YAML, the database and the API without translation loss.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("venue", "symbol", name="uq_instrument_venue_symbol"),
        Index("ix_instrument_underlying", "underlying_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")

    base_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    settlement_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying_key: Mapped[str] = mapped_column(String(32), default="")

    quantity_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    settlement_style: Mapped[str] = mapped_column(String(16), default="LINEAR", nullable=False)
    contract_size: Mapped[Decimal] = mapped_column(Money, nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(Money, nullable=False)
    units_per_contract: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    units_per_lot: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    tick_size: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tick_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    min_quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    quantity_step: Mapped[Decimal] = mapped_column(Money, nullable=False)
    price_precision: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    quantity_precision: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    max_leverage: Mapped[Decimal] = mapped_column(Money, nullable=False)
    margin_model: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_margin_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    maintenance_margin_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    maker_fee_bps: Mapped[Decimal] = mapped_column(Money, nullable=False)
    taker_fee_bps: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fee_currency: Mapped[str] = mapped_column(String(32), default="")
    typical_spread_bps: Mapped[Decimal] = mapped_column(Money, nullable=False)
    slippage_bps_per_unit_liquidity: Mapped[Decimal] = mapped_column(Money, nullable=False)

    funding_model: Mapped[str] = mapped_column(String(32), nullable=False)
    funding_interval_hours: Mapped[Decimal] = mapped_column(Money, nullable=False)
    baseline_funding_rate: Mapped[Decimal] = mapped_column(Money, nullable=False)
    swap_long_points: Mapped[Decimal] = mapped_column(Money, nullable=False)
    swap_short_points: Mapped[Decimal] = mapped_column(Money, nullable=False)
    swap_triple_weekday: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    trading_hours: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    allow_long: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_short: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    price_source: Mapped[str] = mapped_column(String(64), default="simulator")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"


class InstrumentMapping(Base):
    """A source instrument and the instrument that hedges it."""

    __tablename__ = "instrument_mappings"
    __table_args__ = (
        UniqueConstraint("source_instrument_id", "hedge_instrument_id", name="uq_mapping_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    source_instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    hedge_instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    source_instrument: Mapped[Instrument] = relationship(foreign_keys=[source_instrument_id])
    hedge_instrument: Mapped[Instrument] = relationship(foreign_keys=[hedge_instrument_id])
    configs: Mapped[list[HedgeConfig]] = relationship(
        back_populates="mapping", cascade="all, delete-orphan"
    )


class HedgeConfig(Base):
    """Objective, tolerances and optimiser settings for one mapping."""

    __tablename__ = "hedge_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("instrument_mappings.id", ondelete="CASCADE"), nullable=False
    )
    objective: Mapped[str] = mapped_column(String(40), nullable=False)
    target_ratio: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tolerance_bps: Mapped[Decimal] = mapped_column(Money, nullable=False)

    rebalance_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rebalance_interval_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    rebalance_min_notional: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    optimizer_mode: Mapped[str] = mapped_column(String(24), default="EXACT", nullable=False)
    optimizer_priority: Mapped[str] = mapped_column(String(32), default="MIN_RESIDUAL")
    optimizer_search_steps: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    optimizer_weights: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    max_notional: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_position: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    risk_params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    mapping: Mapped[InstrumentMapping] = relationship(back_populates="configs")


# ======================================================================
# trading
# ======================================================================
class HedgeCycleRow(Base):
    __tablename__ = "hedge_cycles"
    __table_args__ = (Index("ix_cycle_state_created", "state", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    pair_name: Mapped[str] = mapped_column(String(128), default="")
    mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("instrument_mappings.id", ondelete="SET NULL"), nullable=True
    )
    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("hedge_configs.id", ondelete="SET NULL"), nullable=True
    )
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    hedge_key: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    objective: Mapped[str] = mapped_column(String(40), default="")

    source_target_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    hedge_target_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    source_filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    hedge_filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    hedge_ratio: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    residual_exposure: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))

    source_order_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    hedge_order_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    events: Mapped[list[HedgeCycleEvent]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan", order_by="HedgeCycleEvent.sequence"
    )


class HedgeCycleEvent(Base):
    """Append-only transition log.  The basis of restart recovery."""

    __tablename__ = "hedge_cycle_events"
    __table_args__ = (
        UniqueConstraint("cycle_row_id", "sequence", name="uq_cycle_event_sequence"),
        Index("ix_cycle_event_time", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_row_id: Mapped[int] = mapped_column(
        ForeignKey("hedge_cycles.id", ondelete="CASCADE"), nullable=False
    )
    cycle_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    cycle: Mapped[HedgeCycleRow] = relationship(back_populates="events")


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_order_venue_symbol", "venue", "symbol"),
        Index("ix_order_cycle", "cycle_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    #: Idempotency key.  A unique constraint here is what makes a retry after a
    #: timeout safe: the second insert fails instead of creating a duplicate.
    client_order_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    cycle_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    leg: Mapped[str | None] = mapped_column(String(16), nullable=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(8), default="IOC")
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    average_price: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    reference_price: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fees_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    fills: Mapped[list[FillRow]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Venue-assigned execution id.  Unique so a replayed execution report
    #: cannot double-count the position.
    exec_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    order_row_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    slippage: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    is_maker: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    order: Mapped[OrderRow] = relationship(back_populates="fills")


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("venue", "symbol", name="uq_position_venue_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), nullable=True
    )
    #: Signed: positive long, negative short.
    quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), nullable=False)
    average_entry: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    funding_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    funding_received: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fees_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ======================================================================
# market data
# ======================================================================
class MarketDataRow(Base):
    __tablename__ = "market_data"
    __table_args__ = (Index("ix_market_data_key_time", "venue", "symbol", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    bid: Mapped[Decimal] = mapped_column(Money, nullable=False)
    ask: Mapped[Decimal] = mapped_column(Money, nullable=False)
    mid: Mapped[Decimal] = mapped_column(Money, nullable=False)
    last: Mapped[Decimal] = mapped_column(Money, nullable=False)
    spread: Mapped[Decimal] = mapped_column(Money, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    funding_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="simulator")
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class FundingRow(Base):
    __tablename__ = "funding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="FUNDING")  # FUNDING | SWAP
    rate: Mapped[Decimal] = mapped_column(Money, nullable=False)
    interval_hours: Mapped[Decimal] = mapped_column(Money, default=Decimal(8))
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class FeeRow(Base):
    __tablename__ = "fees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    fill_exec_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # TAKER | MAKER | SPREAD | SLIPPAGE
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class FxRateRow(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (Index("ix_fx_pair_time", "base_ccy", "quote_ccy", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_ccy: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_ccy: Mapped[str] = mapped_column(String(16), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Money, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="seed")
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# ======================================================================
# observability (append-only)
# ======================================================================
class RiskEventRow(Base):
    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_event_level_time", "level", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)  # PAIR | PORTFOLIO | VENUE
    pair_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Money, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    cycle_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class MarginSnapshotRow(Base):
    __tablename__ = "margin_snapshots"
    __table_args__ = (Index("ix_margin_venue_time", "venue", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    used_margin: Mapped[Decimal] = mapped_column(Money, nullable=False)
    free_margin: Mapped[Decimal] = mapped_column(Money, nullable=False)
    margin_level: Mapped[Decimal] = mapped_column(Money, nullable=False)
    maintenance_margin: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class PnLRecordRow(Base):
    __tablename__ = "pnl_records"
    __table_args__ = (Index("ix_pnl_scope_time", "scope", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    pair_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    gross_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    trading_fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    funding_received: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    funding_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    swap_financing: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    spread_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    slippage_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    fx_impact: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    net_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class SystemEventRow(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_event_kind_time", "kind", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    component: Mapped[str] = mapped_column(String(48), default="")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class AuditLogRow(Base):
    """Who did what, with the before/after state."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity_time", "entity_type", "entity_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class EmergencyActionRow(Base):
    __tablename__ = "emergency_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(128), nullable=False)
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(128), nullable=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="risk_engine")
    correlation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class ConfigurationChangeRow(Base):
    __tablename__ = "configuration_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    entity: Mapped[str] = mapped_column(String(48), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class ScenarioRunRow(Base):
    __tablename__ = "scenario_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING")
    seed: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


ALL_TABLES = [
    User, Instrument, InstrumentMapping, HedgeConfig,
    HedgeCycleRow, HedgeCycleEvent, OrderRow, FillRow, PositionRow,
    MarketDataRow, FundingRow, FeeRow, FxRateRow,
    RiskEventRow, MarginSnapshotRow, PnLRecordRow, SystemEventRow,
    AuditLogRow, EmergencyActionRow, ConfigurationChangeRow, ScenarioRunRow,
]
