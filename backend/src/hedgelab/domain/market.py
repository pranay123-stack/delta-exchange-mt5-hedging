"""Market-data value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .numeric import ZERO, safe_div


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Ticker:
    """Top-of-book snapshot for one instrument on one venue."""

    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: Decimal
    timestamp: datetime
    #: Funding rate per interval as a fraction; ``None`` for non-funded venues.
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None
    #: Depth available at top of book, in quantity units.  Drives slippage.
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)
    #: Set by the simulator when a stale-data scenario is active.
    is_stale: bool = False
    sequence: int = 0

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> Decimal:
        return safe_div(self.spread, self.mid) * Decimal(10000)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    def price_for(self, is_buy: bool) -> Decimal:
        """Aggressive price for a taker order on the given side."""
        return self.ask if is_buy else self.bid

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.timestamp

    def is_older_than(self, seconds: float, now: datetime | None = None) -> bool:
        return self.age(now).total_seconds() > seconds


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Aggregated depth, deepest-first from the touch."""

    venue: str
    symbol: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    timestamp: datetime
    sequence: int = 0

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price if self.bids else ZERO

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price if self.asks else ZERO

    @property
    def mid(self) -> Decimal:
        if not self.bids or not self.asks:
            return ZERO
        return (self.best_bid + self.best_ask) / Decimal(2)

    def depth(self, is_buy: bool) -> tuple[OrderBookLevel, ...]:
        return self.asks if is_buy else self.bids

    def total_size(self, is_buy: bool) -> Decimal:
        return sum((lvl.size for lvl in self.depth(is_buy)), ZERO)

    def sweep(self, is_buy: bool, quantity: Decimal) -> tuple[Decimal, Decimal]:
        """Walk the book for ``quantity``.

        Returns ``(filled_quantity, volume_weighted_price)``.  A partially
        filled sweep returns the quantity actually available -- the caller
        decides whether that is acceptable.
        """
        remaining = abs(quantity)
        if remaining == ZERO:
            return ZERO, ZERO
        notional = ZERO
        filled = ZERO
        for level in self.depth(is_buy):
            take = min(remaining, level.size)
            if take <= ZERO:
                continue
            notional += take * level.price
            filled += take
            remaining -= take
            if remaining <= ZERO:
                break
        if filled == ZERO:
            return ZERO, ZERO
        return filled, notional / filled


@dataclass(frozen=True, slots=True)
class FundingEvent:
    venue: str
    symbol: str
    rate: Decimal
    interval_hours: Decimal
    timestamp: datetime


@dataclass(slots=True)
class MarketSnapshot:
    """All market data needed to evaluate one hedge pair at a point in time."""

    tickers: dict[str, Ticker] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    fx: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utcnow)

    def ticker(self, key: str) -> Ticker | None:
        return self.tickers.get(key)

    def require_ticker(self, key: str) -> Ticker:
        ticker = self.tickers.get(key)
        if ticker is None:
            raise KeyError(f"no market data for {key}")
        return ticker

    def book(self, key: str) -> OrderBook | None:
        return self.books.get(key)

    def put(self, ticker: Ticker) -> None:
        self.tickers[ticker.key] = ticker
        self.timestamp = ticker.timestamp
