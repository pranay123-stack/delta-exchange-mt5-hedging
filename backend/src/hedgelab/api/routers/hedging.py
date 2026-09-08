"""Hedge calculation, optimisation, execution and cycle history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...domain.enums import CycleState
from ...hedge.objectives import ObjectiveError
from ...instruments.registry import InstrumentNotFound, MappingNotFound
from ...logging_setup import get_logger
from ...venues.base import VenueError
from ..deps import HTTP_422, Service, optimizer_config_from, risk_params_from
from ..schemas import (
    ExecuteHedgeRequest,
    HedgeCalculationRequest,
    OpenPositionRequest,
    OptimizerRequest,
)
from ..security import RequireTrader

log = get_logger(__name__)

router = APIRouter(prefix="/hedge", tags=["hedging"])


@router.post(
    "/calculate",
    summary="Calculate a hedge without trading",
    description=(
        "Returns the full derivation: required quantity, achieved ratio, "
        "residual exposure, fees, spread, slippage, funding, margin and the "
        "step-by-step working behind every number."
    ),
)
async def calculate(payload: HedgeCalculationRequest, service: Service) -> dict[str, Any]:
    try:
        calculation = await service.calculate_hedge(
            source_key=payload.source_key,
            hedge_key=payload.hedge_key,
            source_quantity=payload.source_quantity,
            objective=payload.objective,
            target_ratio=payload.target_ratio,
            risk_params=risk_params_from(payload),
            use_live_positions=payload.use_live_positions,
        )
    except InstrumentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (ObjectiveError, ValueError) as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return calculation.to_dict()


@router.post(
    "/optimize",
    summary="Search the quantity lattice for the best hedge",
    description=(
        "Evaluates neighbouring lattice points and picks the best by a single "
        "priority or a weighted score. Returns every candidate that was "
        "considered, so the choice can be justified."
    ),
)
async def optimize(payload: OptimizerRequest, service: Service) -> dict[str, Any]:
    config = optimizer_config_from(payload)
    try:
        result = await service.optimize_hedge(
            config,
            source_key=payload.source_key,
            hedge_key=payload.hedge_key,
            source_quantity=payload.source_quantity,
            objective=payload.objective,
            target_ratio=payload.target_ratio,
            risk_params=risk_params_from(payload),
            use_live_positions=payload.use_live_positions,
        )
    except InstrumentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (ObjectiveError, ValueError) as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc
    return {**result.to_dict(), "calculation": result.calculation.to_dict()}


@router.post(
    "/execute",
    summary="Run a two-leg paper hedge cycle",
    description=(
        "Validates, fetches market data, calculates, risk-checks, executes both "
        "legs and rebalances if the achieved residual is outside tolerance. "
        "Every state transition is persisted. PAPER ONLY."
    ),
)
async def execute(
    payload: ExecuteHedgeRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    try:
        result = await service.execute_hedge(
            payload.mapping_name,
            payload.source_quantity,
            objective=payload.objective,
            target_ratio=payload.target_ratio,
            optimizer=optimizer_config_from(payload.optimizer) if payload.optimizer else None,
            reason=f"{payload.reason} (by {principal.username})",
        )
    except MappingNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return result.to_dict()


@router.post(
    "/open-position",
    summary="Take a naked position on the source venue",
    description=(
        "Creates something that needs hedging. Goes through the same paper "
        "venue as every other order -- there is no path that writes a position "
        "directly."
    ),
)
async def open_position(
    payload: OpenPositionRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    try:
        order = await service.open_source_position(payload.instrument_key, payload.quantity)
    except InstrumentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "order_id": order.order_id,
        "status": order.status.value,
        "filled_quantity": str(order.filled_quantity),
        "average_price": str(order.average_price),
        "fees_paid": str(order.fees_paid),
        "is_paper": True,
    }


@router.get("/rebalance/{mapping_name}", summary="Assess whether a pair needs rebalancing")
async def assess_rebalance(mapping_name: str, service: Service) -> dict[str, Any]:
    try:
        return (await service.assess_rebalance(mapping_name)).to_dict()
    except MappingNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.post("/rebalance/{mapping_name}", summary="Rebalance a pair back into tolerance")
async def rebalance(
    mapping_name: str, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    try:
        return await service.rebalance(mapping_name)
    except MappingNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/cycles", summary="Hedge cycle history")
async def list_cycles(
    service: Service, limit: int = 50, state: str | None = None
) -> dict[str, Any]:
    from ...db.repositories import CycleRepository

    states = None
    if state:
        try:
            states = [CycleState(state.upper()).value]
        except ValueError as exc:
            raise HTTPException(
                HTTP_422,
                f"unknown cycle state {state!r}",
            ) from exc
    async with service.database.session() as session:
        rows = await CycleRepository(session).list(limit=limit, states=states)
        return {
            "count": len(rows),
            "cycles": [
                {
                    "cycle_id": r.cycle_id, "pair": r.pair_name, "state": r.state,
                    "objective": r.objective, "source": r.source_key, "hedge": r.hedge_key,
                    "source_target_quantity": str(r.source_target_quantity),
                    "hedge_target_quantity": str(r.hedge_target_quantity),
                    "source_filled_quantity": str(r.source_filled_quantity),
                    "hedge_filled_quantity": str(r.hedge_filled_quantity),
                    "hedge_ratio": str(r.hedge_ratio),
                    "residual_exposure": str(r.residual_exposure),
                    "error": r.error, "correlation_id": r.correlation_id,
                    "is_paper": r.is_paper,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in rows
            ],
        }


@router.get(
    "/cycles/{cycle_id}",
    summary="One hedge cycle with its full state-machine history",
)
async def get_cycle(cycle_id: str, service: Service) -> dict[str, Any]:
    from ...db.repositories import CycleRepository

    async with service.database.session() as session:
        repo = CycleRepository(session)
        row = await repo.get_row(cycle_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown cycle {cycle_id!r}")
        events = await repo.events(cycle_id)
        return {
            "cycle_id": row.cycle_id, "pair": row.pair_name, "state": row.state,
            "objective": row.objective, "source": row.source_key, "hedge": row.hedge_key,
            "hedge_ratio": str(row.hedge_ratio),
            "residual_exposure": str(row.residual_exposure),
            "source_order_id": row.source_order_id, "hedge_order_id": row.hedge_order_id,
            "error": row.error, "correlation_id": row.correlation_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "events": [
                {
                    "sequence": e.sequence, "from_state": e.from_state,
                    "to_state": e.to_state, "event": e.event, "payload": e.payload,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
        }


@router.get("/state-machine", summary="The hedge-cycle transition graph")
async def state_machine() -> dict[str, Any]:
    from ...execution.state_machine import describe_graph, validate_graph

    return {
        "states": [s.value for s in CycleState],
        "terminal": [s.value for s in CycleState if s.is_terminal],
        "exposed": [s.value for s in CycleState if s.has_exposure],
        "transitions": {name: targets for name, targets in describe_graph()},
        "problems": validate_graph(),
    }
