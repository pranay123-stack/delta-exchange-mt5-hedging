"""Shared fixtures.

Tests run against SQLite so the whole suite works with no Docker and no
PostgreSQL.  The schema is identical -- see ``db/base.py`` for the dialect
variants that make that true.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from hedgelab.config import Settings, reset_settings_cache
from hedgelab.db.session import Database
from hedgelab.domain.enums import (
    FundingModel,
    InstrumentType,
    MarginModel,
    QuantityUnit,
    SettlementStyle,
    VenueKind,
)
from hedgelab.domain.instrument import InstrumentSpec
from hedgelab.faults.injector import FaultInjector
from hedgelab.hedge.calculator import HedgeCalculator
from hedgelab.instruments.registry import InstrumentRegistry
from hedgelab.marketdata.fx import FxService
from hedgelab.marketdata.simulator import MarketSimulator
from hedgelab.service import HedgeLabService

SPEC_DIR = Path(__file__).resolve().parents[1] / "src" / "hedgelab" / "instruments" / "specs"


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def registry() -> InstrumentRegistry:
    return InstrumentRegistry.from_directory(SPEC_DIR)


@pytest.fixture
def fx() -> FxService:
    return FxService()


@pytest.fixture
def simulator(registry: InstrumentRegistry) -> MarketSimulator:
    sim = MarketSimulator(registry.all(), seed=424242)
    sim.advance(20)
    return sim


@pytest.fixture
def calculator(fx: FxService) -> HedgeCalculator:
    return HedgeCalculator(fx)


@pytest.fixture
def faults() -> FaultInjector:
    return FaultInjector(seed=1)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        instrument_spec_dir=SPEC_DIR,
        simulator_seed=424242,
        account_currency="USD",
        paper_starting_balance=Decimal("250000"),
    )


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database.from_settings(settings)
    await db.create_all()
    yield db
    await db.dispose()


@pytest_asyncio.fixture
async def service(settings: Settings, database: Database) -> AsyncIterator[HedgeLabService]:
    svc = HedgeLabService(settings, database=database)
    await svc.startup(create_schema=False, seed_reference_data=True, rehydrate=False)
    svc.advance_market(20)
    yield svc
    # The ``database`` fixture owns disposal; disposing twice would error.


# ----------------------------------------------------------------------
# Synthetic instruments, deliberately unlike anything in the YAML catalogue.
# Used to prove the engine is not tuned to the shipped symbols.
# ----------------------------------------------------------------------
def make_perp(
    symbol: str = "TEST-PERP",
    *,
    contract_size: str = "1",
    quantity_step: str = "1",
    min_quantity: str = "1",
    tick_size: str = "0.01",
    inverse: bool = False,
    quote: str = "USDT",
    settlement: str | None = None,
    base: str = "TST",
    underlying: str = "",
    taker_fee_bps: str = "5",
    spread_bps: str = "2",
    funding_rate: str = "0.0001",
    initial_margin_rate: str = "0.02",
    maintenance_margin_rate: str = "0.005",
    venue: str = "PAPER_DELTA",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, venue=venue, venue_kind=VenueKind.PERPETUAL_EXCHANGE,
        instrument_type=InstrumentType.PERPETUAL,
        base_asset=base, quote_asset=quote,
        settlement_asset=settlement or (base if inverse else quote),
        underlying_key=underlying or base,
        quantity_unit=QuantityUnit.CONTRACT,
        settlement_style=SettlementStyle.INVERSE if inverse else SettlementStyle.LINEAR,
        contract_size=Decimal(contract_size), contract_multiplier=Decimal(1),
        tick_size=Decimal(tick_size), min_quantity=Decimal(min_quantity),
        max_quantity=Decimal("100000000"), quantity_step=Decimal(quantity_step),
        price_precision=8, quantity_precision=8,
        max_leverage=Decimal(50),
        initial_margin_rate=Decimal(initial_margin_rate),
        maintenance_margin_rate=Decimal(maintenance_margin_rate),
        maker_fee_bps=Decimal(2), taker_fee_bps=Decimal(taker_fee_bps),
        typical_spread_bps=Decimal(spread_bps),
        funding_model=FundingModel.PERPETUAL_FUNDING,
        funding_interval_hours=Decimal(8),
        baseline_funding_rate=Decimal(funding_rate),
    )


def make_cfd(
    symbol: str = "TESTUSD",
    *,
    units_per_lot: str = "1",
    quantity_step: str = "0.01",
    min_quantity: str = "0.01",
    tick_size: str = "0.01",
    quote: str = "USD",
    base: str = "TST",
    underlying: str = "",
    spread_bps: str = "8",
    swap_long: str = "-100",
    swap_short: str = "-40",
    initial_margin_rate: str = "0.01",
    maintenance_margin_rate: str = "0.005",
    venue: str = "PAPER_MT5",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, venue=venue, venue_kind=VenueKind.MT5_BROKER,
        instrument_type=InstrumentType.CFD,
        base_asset=base, quote_asset=quote, settlement_asset=quote,
        underlying_key=underlying or base,
        quantity_unit=QuantityUnit.LOT, settlement_style=SettlementStyle.LINEAR,
        contract_size=Decimal(units_per_lot), units_per_lot=Decimal(units_per_lot),
        contract_multiplier=Decimal(1),
        tick_size=Decimal(tick_size), min_quantity=Decimal(min_quantity),
        max_quantity=Decimal("1000000"), quantity_step=Decimal(quantity_step),
        price_precision=8, quantity_precision=8,
        max_leverage=Decimal(100),
        initial_margin_rate=Decimal(initial_margin_rate),
        maintenance_margin_rate=Decimal(maintenance_margin_rate),
        maker_fee_bps=Decimal(0), taker_fee_bps=Decimal(0),
        typical_spread_bps=Decimal(spread_bps),
        funding_model=FundingModel.SWAP_POINTS,
        swap_long_points=Decimal(swap_long), swap_short_points=Decimal(swap_short),
        margin_model=MarginModel.BROKER_LEVERAGE,
    )


def approx_dec(actual: Decimal, expected: str | Decimal, tol: str = "1e-9") -> bool:
    """Decimal-safe closeness check.

    ``pytest.approx`` coerces to float and then refuses to subtract a Decimal,
    so comparisons here stay in Decimal throughout.
    """
    return abs(Decimal(actual) - Decimal(expected)) <= Decimal(tol)
