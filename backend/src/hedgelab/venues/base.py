"""The venue interface.

Every venue -- paper or (hypothetically) live -- implements this protocol.  The
execution engine is written against it and has no idea whether it is talking to
a simulated perpetual exchange or a simulated MT5 broker.

The method set is deliberately the intersection of what a crypto exchange REST
API and an MT5 bridge both expose, so a real adapter would be a thin
translation layer rather than a redesign.
"""

from __future__ import annotations

import abc
from decimal import Decimal

from ..domain.account import AccountSnapshot
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, Ticker
from ..domain.orders import Order, OrderRequest, Position


class VenueError(RuntimeError):
    """Base class for venue-originated failures."""

    def __init__(self, message: str, *, venue: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.venue = venue
        self.retryable = retryable


class VenueDisconnected(VenueError):
    """The venue is unreachable.  Always retryable."""

    def __init__(self, venue: str) -> None:
        super().__init__(f"{venue} is disconnected", venue=venue, retryable=True)


class VenueTimeout(VenueError):
    """A request timed out with an *unknown* outcome.

    This is the dangerous case: the order may or may not have reached the
    venue.  The execution engine must reconcile rather than blindly retry.
    """

    def __init__(self, venue: str, operation: str) -> None:
        super().__init__(f"{venue}: {operation} timed out", venue=venue, retryable=True)
        self.operation = operation


class OrderRejected(VenueError):
    """The venue refused the order.  Not retryable without changing it."""

    def __init__(self, venue: str, reason: str) -> None:
        super().__init__(f"{venue} rejected order: {reason}", venue=venue, retryable=False)
        self.reason = reason


class InsufficientMargin(OrderRejected):
    def __init__(self, venue: str, required: Decimal, available: Decimal) -> None:
        super().__init__(venue, f"insufficient free margin: need {required}, have {available}")
        self.required = required
        self.available = available


class LiveTradingDisabledError(RuntimeError):
    """Raised by every live adapter method.

    This platform is paper-only by construction.  The live classes exist so the
    extension point is visible and typed -- never so an order can be sent.
    """


class TradingVenue(abc.ABC):
    """Abstract venue.  All methods are async to match real network adapters."""

    #: Human-readable venue identifier, matching ``InstrumentSpec.venue``.
    name: str
    #: Currency the venue's balance and margin are denominated in.
    account_currency: str
    #: Always True for the adapters in this repository.
    is_paper: bool = True

    # --- reference data -------------------------------------------------
    @abc.abstractmethod
    async def get_instruments(self) -> list[InstrumentSpec]:
        """Contract specifications for every tradable symbol on this venue."""

    # --- market data ----------------------------------------------------
    @abc.abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """Top of book for ``symbol``."""

    @abc.abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        """Aggregated depth for ``symbol``."""

    # --- account --------------------------------------------------------
    @abc.abstractmethod
    async def get_balance(self) -> AccountSnapshot:
        """Balance, equity, margin and margin level."""

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]:
        """All non-flat positions."""

    @abc.abstractmethod
    async def get_open_orders(self) -> list[Order]:
        """Orders that are not in a terminal state."""

    # --- trading --------------------------------------------------------
    @abc.abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """Submit an order and return its resulting state."""

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """Cancel a working order."""

    @abc.abstractmethod
    async def get_order(self, order_id: str) -> Order:
        """Look up a single order by venue order id."""

    # --- lifecycle ------------------------------------------------------
    async def connect(self) -> None:
        """Establish the session.  Paper adapters have nothing to do."""

    async def disconnect(self) -> None:
        """Tear the session down."""

    async def health(self) -> dict[str, object]:
        """Liveness detail surfaced by ``/system/status``."""
        return {"venue": self.name, "paper": self.is_paper, "connected": True}
