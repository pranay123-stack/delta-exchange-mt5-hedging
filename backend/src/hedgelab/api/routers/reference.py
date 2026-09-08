"""Instruments, mappings and hedge configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...domain.instrument import InstrumentSpec
from ...instruments.registry import HedgeMapping, InstrumentNotFound
from ...logging_setup import get_logger
from ..deps import HTTP_422, Service
from ..schemas import InstrumentUpsertRequest, MappingUpsertRequest, Message
from ..security import RequireAdmin

log = get_logger(__name__)

router = APIRouter(tags=["reference data"])


def _spec_payload(spec: InstrumentSpec) -> dict[str, Any]:
    data = spec.model_dump(mode="json")
    data.update({
        "key": spec.key,
        "units_per_quantity": str(spec.units_per_quantity),
        "sizing_description": spec.describe_sizing(),
        "is_inverse": spec.is_inverse,
        "effective_initial_margin_rate": str(spec.effective_initial_margin_rate),
        "effective_maintenance_margin_rate": str(spec.effective_maintenance_margin_rate),
        "effective_underlying_key": spec.effective_underlying_key,
    })
    return data


@router.get("/instruments", summary="List every configured instrument")
async def list_instruments(
    service: Service, venue: str | None = None, active_only: bool = False
) -> dict[str, Any]:
    specs = service.registry.all(active_only=active_only)
    if venue:
        specs = [s for s in specs if s.venue == venue]
    return {
        "count": len(specs),
        "venues": service.registry.venues(),
        "instruments": [_spec_payload(s) for s in specs],
    }


@router.get("/instruments/{key:path}", summary="One instrument specification")
async def get_instrument(key: str, service: Service) -> dict[str, Any]:
    try:
        return _spec_payload(service.registry.get(key))
    except InstrumentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post(
    "/instruments",
    summary="Create or replace an instrument",
    description=(
        "Adding an instrument is a configuration change, not a code change. "
        "The full specification is accepted here and takes effect immediately."
    ),
)
async def upsert_instrument(
    payload: InstrumentUpsertRequest, service: Service, principal: RequireAdmin
) -> dict[str, Any]:
    try:
        spec = InstrumentSpec.model_validate(payload.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc

    existing = service.registry.find(spec.key)
    before = _spec_payload(existing) if existing else None
    service.registry.register(spec)
    service.specs[spec.key] = spec
    service.simulator.add_instrument(spec)
    for venue in service.venues.values():
        engine = getattr(venue, "engine", None)
        if engine is not None and spec.venue == venue.name:
            engine.instruments[spec.key] = spec

    async with service.database.session() as session:
        from ...db.repositories import InstrumentRepository, ObservabilityRepository

        await InstrumentRepository(session).upsert(spec)
        await ObservabilityRepository(session).record_configuration_change(
            actor=principal.username, entity="instrument", entity_id=spec.key,
            before=before, after=_spec_payload(spec),
            note="instrument created or replaced through the API",
        )
    log.info("instrument upserted", extra={"key": spec.key, "actor": principal.username})
    return _spec_payload(spec)


@router.delete("/instruments/{key:path}", summary="Remove an instrument")
async def delete_instrument(
    key: str, service: Service, principal: RequireAdmin
) -> Message:
    spec = service.registry.find(key)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown instrument {key!r}")
    in_use = [
        m.name for m in service.registry.mappings()
        if key in (m.source_key, m.hedge_key)
    ]
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{key} is used by hedge pair(s) {', '.join(in_use)}; remove them first",
        )
    service.registry.remove(key)
    service.specs.pop(key, None)
    async with service.database.session() as session:
        from ...db.repositories import ObservabilityRepository

        await ObservabilityRepository(session).record_configuration_change(
            actor=principal.username, entity="instrument", entity_id=key,
            before=_spec_payload(spec), after=None, note="instrument removed",
        )
    return Message(detail=f"{key} removed")


@router.get("/instrument-mappings", summary="List hedge pairs")
async def list_mappings(service: Service, enabled_only: bool = False) -> dict[str, Any]:
    from ...domain.quantity import describe_conversion

    result: list[dict[str, Any]] = []
    for mapping in service.registry.mappings(enabled_only=enabled_only):
        source = service.registry.get(mapping.source_key)
        hedge = service.registry.get(mapping.hedge_key)
        source_mid = service.simulator.ticker(source.key).mid
        hedge_mid = service.simulator.ticker(hedge.key).mid
        result.append({
            "key": mapping.key,
            "name": mapping.name,
            "source_key": mapping.source_key,
            "hedge_key": mapping.hedge_key,
            "objective": mapping.objective.value,
            "target_ratio": str(mapping.target_ratio),
            "tolerance_bps": str(mapping.tolerance_bps),
            "max_notional": str(mapping.max_notional) if mapping.max_notional else None,
            "enabled": mapping.enabled,
            "source_sizing": source.describe_sizing(),
            "hedge_sizing": hedge.describe_sizing(),
            "conversion": describe_conversion(source, hedge, source_mid, hedge_mid),
        })
    return {"count": len(result), "mappings": result}


@router.post("/instrument-mappings", summary="Create or replace a hedge pair")
async def upsert_mapping(
    payload: MappingUpsertRequest, service: Service, principal: RequireAdmin
) -> dict[str, Any]:
    mapping = HedgeMapping(
        name=payload.name, source_key=payload.source_key, hedge_key=payload.hedge_key,
        objective=payload.objective, target_ratio=payload.target_ratio,
        tolerance_bps=payload.tolerance_bps, max_notional=payload.max_notional,
        enabled=payload.enabled,
    )
    try:
        service.registry.register_mapping(mapping)
    except (InstrumentNotFound, ValueError) as exc:
        raise HTTPException(HTTP_422, str(exc)) from exc

    async with service.database.session() as session:
        from ...db.repositories import ObservabilityRepository

        await ObservabilityRepository(session).record_configuration_change(
            actor=principal.username, entity="hedge_mapping", entity_id=mapping.name,
            before=None, after={"source": mapping.source_key, "hedge": mapping.hedge_key,
                                "objective": mapping.objective.value},
            note="hedge pair configured through the API",
        )
    return {"key": mapping.key, "name": mapping.name, "enabled": mapping.enabled}


@router.get("/hedge-configs", summary="Effective hedge configuration per pair")
async def hedge_configs(service: Service) -> dict[str, Any]:
    return {
        "risk_thresholds": {
            "warning_margin_level": str(service.risk_engine.thresholds.warning_margin_level),
            "danger_margin_level": str(service.risk_engine.thresholds.danger_margin_level),
            "emergency_margin_level": str(service.risk_engine.thresholds.emergency_margin_level),
            "kill_switch_margin_level": str(service.risk_engine.thresholds.kill_switch_margin_level),
            "warning_residual_bps": str(service.risk_engine.thresholds.warning_residual_bps),
            "danger_residual_bps": str(service.risk_engine.thresholds.danger_residual_bps),
            "emergency_residual_bps": str(service.risk_engine.thresholds.emergency_residual_bps),
            "max_daily_loss": str(service.risk_engine.thresholds.max_daily_loss),
            "max_spread_bps": str(service.risk_engine.thresholds.max_spread_bps),
        },
        "portfolio_limits": {
            "max_total_notional": str(service.portfolio_engine.limits.max_total_notional),
            "max_aggregate_margin_utilization":
                str(service.portfolio_engine.limits.max_aggregate_margin_utilization),
            "max_concentration_pct": str(service.portfolio_engine.limits.max_concentration_pct),
            "max_daily_loss": str(service.portfolio_engine.limits.max_daily_loss),
            "max_net_exposure_pct": str(service.portfolio_engine.limits.max_net_exposure_pct),
            "max_currency_exposure": str(service.portfolio_engine.limits.max_currency_exposure),
        },
        "pairs": [
            {
                "name": m.name, "objective": m.objective.value,
                "target_ratio": str(m.target_ratio),
                "tolerance_bps": str(m.tolerance_bps), "enabled": m.enabled,
            }
            for m in service.registry.mappings()
        ],
    }


@router.get("/fx-rates", summary="Current FX conversion rates")
async def fx_rates(service: Service) -> dict[str, Any]:
    return {
        "pivot": service.fx.pivot,
        "account_currency": service.settings.account_currency,
        "rates": [
            {"pair": q.pair, "base": q.base, "quote": q.quote,
             "rate": str(q.rate), "source": q.source,
             "timestamp": q.timestamp.isoformat()}
            for q in service.fx.all_quotes()
        ],
    }
