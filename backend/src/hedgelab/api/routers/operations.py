"""Simulation control, fault injection, reconciliation, audit and system status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...domain.enums import FaultKind
from ...logging_setup import get_logger
from ...marketdata.scenarios import all_profiles
from ...venues.base import VenueError
from ..deps import Service
from ..schemas import (
    AdvanceRequest,
    FaultRequest,
    FundingRateRequest,
    HealthResponse,
    ReadinessResponse,
    ScenarioRequest,
    ShockRequest,
)
from ..security import RequireAdmin, RequireConfirmation, RequireTrader

log = get_logger(__name__)

router = APIRouter(tags=["operations"])


# ======================================================================
# system
# ======================================================================
@router.get("/system/status", summary="Full system status")
async def system_status(service: Service) -> dict[str, Any]:
    return await service.status()


@router.get("/health", summary="Liveness probe", response_model=HealthResponse)
async def health(service: Service) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=service.settings.version,
        trading_mode=service.settings.trading_mode.value,
        paper_only=True,
    )


@router.get("/ready", summary="Readiness probe", response_model=ReadinessResponse)
async def ready(service: Service) -> ReadinessResponse:
    database_ok = await service.database.ping()
    venues: dict[str, bool] = {}
    for name, venue in service.venues.items():
        engine = getattr(venue, "engine", None)
        venues[name] = bool(engine.is_connected) if engine is not None else True
    all_ok = database_ok and all(venues.values())
    detail = "ready" if all_ok else "; ".join(
        ([] if database_ok else ["database unreachable"])
        + [f"{n} disconnected" for n, ok in venues.items() if not ok]
    )
    return ReadinessResponse(ready=all_ok, database=database_ok, venues=venues, detail=detail)


# ======================================================================
# paper market simulation
# ======================================================================
@router.get("/paper/simulation", summary="Simulator state and available scenarios")
async def simulation_state(service: Service) -> dict[str, Any]:
    return {
        "clock": service.simulator.clock.isoformat(),
        "seed": service.simulator.seed,
        "tick_seconds": str(service.simulator.tick_seconds),
        "active_scenarios": service.simulator.active_scenarios(),
        "underlyings": service.simulator.state_digest(),
        "available_scenarios": [
            {
                "scenario": p.scenario.value, "description": p.description,
                "volatility_multiplier": str(p.volatility_multiplier),
                "spread_multiplier": str(p.spread_multiplier),
                "liquidity_multiplier": str(p.liquidity_multiplier),
                "jump_probability": str(p.jump_probability),
                "latency_ms": p.latency_ms, "freeze_feed": p.freeze_feed,
                "disconnect": p.disconnect,
            }
            for p in all_profiles()
        ],
    }


@router.post("/paper/simulation/advance", summary="Advance the simulated clock")
async def advance(
    payload: AdvanceRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    service.advance_market(payload.steps)
    return {
        "steps": payload.steps,
        "clock": service.simulator.clock.isoformat(),
        "underlyings": service.simulator.state_digest(),
    }


@router.post("/paper/simulation/scenario", summary="Apply a market scenario")
async def set_scenario(
    payload: ScenarioRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    if payload.venue and payload.venue not in service.venues:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown venue {payload.venue!r}")
    return service.set_scenario(payload.scenario, payload.venue)


@router.delete("/paper/simulation/scenario", summary="Return to a normal market")
async def clear_scenario(service: Service, principal: RequireTrader) -> dict[str, Any]:
    service.simulator.clear_scenario()
    return {"active": service.simulator.active_scenarios()}


@router.post("/paper/simulation/shock", summary="Apply an instantaneous price move")
async def shock(
    payload: ShockRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    try:
        return service.apply_shock(payload.underlying, payload.pct_move)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/paper/simulation/funding-rate", summary="Override a funding rate")
async def set_funding_rate(
    payload: FundingRateRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    if service.registry.find(payload.instrument_key) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown instrument {payload.instrument_key!r}"
        )
    return service.set_funding_rate(payload.instrument_key, payload.rate)


# ======================================================================
# fault injection
# ======================================================================
@router.get("/fault-injection", summary="Armed faults and firing history")
async def list_faults(service: Service) -> dict[str, Any]:
    return {
        "available": [
            {"kind": k.value, "immediate": k in {
                FaultKind.DELTA_DISCONNECT, FaultKind.MT5_DISCONNECT,
                FaultKind.STALE_MARKET_DATA, FaultKind.WIDE_SPREAD,
                FaultKind.PRICE_GAP, FaultKind.UNEXPECTED_POSITION,
            }}
            for k in FaultKind
        ],
        "armed": [f.to_dict() for f in service.faults.armed()],
        "history": service.faults.history(50),
        "venue_connectivity": {
            name: engine.is_connected
            for name, venue in service.venues.items()
            # ``engine`` lives on the paper adapters, not on the protocol.
            if (engine := getattr(venue, "engine", None)) is not None
        },
        "active_scenarios": service.simulator.active_scenarios(),
    }


@router.post(
    "/fault-injection",
    summary="Arm or apply a fault",
    description=(
        "Most faults are *armed* and fire the next time the matching code path "
        "runs, which keeps them reproducible. Disconnects, stale data, wide "
        "spreads and price gaps are states and apply immediately."
    ),
)
async def inject_fault(
    payload: FaultRequest, service: Service, principal: RequireAdmin
) -> dict[str, Any]:
    if payload.venue and payload.venue not in service.venues:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown venue {payload.venue!r}")
    return await service.inject_fault(
        payload.kind, venue=payload.venue, symbol=payload.symbol, leg=payload.leg,
        count=payload.count, magnitude=payload.magnitude,
        probability=payload.probability, reason=payload.reason,
    )


@router.delete("/fault-injection", summary="Disarm faults and restore normal conditions")
async def clear_faults(
    service: Service, principal: RequireAdmin, kind: FaultKind | None = None
) -> dict[str, Any]:
    return service.clear_faults(kind)


@router.post("/fault-injection/reconnect/{venue}", summary="Reconnect a venue")
async def reconnect(venue: str, service: Service, principal: RequireAdmin) -> dict[str, Any]:
    if venue not in service.venues:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown venue {venue!r}")
    return service.reconnect(venue)


# ======================================================================
# reconciliation
# ======================================================================
@router.get(
    "/reconciliation",
    summary="Three-way reconciliation",
    description="Compares the database against the live perpetual venue and "
                "the live broker, and classifies every disagreement.",
)
async def reconcile(service: Service) -> dict[str, Any]:
    return (await service.reconcile()).to_dict()


@router.post(
    "/reconciliation/recover",
    summary="Rebuild state and decide whether it is safe to resume",
    description="Restart recovery: reads paper positions, open orders and "
                "unfinished cycles, compares them, and reports what must "
                "happen before trading may resume.",
)
async def recover(service: Service, principal: RequireTrader) -> dict[str, Any]:
    return (await service.recover()).to_dict()


@router.post(
    "/reconciliation/adopt",
    summary="Overwrite the database with live venue state",
    description="The venue holds the money, so it is authoritative. This is an "
                "explicit, audited action. Requires X-Confirm-Action: CONFIRM.",
)
async def adopt(
    service: Service, principal: RequireAdmin, _: RequireConfirmation
) -> dict[str, Any]:
    try:
        return await service.adopt_venue_state(actor=principal.username)
    except VenueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


# ======================================================================
# audit
# ======================================================================
@router.get("/audit", summary="Audit trail")
async def audit(
    service: Service, limit: int = 200, entity_type: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).audit_trail(
            limit=limit, entity_type=entity_type, correlation_id=correlation_id
        )
        return {
            "count": len(rows),
            "entries": [
                {
                    "actor": r.actor, "action": r.action, "entity_type": r.entity_type,
                    "entity_id": r.entity_id, "before": r.before, "after": r.after,
                    "correlation_id": r.correlation_id,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


@router.get("/audit/system-events", summary="System event log")
async def system_events(service: Service, limit: int = 100) -> dict[str, Any]:
    from ...db.repositories import ObservabilityRepository

    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).system_events(limit=limit)
        return {
            "count": len(rows),
            "events": [
                {
                    "kind": r.kind, "severity": r.severity, "component": r.component,
                    "message": r.message, "payload": r.payload,
                    "correlation_id": r.correlation_id,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


@router.get("/audit/configuration-changes", summary="Configuration change log")
async def configuration_changes(service: Service, limit: int = 100) -> dict[str, Any]:
    from sqlalchemy import desc, select

    from ...db.models import ConfigurationChangeRow

    async with service.database.session() as session:
        stmt = (
            select(ConfigurationChangeRow)
            .order_by(desc(ConfigurationChangeRow.timestamp))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            "count": len(rows),
            "changes": [
                {
                    "actor": r.actor, "entity": r.entity, "entity_id": r.entity_id,
                    "before": r.before, "after": r.after, "note": r.note,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }
