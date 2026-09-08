"""Paper venue adapters: order lifecycle, margin, funding, faults and safety."""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedgelab.config import Settings
from hedgelab.domain.enums import (
    FaultKind,
    Leg,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    TradingMode,
)
from hedgelab.domain.orders import Fill, Order, OrderRequest, Position
from hedgelab.faults.injector import FaultInjector
from hedgelab.marketdata.fx import FxService
from hedgelab.marketdata.simulator import MarketSimulator
from hedgelab.venues.base import (
    InsufficientMargin,
    LiveTradingDisabledError,
    OrderRejected,
    VenueDisconnected,
    VenueTimeout,
)
from hedgelab.venues.factory import LIVE_ADAPTERS, PAPER_ADAPTERS, VenueFactory
from hedgelab.venues.live_stubs import DeltaAdapter, MT5Adapter

D = Decimal


@pytest.fixture
def venues(registry, simulator: MarketSimulator, fx: FxService, faults: FaultInjector,
           settings: Settings):
    specs = {s.key: s for s in registry.all()}
    factory = VenueFactory(settings=settings, instruments=specs, simulator=simulator,
                           fx=fx, faults=faults)
    return factory.create_all()


def order(venue: str, symbol: str, side: Side, qty: str, **kwargs) -> OrderRequest:
    return OrderRequest(venue=venue, symbol=symbol, side=side, quantity=D(qty), **kwargs)


# ======================================================================
# safety: no live path exists
# ======================================================================
def test_only_paper_adapters_are_registered() -> None:
    assert LIVE_ADAPTERS == {}
    assert set(PAPER_ADAPTERS) == {"PAPER_DELTA", "PAPER_MT5"}
    assert all(cls.is_paper for cls in PAPER_ADAPTERS.values())


def test_factory_refuses_live_mode(registry, simulator, fx, faults, settings) -> None:
    live_settings = settings.model_copy(update={"trading_mode": TradingMode.LIVE})
    factory = VenueFactory(
        settings=live_settings, instruments={s.key: s for s in registry.all()},
        simulator=simulator, fx=fx, faults=faults,
    )
    with pytest.raises(LiveTradingDisabledError, match="paper-only"):
        factory.create("PAPER_DELTA")


def test_factory_refuses_live_even_with_allow_live_set(
    registry, simulator, fx, faults, settings
) -> None:
    """The flag alone is not enough: there is nothing registered to build."""
    live_settings = settings.model_copy(
        update={"trading_mode": TradingMode.LIVE, "allow_live": True}
    )
    factory = VenueFactory(
        settings=live_settings, instruments={s.key: s for s in registry.all()},
        simulator=simulator, fx=fx, faults=faults,
    )
    with pytest.raises(LiveTradingDisabledError):
        factory.create("PAPER_MT5")


@pytest.mark.parametrize("adapter", [DeltaAdapter, MT5Adapter])
def test_live_adapters_cannot_be_constructed(adapter: type) -> None:
    with pytest.raises(LiveTradingDisabledError, match=r"paper-trading only|no live trading"):
        adapter()


def test_every_paper_order_is_marked_paper(venues) -> None:
    request = order("PAPER_DELTA", "BTCUSDT-PERP", Side.BUY, "10")
    assert request.is_paper is True


def test_unknown_venue_is_rejected(registry, simulator, fx, faults, settings) -> None:
    factory = VenueFactory(
        settings=settings, instruments={s.key: s for s in registry.all()},
        simulator=simulator, fx=fx, faults=faults,
    )
    with pytest.raises(KeyError, match="no paper adapter"):
        factory.create("BINANCE")


# ======================================================================
# order lifecycle
# ======================================================================
async def test_market_order_fills_and_moves_the_position(venues) -> None:
    delta = venues["PAPER_DELTA"]
    result = await delta.place_order(order("PAPER_DELTA", "BTCUSDT-PERP", Side.BUY, "1000"))
    assert result.status is OrderStatus.FILLED
    assert result.filled_quantity == D("1000")
    assert result.average_price > 0

    positions = await delta.get_positions()
    assert positions[0].quantity == D("1000")


async def test_selling_reduces_and_realises(venues) -> None:
    delta = venues["PAPER_DELTA"]
    await delta.place_order(order("PAPER_DELTA", "BTCUSDT-PERP", Side.BUY, "1000"))
    before = (await delta.get_balance()).balance
    await delta.place_order(order("PAPER_DELTA", "BTCUSDT-PERP", Side.SELL, "1000"))
    positions = await delta.get_positions()
    assert positions == []                       # flat positions are filtered out
    assert (await delta.get_balance()).balance != before


async def test_perp_venue_charges_commission_but_mt5_does_not(venues) -> None:
    perp = await venues["PAPER_DELTA"].place_order(
        order("PAPER_DELTA", "BTCUSDT-PERP", Side.BUY, "1000")
    )
    cfd = await venues["PAPER_MT5"].place_order(
        order("PAPER_MT5", "BTCUSD", Side.BUY, "1")
    )
    assert perp.fees_paid > 0
    assert cfd.fees_paid == 0     # the broker is paid through the spread


async def test_order_to_the_wrong_venue_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="wrong venue"):
        await venues["PAPER_DELTA"].place_order(
            order("PAPER_MT5", "BTCUSD", Side.BUY, "1")
        )


async def test_unknown_symbol_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="unknown symbol"):
        await venues["PAPER_DELTA"].place_order(
            order("PAPER_DELTA", "DOGE-PERP", Side.BUY, "1")
        )


async def test_off_step_quantity_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="not a multiple of step"):
        await venues["PAPER_MT5"].place_order(
            order("PAPER_MT5", "BTCUSD", Side.BUY, "0.015")
        )


async def test_below_minimum_quantity_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="below minimum"):
        await venues["PAPER_MT5"].place_order(
            OrderRequest(venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY,
                         quantity=D("0.001"))
        )


async def test_above_maximum_quantity_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="above maximum"):
        await venues["PAPER_MT5"].place_order(
            order("PAPER_MT5", "BTCUSD", Side.BUY, "999")
        )


async def test_off_tick_limit_price_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="not a multiple of tick"):
        await venues["PAPER_DELTA"].place_order(
            OrderRequest(venue="PAPER_DELTA", symbol="BTCUSDT-PERP", side=Side.BUY,
                         quantity=D("10"), order_type=OrderType.LIMIT,
                         price=D("100000.13")),
        )


async def test_zero_quantity_order_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        OrderRequest(venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY, quantity=D("0"))


async def test_limit_order_requires_a_price() -> None:
    with pytest.raises(ValueError, match="require a price"):
        OrderRequest(venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY,
                     quantity=D("1"), order_type=OrderType.LIMIT)


async def test_unmarketable_limit_order_rests_unfilled(venues) -> None:
    mt5 = venues["PAPER_MT5"]
    ticker = await mt5.get_ticker("BTCUSD")
    far_below = (ticker.bid * D("0.5")).quantize(D("0.01"))
    result = await mt5.place_order(OrderRequest(
        venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY, quantity=D("1"),
        order_type=OrderType.LIMIT, price=far_below,
    ))
    assert result.filled_quantity == D("0")
    assert result.status is OrderStatus.SUBMITTED
    assert result in await mt5.get_open_orders()


async def test_insufficient_margin_is_rejected(registry, simulator, fx, faults, settings) -> None:
    poor = settings.model_copy(update={"paper_starting_balance": D("500")})
    factory = VenueFactory(
        settings=poor, instruments={s.key: s for s in registry.all()},
        simulator=simulator, fx=fx, faults=faults,
    )
    mt5 = factory.create("PAPER_MT5")
    with pytest.raises(InsufficientMargin):
        await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "5"))


async def test_closing_orders_are_never_blocked_by_margin(
    registry, simulator, fx, faults, settings
) -> None:
    """De-risking must always be possible, even at the margin limit."""
    tight = settings.model_copy(update={"paper_starting_balance": D("1500")})
    factory = VenueFactory(
        settings=tight, instruments={s.key: s for s in registry.all()},
        simulator=simulator, fx=fx, faults=faults,
    )
    mt5 = factory.create("PAPER_MT5")
    await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))
    # Free margin is now nearly gone, but the closing order still goes through.
    closing = await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.SELL, "1"))
    assert closing.status is OrderStatus.FILLED


async def test_trading_hours_are_enforced_on_the_broker(venues, simulator) -> None:
    """Gold is closed at the weekend; the perp venue is not."""
    from datetime import UTC, datetime

    simulator._clock = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)  # Saturday
    with pytest.raises(OrderRejected, match="outside its trading session"):
        await venues["PAPER_MT5"].place_order(order("PAPER_MT5", "XAUUSD", Side.BUY, "1"))
    # The perpetual venue trades 24/7.
    result = await venues["PAPER_DELTA"].place_order(
        order("PAPER_DELTA", "BTCUSDT-PERP", Side.BUY, "10")
    )
    assert result.status is OrderStatus.FILLED


# ======================================================================
# fills, duplicates and slippage
# ======================================================================
def test_duplicate_execution_reports_do_not_double_count() -> None:
    request = OrderRequest(venue="V", symbol="S", side=Side.BUY, quantity=D("10"))
    order_obj = Order(order_id="o1", request=request)
    fill = Fill(order_id="o1", quantity=D("4"), price=D("100"), fee=D("1"), is_maker=False)
    order_obj.apply_fill(fill)
    order_obj.apply_fill(fill)          # replayed report
    assert order_obj.filled_quantity == D("4")
    assert order_obj.fees_paid == D("1")
    assert len(order_obj.fills) == 1


def test_average_price_is_quantity_weighted() -> None:
    request = OrderRequest(venue="V", symbol="S", side=Side.BUY, quantity=D("10"))
    order_obj = Order(order_id="o1", request=request)
    order_obj.apply_fill(Fill(order_id="o1", quantity=D("2"), price=D("100"),
                              fee=D("0"), is_maker=False))
    order_obj.apply_fill(Fill(order_id="o1", quantity=D("8"), price=D("110"),
                              fee=D("0"), is_maker=False))
    assert order_obj.average_price == D("108")
    assert order_obj.status is OrderStatus.FILLED


async def test_slippage_is_recorded_against_the_reference_price(venues) -> None:
    result = await venues["PAPER_MT5"].place_order(
        order("PAPER_MT5", "BTCUSD", Side.BUY, "1")
    )
    assert result.fills[0].slippage != 0     # crossing the spread costs something
    assert result.fills[0].slippage > 0      # a buy fills above mid


async def test_large_orders_walk_the_book(venues, simulator) -> None:
    """Slippage emerges from depth, it is not a constant."""
    mt5 = venues["PAPER_MT5"]
    small = await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "0.1"))
    book = await mt5.get_orderbook("BTCUSD", 10)
    # Sweep past the touch.  This is a pure book operation, so the venue's
    # max_quantity does not apply -- the point is that price worsens with size.
    big_qty = book.asks[0].size + book.asks[1].size / D("2")
    filled, vwap = book.sweep(True, big_qty)
    assert filled == big_qty
    assert vwap > book.best_ask
    assert small.average_price <= vwap

    # A single order that large is refused by the venue's own size limit.
    with pytest.raises(OrderRejected, match="above maximum"):
        await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, str(big_qty)))


# ======================================================================
# position arithmetic
# ======================================================================
def test_position_flip_realises_the_whole_old_leg() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("10"), D("100"))
    realized = position.apply(D("-15"), D("110"))
    assert realized == D("100")            # 10 units x 10 profit
    assert position.quantity == D("-5")
    assert position.average_entry == D("110")


def test_position_average_updates_on_increase() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("10"), D("100"))
    position.apply(D("10"), D("120"))
    assert position.average_entry == D("110")


def test_position_close_resets_the_average() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("10"), D("100"))
    position.apply(D("-10"), D("110"))
    assert position.is_flat
    assert position.average_entry == D("0")


def test_short_position_average_is_correct() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("-2"), D("100"))
    position.apply(D("-3"), D("110"))
    assert position.average_entry == D("106")


def test_inverse_unrealised_pnl_uses_the_reciprocal_formula() -> None:
    position = Position(venue="V", symbol="S", quantity=D("100000"),
                        average_entry=D("100000"))
    pnl = position.unrealized_pnl_inverse(D("125000"), D("1"))
    # PnL_base = 100000 * (1/100000 - 1/125000) = 0.2 BTC; x 125000 = 25000 USD
    assert pnl == D("25000")


# ======================================================================
# funding and swaps
# ======================================================================
async def test_perp_funding_charges_longs_on_a_positive_rate(venues, simulator) -> None:
    delta = venues["PAPER_DELTA"]
    await delta.place_order(order("PAPER_DELTA", "BTCUSDT-PERP", Side.BUY, "1000"))
    simulator.set_funding_rate("PAPER_DELTA:BTCUSDT-PERP", D("0.0001"))
    accruals = delta.apply_funding()
    assert accruals and accruals[0].amount < 0
    assert accruals[0].kind == "FUNDING"


async def test_perp_funding_pays_shorts_on_a_positive_rate(venues, simulator) -> None:
    delta = venues["PAPER_DELTA"]
    await delta.place_order(order("PAPER_DELTA", "BTCUSDT-PERP", Side.SELL, "1000"))
    simulator.set_funding_rate("PAPER_DELTA:BTCUSDT-PERP", D("0.0001"))
    accruals = delta.apply_funding()
    assert accruals[0].amount > 0


async def test_swap_uses_the_point_value_of_the_symbol(venues) -> None:
    """The same point figure is very different money on XAUUSD and BTCUSD."""
    mt5 = venues["PAPER_MT5"]
    await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))
    accruals = mt5.apply_swaps()
    btc = next(a for a in accruals if a.symbol == "BTCUSD")
    spec = mt5.engine.spec("BTCUSD")
    expected = spec.swap_long_points * spec.tick_size * spec.units_per_quantity
    assert btc.amount == expected
    assert btc.kind == "SWAP"


async def test_short_swap_uses_the_short_points(venues) -> None:
    mt5 = venues["PAPER_MT5"]
    await mt5.place_order(order("PAPER_MT5", "XAUUSD", Side.SELL, "1"))
    accrual = next(a for a in mt5.apply_swaps() if a.symbol == "XAUUSD")
    # XAUUSD short swap is configured positive: shorts earn carry.
    assert accrual.amount > 0


# ======================================================================
# connectivity and faults
# ======================================================================
async def test_disconnect_blocks_every_operation(venues) -> None:
    mt5 = venues["PAPER_MT5"]
    mt5.engine.force_disconnect(True)
    with pytest.raises(VenueDisconnected):
        await mt5.get_ticker("BTCUSD")
    with pytest.raises(VenueDisconnected):
        await mt5.get_positions()
    with pytest.raises(VenueDisconnected):
        await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))


async def test_reconnect_restores_service(venues) -> None:
    mt5 = venues["PAPER_MT5"]
    mt5.engine.force_disconnect(True)
    mt5.engine.force_disconnect(False)
    assert (await mt5.get_ticker("BTCUSD")).mid > 0


async def test_injected_rejection_fires_once(venues, faults) -> None:
    faults.arm(FaultKind.ORDER_REJECTION, venue="PAPER_MT5", count=1)
    with pytest.raises(OrderRejected, match="injected rejection"):
        await venues["PAPER_MT5"].place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))
    ok = await venues["PAPER_MT5"].place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))
    assert ok.status is OrderStatus.FILLED


async def test_injected_timeout_raises_venue_timeout(venues, faults) -> None:
    faults.arm(FaultKind.API_TIMEOUT, venue="PAPER_MT5")
    with pytest.raises(VenueTimeout):
        await venues["PAPER_MT5"].place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))


async def test_injected_partial_fill_caps_the_quantity(venues, faults) -> None:
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    result = await venues["PAPER_MT5"].place_order(
        order("PAPER_MT5", "BTCUSD", Side.BUY, "4", leg=Leg.HEDGE)
    )
    assert result.filled_quantity == D("1")
    assert result.is_partial is True
    # The default time in force is IOC, so the unfilled remainder is cancelled
    # and the order is terminal -- it must not linger as a working order.
    assert result.status is OrderStatus.CANCELLED
    assert result.is_complete is True
    assert result not in await venues["PAPER_MT5"].get_open_orders()


async def test_injected_high_slippage_worsens_the_fill(venues, faults) -> None:
    clean = await venues["PAPER_MT5"].place_order(
        order("PAPER_MT5", "BTCUSD", Side.BUY, "0.1")
    )
    faults.arm(FaultKind.HIGH_SLIPPAGE, magnitude="0.01")
    slipped = await venues["PAPER_MT5"].place_order(
        order("PAPER_MT5", "BTCUSD", Side.BUY, "0.1")
    )
    assert slipped.average_price > clean.average_price * D("1.005")


async def test_duplicate_execution_report_fault_does_not_move_the_position(
    venues, faults
) -> None:
    faults.arm(FaultKind.DUPLICATE_EXECUTION_REPORT, venue="PAPER_MT5")
    result = await venues["PAPER_MT5"].place_order(
        order("PAPER_MT5", "BTCUSD", Side.BUY, "1")
    )
    assert result.filled_quantity == D("1")
    positions = await venues["PAPER_MT5"].get_positions()
    assert positions[0].quantity == D("1")


async def test_unexpected_position_appears_only_on_the_venue(venues) -> None:
    engine = venues["PAPER_MT5"].engine
    engine.inject_unexpected_position("XAUUSD", D("0.5"), D("2400"))
    positions = await venues["PAPER_MT5"].get_positions()
    assert any(p.symbol == "XAUUSD" and p.quantity == D("0.5") for p in positions)


# ======================================================================
# stop-out
# ======================================================================
async def test_stop_out_force_closes_when_the_margin_level_collapses(
    registry, simulator, fx, faults, settings
) -> None:
    tight = settings.model_copy(update={"paper_starting_balance": D("1200")})
    factory = VenueFactory(
        settings=tight, instruments={s.key: s for s in registry.all()},
        simulator=simulator, fx=fx, faults=faults,
    )
    mt5 = factory.create("PAPER_MT5")
    await mt5.place_order(order("PAPER_MT5", "BTCUSD", Side.BUY, "1"))
    simulator.apply_shock("BTC", D("-0.20"))
    closed = mt5.check_stop_out()
    assert closed
    assert await mt5.get_positions() == []


async def test_fill_or_kill_rejects_a_partial(venues, faults) -> None:
    """FOK is all-or-nothing: a partial availability must reject, not fill."""
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    with pytest.raises(OrderRejected, match="fill-or-kill"):
        await venues["PAPER_MT5"].place_order(OrderRequest(
            venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY, quantity=D("4"),
            leg=Leg.HEDGE, time_in_force=TimeInForce.FOK,
        ))


async def test_gtc_partial_stays_working(venues, faults) -> None:
    """A good-till-cancelled order keeps its remainder on the book."""
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    result = await venues["PAPER_MT5"].place_order(OrderRequest(
        venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY, quantity=D("4"),
        leg=Leg.HEDGE, time_in_force=TimeInForce.GTC,
    ))
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert result.is_complete is False
    assert result in await venues["PAPER_MT5"].get_open_orders()


async def test_cancelling_a_partial_keeps_the_filled_quantity(venues, faults) -> None:
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    result = await venues["PAPER_MT5"].place_order(OrderRequest(
        venue="PAPER_MT5", symbol="BTCUSD", side=Side.BUY, quantity=D("4"),
        leg=Leg.HEDGE, time_in_force=TimeInForce.GTC,
    ))
    cancelled = await venues["PAPER_MT5"].cancel_order(result.order_id)
    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.filled_quantity == D("1")   # the fill is not forgotten
