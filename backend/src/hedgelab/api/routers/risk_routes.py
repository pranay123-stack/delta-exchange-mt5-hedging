"""Risk, portfolio, funding, P&L and emergency controls."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...domain.enums import RiskLevel
from ...instruments.registry import MappingNotFound
from ...logging_setup import get_logger
from ...venues.base import VenueError
from ..deps import Service
from ..schemas import (
    KillSwitchRequest,
    PortfolioLimitUpdate,
    RiskThresholdUpdate,
    SettleFundingRequest,
)
from ..security import RequireAdmin, RequireConfirmation, RequireTrader

log = get_logger(__name__)

router = APIRouter(tags=["risk"])


@router.get("/risk", summary="Risk for every configured hedge pair")
async def all_risk(service: Service) -> dict[str, Any]:
    risks = await service.all_pair_risks()
    worst = max((r.level for r in risks), key=lambda x: x.rank, default=RiskLevel.NORMAL)
    return {
        "count": len(risks),
        "worst_level": worst.value,
        "pairs": [r.to_dict() for r in risks],
    }


@router.get(
    "/risk/statistics",
    summary="Realised volatility, correlation and beta per hedge pair",
    description=(
        "Estimated from the prices this process has observed. A pair without "
        "enough samples reports `is_reliable: false` and the objectives fall "
        "back to the configured assumptions rather than presenting a number "
        "derived from a handful of observations as a measurement."
    ),
)
async def statistics(service: Service) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for mapping in service.registry.mappings(enabled_only=True):
        params, estimate = service.risk_parameters_for(
            mapping.source_key, mapping.hedge_key
        )
        pairs.append({
            "pair": mapping.name,
            "estimated": params.estimated,
            "provenance": params.provenance,
            "applied": {
                "source_daily_vol": str(params.source_daily_vol),
                "hedge_daily_vol": str(params.hedge_daily_vol),
                "correlation": str(params.correlation),
                "beta": str(params.beta),
                "residual_daily_vol": str(params.residual_daily_vol),
            },
            **estimate.to_dict(),
        })
    return {
        "sample_seconds": service.stats.sample_seconds,
        "window": service.stats.window,
        "min_samples": service.stats.min_samples,
        "sample_counts": service.stats.sample_counts(),
        "pairs": pairs,
    }


# NOTE: every literal /risk/... route must be registered *above* this one.
# FastAPI matches in registration order, so a parameterised path declared first
# swallows its literal siblings -- /risk/statistics resolved here and returned
# "unknown hedge mapping named 'statistics'".
@router.get("/risk/{mapping_name}", summary="Risk for one hedge pair")
async def pair_risk(mapping_name: str, service: Service) -> dict[str, Any]:
    try:
        return (await service.pair_risk(mapping_name)).to_dict()
    except MappingNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/portfolio", summary="Aggregate portfolio risk")
async def portfolio(service: Service) -> dict[str, Any]:
    return (await service.portfolio_risk()).to_dict()


@router.post("/risk/snapshot", summary="Persist a risk, margin and P&L snapshot")
async def snapshot(service: Service, principal: RequireTrader) -> dict[str, Any]:
    return await service.persist_risk()


@router.patch("/risk/thresholds", summary="Update risk thresholds")
async def update_thresholds(
    payload: RiskThresholdUpdate, service: Service, principal: RequireAdmin
) -> dict[str, Any]:
    from dataclasses import replace

    from ...db.repositories import ObservabilityRepository

    current = service.risk_engine.thresholds
    before = {k: str(getattr(current, k)) for k in payload.model_dump()}
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    service.risk_engine.thresholds = replace(current, **updates)
    after = {k: str(getattr(service.risk_engine.thresholds, k)) for k in payload.model_dump()}

    async with service.database.session() as session:
        await ObservabilityRepository(session).record_configuration_change(
            actor=principal.username, entity="risk_thresholds", entity_id=None,
            before=before, after=after, note="risk thresholds updated through the API",
        )
    return after


@router.patch("/portfolio/limits", summary="Update portfolio limits")
async def update_limits(
    payload: PortfolioLimitUpdate, service: Service, principal: RequireAdmin
) -> dict[str, Any]:
    from dataclasses import replace

    from ...db.repositories import ObservabilityRepository

    current = service.portfolio_engine.limits
    before = {k: str(getattr(current, k)) for k in payload.model_dump()}
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    service.portfolio_engine.limits = replace(current, **updates)
    after = {k: str(getattr(service.portfolio_engine.limits, k)) for k in payload.model_dump()}

    async with service.database.session() as session:
        await ObservabilityRepository(session).record_configuration_change(
            actor=principal.username, entity="portfolio_limits", entity_id=None,
            before=before, after=after, note="portfolio limits updated through the API",
        )
    return after


# ======================================================================
# funding and P&L
# ======================================================================
@router.get("/funding", summary="Funding and swap projection per pair")
async def funding(service: Service, horizon_days: Decimal = Decimal(1)) -> dict[str, Any]:
    projections = []
    for mapping in service.registry.mappings(enabled_only=True):
        try:
            projection = await service.funding_projection(mapping.name, horizon_days)
        except VenueError:
            continue
        projections.append({"pair": mapping.name, **projection.to_dict()})
    return {"horizon_days": str(horizon_days), "pairs": projections}


@router.get("/funding/history", summary="Applied funding and swap charges")
async def funding_history(service: Service, limit: int = 100) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).funding_history(limit=limit)
        return {
            "count": len(rows),
            "entries": [
                {
                    "venue": r.venue, "symbol": r.symbol, "kind": r.kind,
                    "rate": str(r.rate), "amount": str(r.amount),
                    "currency": r.currency, "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


@router.post(
    "/funding/settle",
    summary="Settle one funding interval / swap night",
    description="Charges perpetual funding and broker swap across all open "
                "paper positions, and records each accrual.",
)
async def settle_funding(
    payload: SettleFundingRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    accruals = service.settle_funding(payload.hours)
    await service.persist_funding(accruals)
    return {"count": len(accruals), "accruals": accruals}


@router.get(
    "/pnl",
    summary="P&L attribution per pair",
    description="Decomposes P&L into price, fees, spread, slippage, funding, "
                "swap and FX so it is clear *why* a hedge is profitable or not.",
)
async def pnl(service: Service) -> dict[str, Any]:
    breakdowns = []
    for mapping in service.registry.mappings(enabled_only=True):
        try:
            breakdown = await service.pair_pnl(mapping.name)
        except VenueError:
            continue
        breakdowns.append({"pair": mapping.name, **breakdown.to_dict()})
    total_net = sum(
        (Decimal(str(entry["net_pnl"])) for entry in breakdowns), Decimal(0)
    )
    return {
        "currency": service.settings.account_currency,
        "total_net_pnl": str(total_net),
        "pairs": breakdowns,
    }


@router.get("/pnl/history", summary="Recorded P&L snapshots")
async def pnl_history(service: Service, limit: int = 100) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).pnl_records(limit=limit)
        return {
            "count": len(rows),
            "records": [
                {
                    "scope": r.scope, "pair": r.pair_name, "currency": r.currency,
                    "gross_pnl": str(r.gross_pnl), "net_pnl": str(r.net_pnl),
                    "trading_fees": str(r.trading_fees),
                    "funding_received": str(r.funding_received),
                    "funding_paid": str(r.funding_paid),
                    "swap_financing": str(r.swap_financing),
                    "spread_cost": str(r.spread_cost),
                    "slippage_cost": str(r.slippage_cost),
                    "fx_impact": str(r.fx_impact),
                    "realized_pnl": str(r.realized_pnl),
                    "unrealized_pnl": str(r.unrealized_pnl),
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


@router.get("/margin/history", summary="Recorded margin snapshots")
async def margin_history(
    service: Service, venue: str | None = None, limit: int = 200
) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).margin_snapshots(venue=venue, limit=limit)
        return {
            "count": len(rows),
            "snapshots": [
                {
                    "venue": r.venue, "balance": str(r.balance), "equity": str(r.equity),
                    "used_margin": str(r.used_margin), "free_margin": str(r.free_margin),
                    "margin_level": str(r.margin_level),
                    "maintenance_margin": str(r.maintenance_margin),
                    "unrealized_pnl": str(r.unrealized_pnl),
                    "open_positions": r.open_positions,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


# ======================================================================
# emergency
# ======================================================================
@router.post(
    "/emergency/kill-switch",
    summary="Engage the kill switch",
    description="Halts trading, cancels every working order and flattens every "
                "paper position. Irreversible without an explicit clear. "
                "Requires the X-Confirm-Action: CONFIRM header.",
)
async def kill_switch(
    payload: KillSwitchRequest,
    service: Service,
    principal: RequireAdmin,
    _: RequireConfirmation,
) -> dict[str, Any]:
    return await service.engage_kill_switch(payload.reason, actor=principal.username)


@router.delete("/emergency/kill-switch", summary="Clear the kill switch")
async def clear_kill_switch(service: Service, principal: RequireAdmin) -> dict[str, Any]:
    return await service.clear_kill_switch(actor=principal.username)


@router.post(
    "/emergency/apply",
    summary="Apply the risk engine's prescribed actions for a pair",
    description="Runs whatever the current risk level calls for -- rebalance, "
                "pause, cancel orders, reduce exposure or flatten.",
)
async def apply_actions(
    mapping_name: str, service: Service, principal: RequireAdmin
) -> dict[str, Any]:
    try:
        risk = await service.pair_risk(mapping_name)
    except MappingNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not risk.actions:
        return {"level": risk.level.value, "applied": [],
                "detail": "risk level requires no action"}
    applied = await service.apply_emergency_actions(
        risk.level, risk.actions, trigger=f"manual apply for {mapping_name}",
        scope=mapping_name,
    )
    return {"level": risk.level.value, "applied": applied}


@router.get("/emergency/actions", summary="Emergency action history")
async def emergency_actions(service: Service, limit: int = 100) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).emergency_actions(limit=limit)
        return {
            "count": len(rows),
            "actions": [
                {
                    "trigger": r.trigger, "level": r.level, "action": r.action,
                    "scope": r.scope, "executed": r.executed, "result": r.result,
                    "actor": r.actor, "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


@router.get("/risk/events/log", summary="Risk event history")
async def risk_events(
    service: Service, limit: int = 100, level: str | None = None
) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).risk_events(limit=limit, level=level)
        return {
            "count": len(rows),
            "events": [
                {
                    "level": r.level, "scope": r.scope, "pair": r.pair_name,
                    "metric": r.metric, "value": str(r.value),
                    "threshold": str(r.threshold), "message": r.message,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }
