"""FastAPI dependency wiring.

The service is a process-wide singleton created at startup and stored on the
application state, so every request shares one set of venues, one simulator and
one database pool.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from ..config import Settings, get_settings
from ..hedge.objectives import RiskParameters
from ..hedge.optimizer import OptimizerConfig, OptimizerWeights
from ..service import HedgeLabService
from .schemas import HedgeCalculationRequest, OptimizerRequest

#: Starlette renamed 422 between releases; accept whichever this version has.
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", None) or status.HTTP_422_UNPROCESSABLE_ENTITY


def get_service(request: Request) -> HedgeLabService:
    service: HedgeLabService | None = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - only if startup failed
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service is not initialised",
        )
    return service


Service = Annotated[HedgeLabService, Depends(get_service)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def risk_params_from(request: HedgeCalculationRequest) -> RiskParameters:
    """Overlay any supplied statistical inputs onto the defaults."""
    defaults = RiskParameters()
    return RiskParameters(
        source_daily_vol=request.source_daily_vol or defaults.source_daily_vol,
        hedge_daily_vol=request.hedge_daily_vol or defaults.hedge_daily_vol,
        correlation=request.correlation or defaults.correlation,
        risk_aversion=request.risk_aversion or defaults.risk_aversion,
        horizon_days=request.horizon_days or defaults.horizon_days,
    )


def optimizer_config_from(request: OptimizerRequest) -> OptimizerConfig:
    defaults = OptimizerWeights()
    weights = OptimizerWeights(
        residual_exposure=request.weight_residual or defaults.residual_exposure,
        execution_cost=request.weight_cost or defaults.execution_cost,
        funding_benefit=request.weight_funding or defaults.funding_benefit,
        margin_usage=request.weight_margin or defaults.margin_usage,
        liquidation_risk=request.weight_liquidation or defaults.liquidation_risk,
        slippage=request.weight_slippage or defaults.slippage,
        capital_efficiency=request.weight_capital or defaults.capital_efficiency,
    )
    try:
        return OptimizerConfig(
            mode=request.mode, priority=request.priority,
            search_steps=request.search_steps, weights=weights,
            max_residual_bps=request.max_residual_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc
