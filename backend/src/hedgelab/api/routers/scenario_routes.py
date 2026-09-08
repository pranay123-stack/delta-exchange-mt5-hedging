"""Predefined demo scenarios."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...scenarios.runner import ScenarioRunner
from ..deps import Service
from ..schemas import RunScenarioRequest
from ..security import RequireTrader

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("", summary="List the predefined demo scenarios")
async def list_scenarios(service: Service) -> dict[str, Any]:
    return {"scenarios": ScenarioRunner(service).available()}


@router.get("/history", summary="Previous scenario runs")
async def history(service: Service, limit: int = 50) -> dict[str, Any]:
    return {"runs": await ScenarioRunner(service).history(limit)}


@router.post(
    "/run",
    summary="Run one scenario end to end",
    description=(
        "Drives the real engine -- nothing is scripted. Returns every step with "
        "the numbers the engine actually produced."
    ),
)
async def run_scenario(
    payload: RunScenarioRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    try:
        result = await ScenarioRunner(service).run(payload.scenario, payload.seed)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return result.to_dict()


@router.post("/run-all", summary="Run every scenario in sequence")
async def run_all(service: Service, principal: RequireTrader) -> dict[str, Any]:
    results = await ScenarioRunner(service).run_all()
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "results": [r.to_dict() for r in results],
    }
