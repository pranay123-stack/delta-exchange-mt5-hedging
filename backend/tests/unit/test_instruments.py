"""Instrument specification validation and the generic-instrument guarantee."""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.conftest import make_cfd, make_perp

from hedgelab.domain.instrument import InstrumentSpec, TradingHours, TradingSession
from hedgelab.instruments.registry import InstrumentNotFound, InstrumentRegistry

D = Decimal


def test_units_per_quantity_reflects_the_venue_unit() -> None:
    assert make_perp(contract_size="0.001").units_per_quantity == D("0.001")
    assert make_cfd(units_per_lot="100").units_per_quantity == D("100")


def test_contract_multiplier_is_applied() -> None:
    spec = make_perp(contract_size="0.5")
    spec = spec.model_copy(update={"contract_multiplier": D("4")})
    assert spec.units_per_quantity == D("2")


def test_explicit_units_override_size_times_multiplier() -> None:
    spec = make_perp(contract_size="0.5").model_copy(
        update={"contract_multiplier": D("4"), "units_per_contract": D("7")}
    )
    assert spec.units_per_quantity == D("7")


def test_margin_rates_default_from_leverage() -> None:
    spec = make_perp().model_copy(
        update={"initial_margin_rate": None, "maintenance_margin_rate": None,
                "max_leverage": D("25")}
    )
    assert spec.effective_initial_margin_rate == D("0.04")
    assert spec.effective_maintenance_margin_rate == D("0.02")


def test_fee_currency_defaults_to_settlement_asset() -> None:
    assert make_perp(quote="USDT").effective_fee_currency == "USDT"


def test_underlying_key_defaults_to_base_asset() -> None:
    spec = make_perp(base="SOL").model_copy(update={"underlying_key": ""})
    assert spec.effective_underlying_key == "SOL"


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------
def test_minimum_must_sit_on_the_step_lattice() -> None:
    with pytest.raises(ValidationError, match="not a multiple of"):
        make_cfd(quantity_step="0.03", min_quantity="0.05")


def test_maximum_below_minimum_is_rejected() -> None:
    with pytest.raises(ValidationError, match="max_quantity"):
        make_cfd().model_copy(update={"min_quantity": D("10")}).model_validate(
            {**make_cfd().model_dump(), "min_quantity": "10", "max_quantity": "1"}
        )


def test_step_finer_than_precision_is_rejected() -> None:
    with pytest.raises(ValidationError, match="quantity_precision"):
        InstrumentSpec.model_validate(
            {**make_cfd().model_dump(), "quantity_step": "0.001", "quantity_precision": 2}
        )


def test_tick_finer_than_price_precision_is_rejected() -> None:
    with pytest.raises(ValidationError, match="price_precision"):
        InstrumentSpec.model_validate(
            {**make_cfd().model_dump(), "tick_size": "0.001", "price_precision": 2}
        )


def test_inverse_instruments_cannot_be_quoted_in_lots() -> None:
    with pytest.raises(ValidationError, match="not modelled in lots"):
        InstrumentSpec.model_validate(
            {**make_cfd().model_dump(), "settlement_style": "INVERSE"}
        )


def test_instrument_allowing_neither_direction_is_rejected() -> None:
    with pytest.raises(ValidationError, match="neither direction"):
        InstrumentSpec.model_validate(
            {**make_perp().model_dump(), "allow_long": False, "allow_short": False}
        )


def test_maintenance_margin_above_initial_is_rejected() -> None:
    with pytest.raises(ValidationError, match="maintenance margin"):
        InstrumentSpec.model_validate(
            {**make_perp().model_dump(),
             "initial_margin_rate": "0.01", "maintenance_margin_rate": "0.02"}
        )


def test_empty_symbol_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InstrumentSpec.model_validate({**make_perp().model_dump(), "symbol": "  "})


def test_spec_is_immutable() -> None:
    spec = make_perp()
    with pytest.raises(ValidationError):
        spec.symbol = "OTHER"  # type: ignore[misc]


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InstrumentSpec.model_validate({**make_perp().model_dump(), "leverage_typo": 5})


# ----------------------------------------------------------------------
# trading hours
# ----------------------------------------------------------------------
def test_always_open_instrument_is_always_open() -> None:
    spec = make_perp()
    assert spec.trading_hours.is_open(5, time(3, 0)) is True


def test_session_bounds_are_respected() -> None:
    hours = TradingHours(
        always_open=False,
        sessions=(TradingSession(days=(0, 1, 2, 3, 4), start=time(8), end=time(17)),),
    )
    assert hours.is_open(0, time(12)) is True
    assert hours.is_open(0, time(18)) is False
    assert hours.is_open(5, time(12)) is False


def test_session_wrapping_midnight_works() -> None:
    hours = TradingHours(
        always_open=False,
        sessions=(TradingSession(days=(6,), start=time(22), end=time(6)),),
    )
    assert hours.is_open(6, time(23)) is True
    assert hours.is_open(6, time(3)) is True
    assert hours.is_open(6, time(12)) is False


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------
def test_registry_loads_the_shipped_catalogue(registry: InstrumentRegistry) -> None:
    assert len(registry) >= 9
    assert registry.venues() == ["PAPER_DELTA", "PAPER_MT5"]
    assert len(registry.mappings()) >= 5


def test_registry_rejects_unknown_keys(registry: InstrumentRegistry) -> None:
    with pytest.raises(InstrumentNotFound):
        registry.get("NOPE:NOPE")


def test_registry_finds_hedge_candidates_by_underlying(registry: InstrumentRegistry) -> None:
    candidates = registry.hedge_candidates("PAPER_DELTA:PAXGUSDT-PERP")
    # PAXG is spelled differently from XAU but shares an underlying_key.
    assert any(c.symbol == "XAUUSD" for c in candidates)
    assert all(c.venue == "PAPER_MT5" for c in candidates)


def test_registry_refuses_a_same_venue_mapping(registry: InstrumentRegistry) -> None:
    from hedgelab.instruments.registry import HedgeMapping

    with pytest.raises(ValueError, match="same venue"):
        registry.register_mapping(HedgeMapping(
            name="bad", source_key="PAPER_DELTA:BTCUSDT-PERP",
            hedge_key="PAPER_DELTA:ETHUSDT-PERP",
        ))


def test_registry_refuses_two_enabled_mappings_sharing_a_leg(
    registry: InstrumentRegistry,
) -> None:
    """One venue position cannot belong to two pairs."""
    from hedgelab.instruments.registry import HedgeMapping

    with pytest.raises(ValueError, match="shares instrument"):
        registry.register_mapping(HedgeMapping(
            name="duplicate hedge leg",
            source_key="PAPER_DELTA:SOLUSDT-PERP",
            hedge_key="PAPER_MT5:BTCUSD",
        ))


def test_registry_allows_a_disabled_mapping_to_share_a_leg(
    registry: InstrumentRegistry,
) -> None:
    from hedgelab.instruments.registry import HedgeMapping

    mapping = registry.register_mapping(HedgeMapping(
        name="spare (disabled)", source_key="PAPER_DELTA:SOLUSDT-PERP",
        hedge_key="PAPER_MT5:BTCUSD", enabled=False,
    ))
    assert mapping.enabled is False


def test_registering_a_brand_new_instrument_needs_no_code_change(
    registry: InstrumentRegistry,
) -> None:
    """The generic-instrument guarantee, at the registry level."""
    exotic = InstrumentSpec.model_validate({
        "symbol": "WHEAT-Z6", "venue": "PAPER_MT5", "venue_kind": "MT5_BROKER",
        "instrument_type": "FUTURE", "base_asset": "WHEAT", "quote_asset": "EUR",
        "settlement_asset": "EUR", "quantity_unit": "LOT",
        "contract_size": "5000", "units_per_lot": "5000",
        "tick_size": "0.25", "min_quantity": "0.5", "quantity_step": "0.5",
        "price_precision": 2, "quantity_precision": 2, "max_leverage": "15",
    })
    registry.register(exotic)
    assert registry.get("PAPER_MT5:WHEAT-Z6").units_per_quantity == D("5000")
    assert exotic.describe_sizing() == "1 lot = 5000 WHEAT"
