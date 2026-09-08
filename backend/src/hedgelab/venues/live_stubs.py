"""Live adapter extension points -- deliberately NOT implemented.

These classes exist so the shape of a real integration is visible and typed.
**Every** method raises :class:`LiveTradingDisabledError`.  They are not
registered in the venue factory and cannot be constructed through it, so no
code path in this platform can reach a real exchange or broker.

To build a real adapter you would replace the bodies below with an HTTP/ WS
client (Delta) or a MetaTrader5 bridge (MT5), add credential settings, and
register the class in ``venues/factory.py`` behind the ``allow_live`` flag.
That is an intentional, reviewable change -- not an accident.
"""

from __future__ import annotations

from typing import NoReturn

from ..domain.account import AccountSnapshot
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, Ticker
from ..domain.orders import Order, OrderRequest, Position
from .base import LiveTradingDisabledError, TradingVenue


def _refuse(operation: str, venue: str) -> NoReturn:
    raise LiveTradingDisabledError(
        f"{venue}.{operation}() is not implemented: this platform is paper-trading only. "
        f"See PAPER_TRADING.md."
    )


class _LiveAdapterBase(TradingVenue):
    """Common refusal behaviour for every live adapter."""

    is_paper = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabledError(
            f"{type(self).__name__} cannot be constructed: this platform has no live "
            f"trading implementation. See PAPER_TRADING.md."
        )

    async def get_instruments(self) -> list[InstrumentSpec]:
        _refuse("get_instruments", self.name)

    async def get_ticker(self, symbol: str) -> Ticker:
        _refuse("get_ticker", self.name)

    async def get_orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        _refuse("get_orderbook", self.name)

    async def get_balance(self) -> AccountSnapshot:
        _refuse("get_balance", self.name)

    async def get_positions(self) -> list[Position]:
        _refuse("get_positions", self.name)

    async def get_open_orders(self) -> list[Order]:
        _refuse("get_open_orders", self.name)

    async def place_order(self, request: OrderRequest) -> Order:
        _refuse("place_order", self.name)

    async def cancel_order(self, order_id: str) -> Order:
        _refuse("cancel_order", self.name)

    async def get_order(self, order_id: str) -> Order:
        _refuse("get_order", self.name)


class DeltaAdapter(_LiveAdapterBase):
    """Extension point for a real Delta Exchange REST/WebSocket adapter."""

    name = "DELTA"
    account_currency = "USDT"


class MT5Adapter(_LiveAdapterBase):
    """Extension point for a real MetaTrader 5 terminal bridge."""

    name = "MT5"
    account_currency = "USD"
