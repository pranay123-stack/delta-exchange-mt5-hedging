"""PaperMT5Adapter -- simulates an Exness-style MT5 broker.

Characteristics modelled:
  * quantity in **lots**, where one lot is a symbol-specific number of base
    units (1 BTC, 10 ETH, 100 oz of gold, 100 SOL);
  * **no commission** -- the broker is paid through a wider spread, which the
    fill price already reflects;
  * **swap points** charged per lot per night, tripled on the configured
    weekday, with the sign carried by the points themselves;
  * **trading sessions** -- gold closes at the weekend, and orders outside the
    session are rejected;
  * broker **stop-out** at a margin level percentage.
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

DEFAULT_VENUE_NAME = "PAPER_MT5"


class PaperMT5Adapter(TradingVenue):
    """Paper implementation of an MT5 broker bridge."""

    is_paper = True
    venue_kind = VenueKind.MT5_BROKER

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
        stop_out_level: Decimal = Decimal(50),
    ) -> None:
        self.name = name
        self.account_currency = account_currency
        self.stop_out_level = stop_out_level
        self.engine = PaperMatchingEngine(
            venue=name,
            account_currency=account_currency,
            instruments={k: v for k, v in instruments.items() if v.venue == name},
            simulator=simulator,
            fx=fx,
            faults=faults,
            starting_balance=starting_balance,
            config=PaperEngineConfig(
                stop_out_margin_level=stop_out_level,
                base_latency_ms=8,  # broker bridges are slower than an exchange REST API
                allow_partial_fills=True,
                charges_commission=False,  # paid via spread
                enforce_trading_hours=True,
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

    # --- broker-specific -------------------------------------------------
    def apply_swaps(self, hours: Decimal | None = None) -> list[FundingAccrual]:
        """Charge overnight financing (rollover) on all open positions."""
        return self.engine.apply_funding(hours)

    def check_stop_out(self) -> list[str]:
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
            "stop_out_level": str(self.stop_out_level),
        }
