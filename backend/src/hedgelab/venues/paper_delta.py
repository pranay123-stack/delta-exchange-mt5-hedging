"""PaperDeltaAdapter -- simulates a Delta-Exchange-style perpetual venue.

Characteristics modelled:
  * quantity in **contracts**, both linear (USDT-margined) and inverse
    (coin-margined) settlement styles;
  * explicit maker/taker **commission** on top of the spread;
  * **funding** every ``funding_interval_hours``, longs paying shorts on a
    positive rate;
  * isolated linear margin with a liquidation-style stop-out.

It holds no credentials and has no network client.  ``is_paper`` is a class
constant that cannot be turned off.
"""

from __future__ import annotations

from decimal import Decimal

from ..domain.account import AccountSnapshot
from ..domain.enums import VenueKind
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, Ticker
from ..domain.orders import Order, OrderRequest, Position
from ..faults.injector import FaultInjector
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from ..marketdata.simulator import MarketSimulator
from .base import OrderRejected, TradingVenue, VenueDisconnected
from .paper_engine import FundingAccrual, PaperEngineConfig, PaperMatchingEngine

log = get_logger(__name__)

DEFAULT_VENUE_NAME = "PAPER_DELTA"


class PaperDeltaAdapter(TradingVenue):
    """Paper implementation of a perpetual-futures exchange."""

    is_paper = True
    venue_kind = VenueKind.PERPETUAL_EXCHANGE

    def __init__(
        self,
        *,
        instruments: dict[str, InstrumentSpec],
        simulator: MarketSimulator,
        fx: FxService,
        faults: FaultInjector,
        starting_balance: Decimal,
        name: str = DEFAULT_VENUE_NAME,
        account_currency: str = "USD",
    ) -> None:
        self.name = name
        self.account_currency = account_currency
        self.engine = PaperMatchingEngine(
            venue=name,
            account_currency=account_currency,
            instruments={k: v for k, v in instruments.items() if v.venue == name},
            simulator=simulator,
            fx=fx,
            faults=faults,
            starting_balance=starting_balance,
            config=PaperEngineConfig(
                # Exchanges liquidate when equity falls to maintenance margin;
                # expressed as a margin level that is 100% of maintenance.
                stop_out_margin_level=Decimal(100),
                base_latency_ms=2,
                allow_partial_fills=True,
                charges_commission=True,
                enforce_trading_hours=False,  # perpetuals trade 24/7
            ),
        )

    # --- reference data -------------------------------------------------
    async def get_instruments(self) -> list[InstrumentSpec]:
        return sorted(self.engine.instruments.values(), key=lambda s: s.symbol)

    # --- market data ----------------------------------------------------
    async def get_ticker(self, symbol: str) -> Ticker:
        return self.engine.ticker(symbol)

    async def get_orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        return self.engine.orderbook(symbol, depth)

    # --- account --------------------------------------------------------
    async def get_balance(self) -> AccountSnapshot:
        return self.engine.account()

    async def get_positions(self) -> list[Position]:
        if not self.engine.is_connected:
            raise VenueDisconnected(self.name)
        return self.engine.open_positions()

    async def get_open_orders(self) -> list[Order]:
        if not self.engine.is_connected:
            raise VenueDisconnected(self.name)
        return self.engine.open_orders()

    # --- trading --------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        if request.venue != self.name:
            raise OrderRejected(self.name, f"order routed to wrong venue: {request.venue}")
        return await self.engine.place_order(request)

    async def cancel_order(self, order_id: str) -> Order:
        order = await self.get_order(order_id)
        order.cancel()
        return order

    async def get_order(self, order_id: str) -> Order:
        try:
            return self.engine.orders[order_id]
        except KeyError:
            raise OrderRejected(self.name, f"unknown order id {order_id!r}") from None

    # --- perpetual-specific ---------------------------------------------
    def apply_funding(self, hours: Decimal | None = None) -> list[FundingAccrual]:
        """Settle one funding interval across all open positions."""
        return self.engine.apply_funding(hours)

    def check_liquidations(self) -> list[str]:
        return self.engine.check_stop_out()

    async def health(self) -> dict[str, object]:
        snapshot = self.engine.account()
        return {
            "venue": self.name,
            "kind": self.venue_kind.value,
            "paper": True,
            "connected": self.engine.is_connected,
            "instruments": len(self.engine.instruments),
            "open_positions": snapshot.open_positions,
            "margin_level": str(snapshot.margin_level),
        }
