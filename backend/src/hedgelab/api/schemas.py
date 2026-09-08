"""Request and response models.

Decimals serialise as **strings**, never floats.  A quantity that round-trips
through a JSON float can come back off the venue's lattice, and the dashboard
would then display a number the venue would reject.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from ..domain.enums import (
    FaultKind,
    HedgeObjective,
    MarketScenario,
    OptimizerMode,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    @field_serializer("*", when_used="json")
    def _decimals_as_strings(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        return value


# ======================================================================
# hedge calculation
# ======================================================================
class HedgeCalculationRequest(ApiModel):
    source_key: str = Field(description="Source instrument, VENUE:SYMBOL")
    hedge_key: str = Field(description="Hedge instrument, VENUE:SYMBOL")
    source_quantity: Decimal = Field(description="Signed source quantity in venue units")
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL
    target_ratio: Decimal = Decimal(1)
    use_live_positions: bool = True
    #: Statistical inputs for the risk- and cost-aware objectives.
    source_daily_vol: Decimal | None = None
    hedge_daily_vol: Decimal | None = None
    correlation: Decimal | None = None
    risk_aversion: Decimal | None = None
    horizon_days: Decimal | None = None


class OptimizerRequest(HedgeCalculationRequest):
    mode: OptimizerMode = OptimizerMode.STEP_SEARCH
    priority: str = "MIN_RESIDUAL"
    search_steps: int = Field(default=6, ge=0, le=50)
    max_residual_bps: Decimal | None = None
    weight_residual: Decimal | None = None
    weight_cost: Decimal | None = None
    weight_funding: Decimal | None = None
    weight_margin: Decimal | None = None
    weight_liquidation: Decimal | None = None
    weight_slippage: Decimal | None = None
    weight_capital: Decimal | None = None


class ExecuteHedgeRequest(ApiModel):
    mapping_name: str
    source_quantity: Decimal = Field(
        default=Decimal(0),
        description="Signed source trade. Zero hedges an existing position.",
    )
    objective: HedgeObjective | None = None
    target_ratio: Decimal | None = None
    reason: str = "operator request"
    optimizer: OptimizerRequest | None = None


class OpenPositionRequest(ApiModel):
    instrument_key: str
    quantity: Decimal = Field(description="Signed quantity in venue units")


# ======================================================================
# configuration
# ======================================================================
class InstrumentUpsertRequest(ApiModel):
    """A full instrument specification, as accepted by the YAML catalogue."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    venue: str


class MappingUpsertRequest(ApiModel):
    name: str
    source_key: str
    hedge_key: str
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL
    target_ratio: Decimal = Decimal(1)
    tolerance_bps: Decimal = Decimal(25)
    max_notional: Decimal | None = None
    enabled: bool = True


class RiskThresholdUpdate(ApiModel):
    warning_margin_level: Decimal | None = None
    danger_margin_level: Decimal | None = None
    emergency_margin_level: Decimal | None = None
    kill_switch_margin_level: Decimal | None = None
    warning_residual_bps: Decimal | None = None
    danger_residual_bps: Decimal | None = None
    emergency_residual_bps: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_spread_bps: Decimal | None = None


class PortfolioLimitUpdate(ApiModel):
    max_total_notional: Decimal | None = None
    max_aggregate_margin_utilization: Decimal | None = None
    max_concentration_pct: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_net_exposure_pct: Decimal | None = None
    max_currency_exposure: Decimal | None = None


# ======================================================================
# simulation and faults
# ======================================================================
class ScenarioRequest(ApiModel):
    scenario: MarketScenario
    venue: str | None = None


class AdvanceRequest(ApiModel):
    steps: int = Field(default=1, ge=1, le=100000)


class ShockRequest(ApiModel):
    underlying: str
    pct_move: Decimal = Field(description="Relative move, e.g. -0.03 for -3%")


class FundingRateRequest(ApiModel):
    instrument_key: str
    rate: Decimal = Field(description="Funding rate per interval, as a fraction")


class SettleFundingRequest(ApiModel):
    hours: Decimal | None = Field(
        default=None,
        description="Hours to settle. Omit for exactly one interval / one night.",
    )


class FaultRequest(ApiModel):
    kind: FaultKind
    venue: str | None = None
    symbol: str | None = None
    leg: str | None = Field(default=None, description="SOURCE or HEDGE")
    count: int | None = Field(default=1, description="Firings; null means unlimited")
    magnitude: Decimal = Decimal("0.5")
    probability: Decimal = Decimal(1)
    reason: str = "operator-injected fault"


class KillSwitchRequest(ApiModel):
    reason: str = Field(min_length=3, description="Recorded in the audit trail")


class RunScenarioRequest(ApiModel):
    scenario: str = Field(description="Scenario letter A-J, or its name")
    seed: int | None = None


# ======================================================================
# generic envelopes
# ======================================================================
class Message(ApiModel):
    detail: str


class HealthResponse(ApiModel):
    status: str
    version: str
    trading_mode: str
    paper_only: bool


class ReadinessResponse(ApiModel):
    ready: bool
    database: bool
    venues: dict[str, bool]
    detail: str
