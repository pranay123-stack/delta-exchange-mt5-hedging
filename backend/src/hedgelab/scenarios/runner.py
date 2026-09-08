"""Scenario execution and persistence."""

from __future__ import annotations

from typing import Any

from ..logging_setup import correlation_scope, get_logger, new_correlation_id
from ..service import HedgeLabService
from .definitions import SCENARIOS, ScenarioResult, get_scenario

log = get_logger(__name__)


class ScenarioRunner:
    """Runs a demo scenario against the live service and records the outcome."""

    def __init__(self, service: HedgeLabService) -> None:
        self.service = service

    def available(self) -> list[dict[str, str]]:
        return [d.to_dict() for d in SCENARIOS.values()]

    async def run(self, key: str, seed: int | None = None) -> ScenarioResult:
        definition = get_scenario(key)
        result = ScenarioResult(key=definition.key, name=definition.name, passed=False)

        async with self.service.database.session() as session:
            from ..db.repositories import ObservabilityRepository

            row = await ObservabilityRepository(session).start_scenario(
                definition.key, seed or self.service.settings.simulator_seed
            )
            row_id = row.id

        with correlation_scope(new_correlation_id()):
            log.info("scenario started", extra={"scenario": definition.key,
                                                "scenario_name": definition.name})
            try:
                await definition.run(self.service, result)
            except Exception as exc:
                result.passed = False
                result.error = f"{type(exc).__name__}: {exc}"
                result.summary = result.summary or "scenario raised before completing"
                log.exception("scenario failed", extra={"scenario": definition.key})

        async with self.service.database.session() as session:
            from sqlalchemy import select

            from ..db.models import ScenarioRunRow
            from ..db.repositories import ObservabilityRepository

            row = (
                await session.execute(select(ScenarioRunRow).where(ScenarioRunRow.id == row_id))
            ).scalar_one()
            await ObservabilityRepository(session).finish_scenario(
                row, "PASSED" if result.passed else "FAILED", result.to_dict()
            )
        log.info(
            "scenario finished",
            extra={"scenario": definition.key, "passed": result.passed},
        )
        return result

    async def run_all(self) -> list[ScenarioResult]:
        results: list[ScenarioResult] = []
        for key in sorted(SCENARIOS):
            results.append(await self.run(key))
        return results

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        from ..db.repositories import ObservabilityRepository

        async with self.service.database.session() as session:
            rows = await ObservabilityRepository(session).scenario_runs(limit)
            return [
                {
                    "scenario": r.scenario, "status": r.status, "seed": r.seed,
                    "summary": r.summary.get("summary") if r.summary else None,
                    "passed": r.summary.get("passed") if r.summary else None,
                    "started_at": r.started_at.isoformat(),
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
                for r in rows
            ]
