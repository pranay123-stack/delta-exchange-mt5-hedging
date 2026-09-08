"""The generic instrument specification.

Nothing in the engine knows what "BTC", "gold" or "XAUUSD" means.  An
instrument is a *record* of the parameters a venue publishes in its contract
specification, and every calculation in the platform is driven by that record.
Adding a new instrument means adding a YAML file or a database row -- never a
source change.  ``tests/unit/test_generic_instruments.py`` enforces that by
hedging an instrument the code has never seen.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    FundingModel,
    InstrumentType,
    MarginModel,
    QuantityUnit,
    SettlementStyle,
    VenueKind,
)
from .numeric import ZERO, dec, decimal_places

PositiveDec = Annotated[Decimal, Field(gt=0)]
NonNegDec = Annotated[Decimal, Field(ge=0)]


class TradingSession(BaseModel):
    """One continuous window during which the instrument trades."""

    model_config = ConfigDict(frozen=True)

    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # 0 = Monday
    start: time = time(0, 0)
    end: time = time(23, 59, 59)

    def contains(self, weekday: int, at: time) -> bool:
        if weekday not in self.days:
            return False
        if self.start <= self.end:
            return self.start <= at <= self.end
        # Session wraps midnight (e.g. 22:00 -> 06:00).
        return at >= self.start or at <= self.end


class TradingHours(BaseModel):
    """Trading calendar.  ``always_open`` models a 24/7 perpetual venue."""

    model_config = ConfigDict(frozen=True)

    always_open: bool = True
    timezone: str = "UTC"
    sessions: tuple[TradingSession, ...] = ()

    def is_open(self, weekday: int, at: time) -> bool:
        if self.always_open:
            return True
        return any(s.contains(weekday, at) for s in self.sessions)


class InstrumentSpec(BaseModel):
    """Complete, venue-agnostic contract specification.

    The fields are grouped in the order a venue's own contract-spec page lists
    them so a new instrument can be transcribed field by field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- identity -------------------------------------------------------
    symbol: str
    venue: str
    venue_kind: VenueKind
    instrument_type: InstrumentType
    display_name: str = ""

    # --- assets ---------------------------------------------------------
    base_asset: str
    quote_asset: str
    settlement_asset: str

    # --- size / quantity semantics --------------------------------------
    quantity_unit: QuantityUnit
    settlement_style: SettlementStyle = SettlementStyle.LINEAR
    contract_size: PositiveDec = Decimal(1)
    contract_multiplier: PositiveDec = Decimal(1)
    #: Explicit overrides.  When ``None`` they derive from size x multiplier.
    units_per_contract: PositiveDec | None = None
    units_per_lot: PositiveDec | None = None

    # --- price / quantity lattice ---------------------------------------
    tick_size: PositiveDec = Decimal("0.01")
    #: Money value of one tick for one quantity unit.  ``None`` derives it.
    tick_value: PositiveDec | None = None
    min_quantity: PositiveDec = Decimal("0.01")
    max_quantity: PositiveDec = Decimal("1000000")
    quantity_step: PositiveDec = Decimal("0.01")
    price_precision: int = Field(default=2, ge=0, le=12)
    quantity_precision: int = Field(default=2, ge=0, le=12)

    # --- margin ---------------------------------------------------------
    max_leverage: PositiveDec = Decimal(10)
    margin_model: MarginModel = MarginModel.ISOLATED_LINEAR
    initial_margin_rate: PositiveDec | None = None      # defaults to 1/leverage
    maintenance_margin_rate: PositiveDec | None = None  # defaults to half of initial

    # --- costs ----------------------------------------------------------
    maker_fee_bps: Decimal = Decimal("2")
    taker_fee_bps: Decimal = Decimal("5")
    fee_currency: str = ""            # blank -> settlement asset
    typical_spread_bps: NonNegDec = Decimal("2")
    slippage_bps_per_unit_liquidity: NonNegDec = Decimal("0")

    # --- financing ------------------------------------------------------
    funding_model: FundingModel = FundingModel.NONE
    funding_interval_hours: PositiveDec = Decimal(8)
    #: Baseline funding rate per interval, as a fraction (0.0001 = 1 bp).
    baseline_funding_rate: Decimal = Decimal(0)
    #: MT5-style overnight swap, in *points* per lot per night.
    swap_long_points: Decimal = Decimal(0)
    swap_short_points: Decimal = Decimal(0)
    swap_triple_weekday: int = Field(default=2, ge=0, le=6)  # Wednesday

    # --- venue rules ----------------------------------------------------
    trading_hours: TradingHours = TradingHours()
    allow_long: bool = True
    allow_short: bool = True
    price_source: str = "simulator"
    #: Reference symbol used to correlate independent price feeds.
    underlying_key: str = ""
    active: bool = True

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @field_validator(
        "contract_size",
        "contract_multiplier",
        "units_per_contract",
        "units_per_lot",
        "tick_size",
        "tick_value",
        "min_quantity",
        "max_quantity",
        "quantity_step",
        "max_leverage",
        "initial_margin_rate",
        "maintenance_margin_rate",
        "maker_fee_bps",
        "taker_fee_bps",
        "typical_spread_bps",
        "slippage_bps_per_unit_liquidity",
        "baseline_funding_rate",
        "swap_long_points",
        "swap_short_points",
        "funding_interval_hours",
        mode="before",
    )
    @classmethod
    def _coerce_decimal(cls, value: Any) -> Any:
        if value is None or isinstance(value, Decimal):
            return value
        return dec(value)

    @field_validator("symbol", "venue", "base_asset", "quote_asset", "settlement_asset")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _check_consistency(self) -> InstrumentSpec:
        if self.max_quantity < self.min_quantity:
            raise ValueError(
                f"{self.symbol}: max_quantity {self.max_quantity} < min_quantity {self.min_quantity}"
            )
        # The minimum must itself be reachable on the step lattice, otherwise
        # every order the engine builds is rejected by the venue.
        remainder = (self.min_quantity / self.quantity_step) % 1
        if remainder != 0:
            raise ValueError(
                f"{self.symbol}: min_quantity {self.min_quantity} is not a multiple of "
                f"quantity_step {self.quantity_step}"
            )
        if decimal_places(self.quantity_step) > self.quantity_precision:
            raise ValueError(
                f"{self.symbol}: quantity_step {self.quantity_step} needs more decimals "
                f"than quantity_precision {self.quantity_precision}"
            )
        if decimal_places(self.tick_size) > self.price_precision:
            raise ValueError(
                f"{self.symbol}: tick_size {self.tick_size} needs more decimals than "
                f"price_precision {self.price_precision}"
            )
        if self.settlement_style is SettlementStyle.INVERSE and self.quantity_unit is QuantityUnit.LOT:
            raise ValueError(f"{self.symbol}: inverse instruments are not modelled in lots")
        if not self.allow_long and not self.allow_short:
            raise ValueError(f"{self.symbol}: instrument allows neither direction")
        if (
            self.initial_margin_rate is not None
            and self.maintenance_margin_rate is not None
            and self.maintenance_margin_rate > self.initial_margin_rate
        ):
            raise ValueError(
                f"{self.symbol}: maintenance margin rate exceeds initial margin rate"
            )
        return self

    # ------------------------------------------------------------------
    # Derived specification
    # ------------------------------------------------------------------
    @property
    def key(self) -> str:
        """Globally unique instrument key, ``VENUE:SYMBOL``."""
        return f"{self.venue}:{self.symbol}"

    @property
    def units_per_quantity(self) -> Decimal:
        """Economic units carried by one order-quantity unit.

        * ``CONTRACT``/linear -- base-asset units per contract.
        * ``LOT``             -- base-asset units per lot (100 for XAUUSD).
        * ``BASE_UNIT``       -- 1 by construction.
        * ``CONTRACT``/inverse -- *quote* units per contract (1 USD, 100 USD ...).
        """
        if self.quantity_unit is QuantityUnit.BASE_UNIT:
            return Decimal(1)
        if self.quantity_unit is QuantityUnit.LOT:
            if self.units_per_lot is not None:
                return self.units_per_lot
            return self.contract_size * self.contract_multiplier
        if self.units_per_contract is not None:
            return self.units_per_contract
        return self.contract_size * self.contract_multiplier

    @property
    def is_inverse(self) -> bool:
        return self.settlement_style is SettlementStyle.INVERSE

    @property
    def effective_initial_margin_rate(self) -> Decimal:
        if self.initial_margin_rate is not None:
            return self.initial_margin_rate
        return Decimal(1) / self.max_leverage

    @property
    def effective_maintenance_margin_rate(self) -> Decimal:
        if self.maintenance_margin_rate is not None:
            return self.maintenance_margin_rate
        return self.effective_initial_margin_rate / 2

    @property
    def effective_fee_currency(self) -> str:
        return self.fee_currency or self.settlement_asset

    @property
    def effective_underlying_key(self) -> str:
        """Key used to decide two instruments track the same underlying."""
        return self.underlying_key or self.base_asset

    @property
    def effective_tick_value(self) -> Decimal:
        """Money value of one tick for one quantity unit, in quote currency."""
        if self.tick_value is not None:
            return self.tick_value
        if self.is_inverse:
            # An inverse contract's tick value depends on price; the spec value
            # is only a nominal reference and callers should use
            # ``QuantityConverter`` for exact numbers.
            return self.tick_size
        return self.tick_size * self.units_per_quantity

    def describe_sizing(self) -> str:
        """Human-readable one-liner used in logs and the dashboard."""
        unit = self.quantity_unit.value.lower()
        if self.is_inverse:
            return f"1 {unit} = {self.units_per_quantity} {self.quote_asset} (inverse)"
        return f"1 {unit} = {self.units_per_quantity} {self.base_asset}"

    def supports_side_sign(self, signed_quantity: Decimal) -> bool:
        if signed_quantity > ZERO:
            return self.allow_long
        if signed_quantity < ZERO:
            return self.allow_short
        return True
