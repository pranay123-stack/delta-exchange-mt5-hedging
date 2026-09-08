"""Enumerations shared across the platform.

Every enum is a ``str`` enum so values round-trip through JSON, YAML and the
database without custom serialisers.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum with a readable ``repr`` and ``str``."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class TradingMode(StrEnum):
    """Global execution mode.

    ``LIVE`` exists so the type system can express it; no live adapter is
    registered anywhere in this repository and the venue factory refuses to
    build one.  See ``venues/factory.py`` and ``PAPER_TRADING.md``.
    """

    PAPER = "PAPER"
    LIVE = "LIVE"


class VenueKind(StrEnum):
    """Family of venue an instrument trades on."""

    PERPETUAL_EXCHANGE = "PERPETUAL_EXCHANGE"
    MT5_BROKER = "MT5_BROKER"
    SPOT_EXCHANGE = "SPOT_EXCHANGE"


class InstrumentType(StrEnum):
    PERPETUAL = "PERPETUAL"
    FUTURE = "FUTURE"
    CFD = "CFD"
    SPOT = "SPOT"
    FX = "FX"


class QuantityUnit(StrEnum):
    """The unit a venue accepts order quantities in.

    This is the root cause of ``1 contract != 1 lot``: the same economic
    exposure is expressed in different units on each venue.
    """

    CONTRACT = "CONTRACT"
    LOT = "LOT"
    BASE_UNIT = "BASE_UNIT"


class SettlementStyle(StrEnum):
    """How profit and loss is denominated.

    LINEAR   -- PnL in the quote currency, size in base units.
    INVERSE  -- contract is a fixed amount of *quote*, PnL settles in base.
    """

    LINEAR = "LINEAR"
    INVERSE = "INVERSE"


class MarginModel(StrEnum):
    ISOLATED_LINEAR = "ISOLATED_LINEAR"
    CROSS_LINEAR = "CROSS_LINEAR"
    BROKER_LEVERAGE = "BROKER_LEVERAGE"


class FundingModel(StrEnum):
    """How the instrument charges for carry."""

    PERPETUAL_FUNDING = "PERPETUAL_FUNDING"
    SWAP_POINTS = "SWAP_POINTS"
    NONE = "NONE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class HedgeObjective(StrEnum):
    """What the hedge is trying to neutralise.

    The objectives differ materially for inverse instruments and for legs
    quoted in different currencies -- see ``HEDGE_ENGINE.md``.
    """

    BASE_ASSET_NEUTRAL = "BASE_ASSET_NEUTRAL"
    NOTIONAL_NEUTRAL = "NOTIONAL_NEUTRAL"
    QUOTE_PNL_NEUTRAL = "QUOTE_PNL_NEUTRAL"
    ACCOUNT_CCY_PNL_NEUTRAL = "ACCOUNT_CCY_PNL_NEUTRAL"
    CUSTOM_RATIO = "CUSTOM_RATIO"
    FUNDING_ADJUSTED = "FUNDING_ADJUSTED"
    COST_ADJUSTED = "COST_ADJUSTED"
    RISK_WEIGHTED = "RISK_WEIGHTED"
    PARTIAL = "PARTIAL"


class OptimizerMode(StrEnum):
    EXACT = "EXACT"
    STEP_SEARCH = "STEP_SEARCH"
    WEIGHTED = "WEIGHTED"


class CycleState(StrEnum):
    """States of the two-leg hedge execution machine."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    AWAITING_MARKET_DATA = "AWAITING_MARKET_DATA"
    CALCULATED = "CALCULATED"
    RISK_APPROVED = "RISK_APPROVED"
    LEG_1_SUBMITTED = "LEG_1_SUBMITTED"
    LEG_1_PARTIAL = "LEG_1_PARTIAL"
    LEG_1_FILLED = "LEG_1_FILLED"
    LEG_2_SUBMITTED = "LEG_2_SUBMITTED"
    LEG_2_PARTIAL = "LEG_2_PARTIAL"
    BOTH_FILLED = "BOTH_FILLED"
    REBALANCING = "REBALANCING"
    RISK_REDUCTION = "RISK_REDUCTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    EMERGENCY = "EMERGENCY"

    @property
    def is_terminal(self) -> bool:
        return self in (CycleState.COMPLETED, CycleState.FAILED)

    @property
    def has_exposure(self) -> bool:
        """True when leg 1 may have put exposure on the book."""
        return self in (
            CycleState.LEG_1_SUBMITTED,
            CycleState.LEG_1_PARTIAL,
            CycleState.LEG_1_FILLED,
            CycleState.LEG_2_SUBMITTED,
            CycleState.LEG_2_PARTIAL,
            CycleState.BOTH_FILLED,
            CycleState.REBALANCING,
            CycleState.RISK_REDUCTION,
            CycleState.RECOVERY_REQUIRED,
            CycleState.EMERGENCY,
        )


class RiskLevel(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    DANGER = "DANGER"
    EMERGENCY = "EMERGENCY"
    KILL_SWITCH = "KILL_SWITCH"

    @property
    def rank(self) -> int:
        order = [
            RiskLevel.NORMAL,
            RiskLevel.WARNING,
            RiskLevel.DANGER,
            RiskLevel.EMERGENCY,
            RiskLevel.KILL_SWITCH,
        ]
        return order.index(self)


class EmergencyAction(StrEnum):
    STOP_NEW_TRADES = "STOP_NEW_TRADES"
    PAUSE_PAIR = "PAUSE_PAIR"
    REDUCE_EXPOSURE = "REDUCE_EXPOSURE"
    REBALANCE = "REBALANCE"
    CANCEL_OPEN_ORDERS = "CANCEL_OPEN_ORDERS"
    FLATTEN_POSITIONS = "FLATTEN_POSITIONS"
    ENTER_EMERGENCY_MODE = "ENTER_EMERGENCY_MODE"


class FaultKind(StrEnum):
    """Injectable failure modes for the demo/fault-injection framework."""

    LEG1_PARTIAL_FILL = "LEG1_PARTIAL_FILL"
    LEG2_PARTIAL_FILL = "LEG2_PARTIAL_FILL"
    ORDER_REJECTION = "ORDER_REJECTION"
    API_TIMEOUT = "API_TIMEOUT"
    DELTA_DISCONNECT = "DELTA_DISCONNECT"
    MT5_DISCONNECT = "MT5_DISCONNECT"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    WIDE_SPREAD = "WIDE_SPREAD"
    HIGH_SLIPPAGE = "HIGH_SLIPPAGE"
    UNEXPECTED_POSITION = "UNEXPECTED_POSITION"
    DUPLICATE_EXECUTION_REPORT = "DUPLICATE_EXECUTION_REPORT"
    DATABASE_RESTART = "DATABASE_RESTART"
    APPLICATION_RESTART = "APPLICATION_RESTART"
    PRICE_GAP = "PRICE_GAP"


class MarketScenario(StrEnum):
    NORMAL = "NORMAL"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    SPREAD_WIDENING = "SPREAD_WIDENING"
    PRICE_GAP = "PRICE_GAP"
    STALE_DATA = "STALE_DATA"
    LIQUIDITY_REDUCTION = "LIQUIDITY_REDUCTION"
    EXCHANGE_DISCONNECT = "EXCHANGE_DISCONNECT"


class ReconciliationIssue(StrEnum):
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    MISSING_POSITION = "MISSING_POSITION"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    ORDER_MISMATCH = "ORDER_MISMATCH"
    STALE_STATE = "STALE_STATE"


class Leg(StrEnum):
    SOURCE = "SOURCE"
    HEDGE = "HEDGE"
