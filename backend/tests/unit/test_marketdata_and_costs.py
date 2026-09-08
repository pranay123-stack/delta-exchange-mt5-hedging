"""Market simulator determinism, scenarios, FX, funding and P&L attribution."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.conftest import make_cfd, make_perp

from hedgelab.costs.funding import project_funding
from hedgelab.costs.pnl import LegAccounting, PnLCalculator
from hedgelab.domain.enums import MarketScenario
from hedgelab.domain.market import MarketSnapshot, OrderBook, OrderBookLevel, Ticker
from hedgelab.domain.numeric import (
    NumericError,
    ceil_to_step,
    dec,
    decimal_places,
    floor_to_step,
    from_bps,
    nearest_step,
    normalize,
    round_to_step,
    safe_div,
)
from hedgelab.domain.orders import Fill, Position
from hedgelab.marketdata.fx import FxRateUnavailable, FxService
from hedgelab.marketdata.simulator import MarketSimulator

D = Decimal


# ======================================================================
# numeric primitives
# ======================================================================
def test_float_input_does_not_leak_binary_error() -> None:
    assert dec(0.1) == D("0.1")
    assert dec(0.1) + dec(0.2) == D("0.3")


def test_bool_is_not_a_number() -> None:
    with pytest.raises(NumericError):
        dec(True)


def test_unparseable_input_is_rejected() -> None:
    with pytest.raises(NumericError):
        dec("not a number")


def test_out_of_range_input_is_rejected() -> None:
    with pytest.raises(NumericError):
        dec("1e30")


@pytest.mark.parametrize(
    ("value", "step", "down", "up", "near"),
    [
        ("1.27", "0.1", "1.2", "1.3", "1.3"),
        ("-1.27", "0.1", "-1.3", "-1.2", "-1.3"),
        ("5", "2", "4", "6", "6"),
    ],
)
def test_step_rounding_directions(value, step, down, up, near) -> None:
    assert floor_to_step(D(value), D(step)) == D(down)
    assert ceil_to_step(D(value), D(step)) == D(up)
    assert nearest_step(D(value), D(step)) == D(near)


def test_round_to_step_toward_zero_is_symmetric() -> None:
    assert round_to_step(D("1.27"), D("0.1")) == D("1.2")
    assert round_to_step(D("-1.27"), D("0.1")) == D("-1.2")


def test_zero_step_is_rejected() -> None:
    with pytest.raises(NumericError):
        round_to_step(D("1"), D("0"))


def test_safe_div_returns_the_default_on_zero() -> None:
    assert safe_div(D("1"), D("0")) == D("0")
    assert safe_div(D("1"), D("0"), D("-1")) == D("-1")


def test_decimal_places_from_step() -> None:
    assert decimal_places(D("0.001")) == 3
    assert decimal_places(D("1")) == 0
    assert decimal_places(D("100")) == 0


def test_normalize_keeps_integers_readable() -> None:
    assert str(normalize(D("50.00"))) == "50"
    assert str(normalize(D("0.500"))) == "0.5"


def test_bps_conversion_round_trips() -> None:
    assert from_bps(D("5")) == D("0.0005")


# ======================================================================
# tickers and books
# ======================================================================
def _ticker(bid: str, ask: str) -> Ticker:
    return Ticker(venue="V", symbol="S", bid=D(bid), ask=D(ask), last=D(bid),
                  volume=D("1"), timestamp=datetime.now(UTC))


def test_ticker_derives_mid_and_spread() -> None:
    ticker = _ticker("99", "101")
    assert ticker.mid == D("100")
    assert ticker.spread == D("2")
    assert ticker.spread_bps == D("200")


def test_ticker_price_for_side() -> None:
    ticker = _ticker("99", "101")
    assert ticker.price_for(is_buy=True) == D("101")
    assert ticker.price_for(is_buy=False) == D("99")


def test_book_sweep_returns_partial_when_depth_runs_out() -> None:
    book = OrderBook(
        venue="V", symbol="S", bids=(),
        asks=(OrderBookLevel(D("100"), D("2")), OrderBookLevel(D("101"), D("3"))),
        timestamp=datetime.now(UTC),
    )
    filled, vwap = book.sweep(True, D("10"))
    assert filled == D("5")
    assert vwap == (D("2") * D("100") + D("3") * D("101")) / D("5")


def test_book_sweep_on_an_empty_side_returns_zero() -> None:
    book = OrderBook(venue="V", symbol="S", bids=(), asks=(), timestamp=datetime.now(UTC))
    assert book.sweep(True, D("1")) == (D("0"), D("0"))


def test_snapshot_requires_known_keys() -> None:
    snapshot = MarketSnapshot()
    with pytest.raises(KeyError):
        snapshot.require_ticker("V:S")


# ======================================================================
# simulator
# ======================================================================
def test_same_seed_produces_identical_paths(registry) -> None:
    a = MarketSimulator(registry.all(), seed=7)
    b = MarketSimulator(registry.all(), seed=7)
    a.advance(150)
    b.advance(150)
    assert a.state_digest() == b.state_digest()


def test_different_seeds_diverge(registry) -> None:
    a = MarketSimulator(registry.all(), seed=7)
    b = MarketSimulator(registry.all(), seed=8)
    a.advance(150)
    b.advance(150)
    assert a.state_digest() != b.state_digest()


def test_replay_from_the_same_seed_is_reproducible_at_any_step(registry) -> None:
    a = MarketSimulator(registry.all(), seed=11)
    a.advance(40)
    checkpoint = a.state_digest()
    b = MarketSimulator(registry.all(), seed=11)
    b.advance(40)
    assert b.state_digest() == checkpoint


def test_legs_on_different_venues_have_a_basis(simulator: MarketSimulator) -> None:
    """Without a basis, cross-venue hedging would look risk-free."""
    perp = simulator.ticker("PAPER_DELTA:BTCUSDT-PERP")
    cfd = simulator.ticker("PAPER_MT5:BTCUSD")
    assert perp.mid != cfd.mid
    drift = abs(perp.mid - cfd.mid) / cfd.mid
    assert drift < D("0.01")     # correlated, but not identical


def test_prices_respect_the_tick_lattice(simulator: MarketSimulator, registry) -> None:
    for spec in registry.all():
        ticker = simulator.ticker(spec.key)
        assert (ticker.bid / spec.tick_size) % 1 == 0
        assert (ticker.ask / spec.tick_size) % 1 == 0
        assert ticker.ask > ticker.bid


def test_spread_widening_scenario_widens_spreads(simulator: MarketSimulator) -> None:
    before = simulator.ticker("PAPER_MT5:BTCUSD").spread_bps
    simulator.set_scenario(MarketScenario.SPREAD_WIDENING)
    after = simulator.ticker("PAPER_MT5:BTCUSD").spread_bps
    assert after > before * 5


def test_stale_scenario_freezes_the_timestamp(simulator: MarketSimulator) -> None:
    simulator.set_scenario(MarketScenario.STALE_DATA)
    first = simulator.ticker("PAPER_MT5:BTCUSD")
    simulator.advance(20)
    second = simulator.ticker("PAPER_MT5:BTCUSD")
    assert first.timestamp == second.timestamp
    assert first.mid == second.mid
    assert second.is_stale is True


def test_disconnect_scenario_is_reported(simulator: MarketSimulator) -> None:
    simulator.set_scenario(MarketScenario.EXCHANGE_DISCONNECT, venue="PAPER_MT5")
    assert simulator.is_disconnected("PAPER_MT5") is True
    assert simulator.is_disconnected("PAPER_DELTA") is False


def test_scenario_can_be_scoped_to_one_venue(simulator: MarketSimulator) -> None:
    simulator.set_scenario(MarketScenario.SPREAD_WIDENING, venue="PAPER_MT5")
    assert simulator.ticker("PAPER_MT5:BTCUSD").spread_bps > D("50")
    assert simulator.ticker("PAPER_DELTA:BTCUSDT-PERP").spread_bps < D("10")


def test_liquidity_reduction_thins_the_book(simulator: MarketSimulator) -> None:
    before = simulator.ticker("PAPER_MT5:BTCUSD").bid_size
    simulator.set_scenario(MarketScenario.LIQUIDITY_REDUCTION)
    after = simulator.ticker("PAPER_MT5:BTCUSD").bid_size
    assert after < before


def test_shock_moves_the_price_immediately(simulator: MarketSimulator) -> None:
    before = simulator.underlying_price("BTC")
    after = simulator.apply_shock("BTC", D("-0.1"))
    assert after == before * D("0.9")


def test_unknown_underlying_shock_is_rejected(simulator: MarketSimulator) -> None:
    with pytest.raises(KeyError):
        simulator.apply_shock("NOTATHING", D("0.1"))


def test_a_brand_new_underlying_gets_a_synthesised_process(simulator, registry) -> None:
    """Adding an instrument the simulator has never heard of must just work."""
    from hedgelab.domain.instrument import InstrumentSpec

    exotic = InstrumentSpec.model_validate({
        "symbol": "COCOA-Z6", "venue": "PAPER_MT5", "venue_kind": "MT5_BROKER",
        "instrument_type": "FUTURE", "base_asset": "COCOA", "quote_asset": "GBP",
        "settlement_asset": "GBP", "quantity_unit": "LOT",
        "contract_size": "10", "units_per_lot": "10", "tick_size": "1",
        "min_quantity": "1", "quantity_step": "1",
        "price_precision": 2, "quantity_precision": 2, "max_leverage": "10",
    })
    simulator.add_instrument(exotic)
    ticker = simulator.ticker(exotic.key)
    assert ticker.ask > ticker.bid > 0


def test_funding_rate_is_published_only_for_funded_instruments(simulator) -> None:
    assert simulator.ticker("PAPER_DELTA:BTCUSDT-PERP").funding_rate is not None
    assert simulator.ticker("PAPER_MT5:BTCUSD").funding_rate is None


def test_funding_rate_precision_is_bounded(simulator) -> None:
    rate = simulator.ticker("PAPER_DELTA:BTCUSDT-PERP").funding_rate
    assert rate is not None
    assert -rate.as_tuple().exponent <= 8


# ======================================================================
# FX
# ======================================================================
def test_fx_identity_is_one(fx: FxService) -> None:
    assert fx.rate("USD", "USD") == D("1")


def test_fx_inverse_is_derived(fx: FxService) -> None:
    forward = fx.rate("USD", "INR")
    assert abs(fx.rate("INR", "USD") * forward - D("1")) < D("1e-12")


def test_fx_triangulates_through_the_pivot(fx: FxService) -> None:
    expected = fx.rate("USDT", "USD") * fx.rate("USD", "INR")
    assert fx.rate("USDT", "INR") == expected


def test_missing_fx_path_raises_on_the_execution_path(fx: FxService) -> None:
    with pytest.raises(FxRateUnavailable):
        fx.rate("ZZZ", "INR")


def test_missing_fx_path_degrades_on_the_presentation_path(fx: FxService) -> None:
    assert fx.try_rate("ZZZ", "INR") == D("1")


def test_negative_fx_rate_is_rejected(fx: FxService) -> None:
    with pytest.raises(ValueError):
        fx.set_rate("USD", "XYZ", D("-1"))


# ======================================================================
# funding projection
# ======================================================================
def test_projection_signs_match_who_pays() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1", swap_long="-100", swap_short="-40")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd,
        source_quantity=D("10"), hedge_quantity=D("-10"),
        source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"),
    )
    assert projection.source_per_day < 0        # long pays a positive rate
    assert projection.hedge_per_day < 0         # short pays swap
    assert projection.net_per_day == projection.source_per_day + projection.hedge_per_day
    assert projection.is_positive_carry is False


def test_short_source_receives_funding() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd,
        source_quantity=D("-10"), hedge_quantity=D("10"),
        source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"),
    )
    assert projection.source_per_day > 0


def test_projection_scales_with_the_horizon() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd, source_quantity=D("10"),
        hedge_quantity=D("-10"), source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"), horizon_days=D("7"),
    )
    assert projection.net_over_horizon == projection.net_per_day * 7


def test_projection_explains_itself() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd, source_quantity=D("10"),
        hedge_quantity=D("-10"), source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"),
    )
    assert "funding" in projection.explanation
    assert "swap" in projection.explanation


# ======================================================================
# P&L attribution
# ======================================================================
def _legs():
    source = make_perp(contract_size="0.001")
    hedge = make_cfd(units_per_lot="1")
    source_leg = LegAccounting(
        spec=source,
        position=Position(venue=source.venue, symbol=source.symbol,
                          quantity=D("5000"), average_entry=D("100000")),
        funding_paid=D("120"),
    )
    hedge_leg = LegAccounting(
        spec=hedge,
        position=Position(venue=hedge.venue, symbol=hedge.symbol,
                          quantity=D("-5"), average_entry=D("100050")),
        swap_financing=D("-90"),
    )
    source_leg.add_fill(Fill(order_id="a", quantity=D("5000"), price=D("100000"),
                             fee=D("250"), is_maker=False, slippage=D("5")))
    hedge_leg.add_fill(Fill(order_id="b", quantity=D("5"), price=D("100050"),
                            fee=D("0"), is_maker=False, slippage=D("-8")))
    return source_leg, hedge_leg


def _tick(venue: str, symbol: str, mid: str) -> Ticker:
    m = D(mid)
    return Ticker(venue=venue, symbol=symbol, bid=m - D("5"), ask=m + D("5"),
                  last=m, volume=D("1"), timestamp=datetime.now(UTC))


def test_components_sum_to_the_net(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.check_consistency() is True
    total = sum(c.amount for c in breakdown.components)
    assert abs(total - breakdown.net_pnl) < D("1e-9")


def test_costs_are_negative_and_separated(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.trading_fees < 0
    assert breakdown.spread_cost < 0
    assert breakdown.total_costs == (
        breakdown.trading_fees + breakdown.spread_cost + breakdown.slippage_cost
    )


def test_net_funding_combines_both_legs(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.net_funding == D("-120") + D("-90")


def test_fx_impact_is_separated_from_price_pnl(fx: FxService) -> None:
    """The USDT leg's conversion effect is its own line, not hidden in price."""
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.fx_conversion_impact != 0
    assert any(c.name == "fx_conversion" for c in breakdown.components)


def test_break_even_cost_is_what_the_book_must_earn(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.break_even_cost > 0
    assert breakdown.to_dict()["consistent"] is True


def test_every_component_carries_an_explanation(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert all(c.explanation for c in breakdown.components)
