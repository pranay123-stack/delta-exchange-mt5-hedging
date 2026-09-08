"""Order, fill and position value objects.

These are the in-memory representations exchanged between the execution
engine and the venue adapters.  Their persistent counterparts live in
``db/models.py``; keeping them separate lets the calculation layer be tested
with no database at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .enums import Leg, OrderStatus, OrderType, PositionSide, Side, TimeInForce
from .market import utcnow
from .numeric import ZERO, safe_div


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An intent to trade, before any venue has seen it."""

    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.IOC
    client_order_id: str = field(default_factory=lambda: new_id("cli"))
    cycle_id: str | None = None
    leg: Leg | None = None
    correlation_id: str | None = None
    #: Always true in this platform.  Persisted and shown in the UI.
    is_paper: bool = True

    def __post_init__(self) -> None:
        if self.quantity <= ZERO:
            raise ValueError("order quantity must be positive; direction is carried by `side`")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("limit orders require a price")

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.side.sign


@dataclass(slots=True)
class Fill:
    """One execution report."""

    order_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    is_maker: bool
    timestamp: datetime = field(default_factory=utcnow)
    exec_id: str = field(default_factory=lambda: new_id("exec"))
    #: Signed difference between fill price and the reference price at submit.
    slippage: Decimal = ZERO


@dataclass(slots=True)
class Order:
    """Venue-side order state, mutated as execution reports arrive."""

    order_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: Decimal = ZERO
    average_price: Decimal = ZERO
    fees_paid: Decimal = ZERO
    reject_reason: str | None = None
    fills: list[Fill] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    #: Reference mid at submission time; the baseline for slippage.
    reference_price: Decimal = ZERO

    @property
    def remaining_quantity(self) -> Decimal:
        return max(ZERO, self.request.quantity - self.filled_quantity)

    @property
    def is_complete(self) -> bool:
        return self.status.is_terminal

    @property
    def is_partial(self) -> bool:
        """Some quantity traded, but not all of it.

        Read from the *quantities*, not the status: an IOC order that fills
        half is terminal (CANCELLED) yet still partially filled, so a status
        check would miss it.
        """
        return ZERO < self.filled_quantity < self.request.quantity

    @property
    def fill_ratio(self) -> Decimal:
        return safe_div(self.filled_quantity, self.request.quantity)

    @property
    def signed_filled_quantity(self) -> Decimal:
        return self.filled_quantity * self.request.side.sign

    def apply_fill(self, fill: Fill) -> None:
        """Fold an execution report in, keeping the running average price.

        Duplicate ``exec_id`` values are ignored -- venues do resend execution
        reports, and applying one twice would double-count the position.
        """
        if any(existing.exec_id == fill.exec_id for existing in self.fills):
            return
        new_filled = self.filled_quantity + fill.quantity
        if new_filled > ZERO:
            notional = self.average_price * self.filled_quantity + fill.price * fill.quantity
            self.average_price = notional / new_filled
        self.filled_quantity = new_filled
        self.fees_paid += fill.fee
        self.fills.append(fill)
        if self.filled_quantity >= self.request.quantity:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED
        self.updated_at = fill.timestamp

    def reject(self, reason: str) -> None:
        self.status = OrderStatus.REJECTED
        self.reject_reason = reason
        self.updated_at = utcnow()

    def cancel(self) -> None:
        """Cancel the working remainder.

        The status becomes CANCELLED even when part of the order traded --
        which is what exchanges report, and what ``filled_quantity`` is for.
        Calling it FILLED would make a half-filled order indistinguishable from
        a complete one in every downstream query.
        """
        if self.filled_quantity >= self.request.quantity:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.CANCELLED
        self.updated_at = utcnow()


@dataclass(slots=True)
class Position:
    """Net position on one venue in one instrument.

    ``quantity`` is signed: positive is long, negative is short.  A single
    signed field avoids an entire class of bug where side and size disagree.
    """

    venue: str
    symbol: str
    quantity: Decimal = ZERO
    average_entry: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    funding_paid: Decimal = ZERO
    funding_received: Decimal = ZERO
    fees_paid: Decimal = ZERO
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    @property
    def side(self) -> PositionSide:
        if self.quantity > ZERO:
            return PositionSide.LONG
        if self.quantity < ZERO:
            return PositionSide.SHORT
        return PositionSide.FLAT

    @property
    def is_flat(self) -> bool:
        return self.quantity == ZERO

    @property
    def net_funding(self) -> Decimal:
        return self.funding_received - self.funding_paid

    def apply(self, signed_quantity: Decimal, price: Decimal, fee: Decimal = ZERO) -> Decimal:
        """Apply a signed execution, returning realized PnL from this trade.

        Handles the three cases exchanges distinguish: increasing a position
        (average price updates), reducing it (PnL realizes at the old average),
        and flipping through zero (realize the whole old leg, then open a new
        one at the trade price).
        """
        self.fees_paid += fee
        realized = ZERO
        old_qty = self.quantity
        new_qty = old_qty + signed_quantity

        if old_qty == ZERO or (old_qty > ZERO) == (signed_quantity > ZERO):
            # Opening or increasing: weighted-average the entry.
            total = old_qty + signed_quantity
            if total != ZERO:
                self.average_entry = (
                    self.average_entry * old_qty + price * signed_quantity
                ) / total
        else:
            closing = min(abs(signed_quantity), abs(old_qty))
            direction = Decimal(1) if old_qty > ZERO else Decimal(-1)
            realized = (price - self.average_entry) * closing * direction
            self.realized_pnl += realized
            if abs(signed_quantity) > abs(old_qty):
                # Flipped through zero: the remainder opens at the trade price.
                self.average_entry = price
            elif new_qty == ZERO:
                self.average_entry = ZERO

        self.quantity = new_qty
        if self.quantity == ZERO:
            self.average_entry = ZERO
        self.updated_at = utcnow()
        return realized

    def unrealized_pnl_linear(self, mark: Decimal, units_per_quantity: Decimal) -> Decimal:
        """Unrealized PnL in quote currency for a linear instrument."""
        if self.is_flat:
            return ZERO
        return (mark - self.average_entry) * self.quantity * units_per_quantity

    def unrealized_pnl_inverse(self, mark: Decimal, quote_per_quantity: Decimal) -> Decimal:
        """Unrealized PnL in *quote* currency for an inverse instrument.

        ``PnL_base = N * (1/entry - 1/mark)``; multiplying by ``mark`` restates
        it in quote terms so both styles can be summed.
        """
        if self.is_flat or self.average_entry <= ZERO or mark <= ZERO:
            return ZERO
        notional = self.quantity * quote_per_quantity
        pnl_base = notional * (Decimal(1) / self.average_entry - Decimal(1) / mark)
        return pnl_base * mark
