"""Shared paper matching engine.

Both paper adapters delegate here.  The engine is deliberately *not* a toy: it
walks real simulated depth, so slippage emerges from order size versus
liquidity rather than being a hardcoded constant, and it applies the same
margin arithmetic a venue would before accepting an order.

Nothing in this file can reach a network.  It is the only place a paper fill
is created, so "no order can escape to a real venue" is a one-file audit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..domain.account import AccountSnapshot
from ..domain.enums import (
    FaultKind,
    FundingModel,
    OrderStatus,
    OrderType,
    SettlementStyle,
    TimeInForce,
)
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, Ticker
from ..domain.numeric import ZERO, dec, from_bps, quantize
from ..domain.orders import Fill, Order, OrderRequest, Position, new_id
from ..domain.quantity import QuantityConverter
from ..faults.injector import FaultInjector
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from ..marketdata.simulator import MarketSimulator
from .base import (
    InsufficientMargin,
    OrderRejected,
    VenueDisconnected,
    VenueTimeout,
)

log = get_logger(__name__)


@dataclass
class FundingAccrual:
    """One applied funding or swap charge."""

    venue: str
    symbol: str
    rate: Decimal
    amount: Decimal
    currency: str
    timestamp: datetime
    kind: str  # FUNDING | SWAP

    def to_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "rate": str(self.rate),
            "amount": str(self.amount),
            "currency": self.currency,
            "kind": self.kind,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class PaperEngineConfig:
    """Behavioural knobs the two venue families differ on."""

    #: Fraction of equity below which the venue force-liquidates (stop-out).
    stop_out_margin_level: Decimal = Decimal(50)
    #: Extra latency floor, on top of the active scenario's latency.
    base_latency_ms: int = 2
    #: When True, orders that cannot fill in full at the touch fill partially
    #: rather than being rejected (IOC semantics).
    allow_partial_fills: bool = True
    #: Charge commission (perp venues) vs earn the spread only (MT5 brokers).
    charges_commission: bool = True
    #: Reject orders outside the instrument's trading session.
    enforce_trading_hours: bool = True


class PaperMatchingEngine:
    """Order lifecycle, position keeping and margin for one paper venue."""

    def __init__(
        self,
        *,
        venue: str,
        account_currency: str,
        instruments: dict[str, InstrumentSpec],
        simulator: MarketSimulator,
        fx: FxService,
        faults: FaultInjector,
        starting_balance: Decimal,
        config: PaperEngineConfig | None = None,
    ) -> None:
        self.venue = venue
        self.account_currency = account_currency
        self.instruments = instruments
        self.simulator = simulator
        self.fx = fx
        self.faults = faults
        self.config = config or PaperEngineConfig()
        self.balance = dec(starting_balance)
        self.starting_balance = self.balance
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.funding_history: list[FundingAccrual] = []
        self.total_fees = ZERO
        self.total_funding = ZERO
        self._forced_disconnect = False
        self._seen_exec_ids: set[str] = set()

    # ------------------------------------------------------------------
    # connectivity
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return not self._forced_disconnect and not self.simulator.is_disconnected(self.venue)

    def force_disconnect(self, disconnected: bool = True) -> None:
        self._forced_disconnect = disconnected
        log.warning(
            "paper venue connectivity changed",
            extra={"venue": self.venue, "connected": not disconnected},
        )

    def _require_connection(self) -> None:
        if not self.is_connected:
            raise VenueDisconnected(self.venue)

    async def _simulate_latency(self) -> None:
        profile = self.simulator.profile_for_venue(self.venue)
        delay_ms = self.config.base_latency_ms + profile.latency_ms
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    # ------------------------------------------------------------------
    # reference data / market data
    # ------------------------------------------------------------------
    def spec(self, symbol: str) -> InstrumentSpec:
        key = f"{self.venue}:{symbol}"
        try:
            return self.instruments[key]
        except KeyError:
            raise OrderRejected(self.venue, f"unknown symbol {symbol!r}") from None

    def ticker(self, symbol: str) -> Ticker:
        self._require_connection()
        return self.simulator.ticker(f"{self.venue}:{symbol}")

    def orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        self._require_connection()
        return self.simulator.orderbook(f"{self.venue}:{symbol}", depth)

    # ------------------------------------------------------------------
    # margin & valuation
    # ------------------------------------------------------------------
    def _mark(self, spec: InstrumentSpec) -> Decimal:
        """Mark price, tolerating a disconnected feed by using the last tick."""
        return self.simulator.ticker(spec.key).mid

    def position_margin(
        self, spec: InstrumentSpec, position: Position, mark: Decimal
    ) -> tuple[Decimal, Decimal]:
        """``(initial_margin, maintenance_margin)`` in account currency."""
        if position.is_flat:
            return ZERO, ZERO
        converter = QuantityConverter(spec)
        notional_quote = abs(converter.notional_quote(position.quantity, mark))
        notional_acct = notional_quote * self.fx.try_rate(spec.quote_asset, self.account_currency)
        return (
            notional_acct * spec.effective_initial_margin_rate,
            notional_acct * spec.effective_maintenance_margin_rate,
        )

    def position_unrealized(self, spec: InstrumentSpec, position: Position, mark: Decimal) -> Decimal:
        if position.is_flat:
            return ZERO
        converter = QuantityConverter(spec)
        if spec.settlement_style is SettlementStyle.INVERSE:
            pnl_quote = position.unrealized_pnl_inverse(mark, spec.units_per_quantity)
        else:
            pnl_quote = position.unrealized_pnl_linear(mark, converter.spec.units_per_quantity)
        return pnl_quote * self.fx.try_rate(spec.quote_asset, self.account_currency)

    def account(self) -> AccountSnapshot:
        used = ZERO
        maintenance = ZERO
        unrealized = ZERO
        realized = ZERO
        open_count = 0
        for key, position in self.positions.items():
            spec = self.instruments[key]
            realized += position.realized_pnl * self.fx.try_rate(spec.quote_asset, self.account_currency)
            if position.is_flat:
                continue
            open_count += 1
            mark = self._mark(spec)
            im, mm = self.position_margin(spec, position, mark)
            used += im
            maintenance += mm
            unrealized += self.position_unrealized(spec, position, mark)
        return AccountSnapshot.build(
            venue=self.venue,
            currency=self.account_currency,
            balance=quantize(self.balance, 8),
            used_margin=quantize(used, 8),
            maintenance_margin=quantize(maintenance, 8),
            unrealized_pnl=quantize(unrealized, 8),
            realized_pnl=quantize(realized, 8),
            fees_paid=quantize(self.total_fees, 8),
            funding_net=quantize(self.total_funding, 8),
            open_positions=open_count,
            is_connected=self.is_connected,
        )

    # ------------------------------------------------------------------
    # order placement
    # ------------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        """Validate, fault-inject, match and book an order.

        Order of checks matters: connectivity, then static venue rules, then
        margin, then faults, then matching.  A fault must not be able to make
        an order pass a rule it would otherwise fail.
        """
        self._require_connection()
        leg = request.leg.value if request.leg else None

        # An API timeout leaves the caller not knowing whether the order landed.
        if self.faults.should_fire(
            FaultKind.API_TIMEOUT, venue=self.venue, symbol=request.symbol, leg=leg
        ):
            await self._simulate_latency()
            raise VenueTimeout(self.venue, "place_order")

        spec = self.spec(request.symbol)
        order = Order(order_id=new_id(f"{self.venue.lower()}-ord"), request=request)
        self.orders[order.order_id] = order

        try:
            self._validate_static(spec, request)
            ticker = self.ticker(request.symbol)
            order.reference_price = ticker.mid
            self._validate_margin(spec, request, ticker)
        except OrderRejected as exc:
            order.reject(exc.reason)
            log.warning(
                "paper order rejected",
                extra={"venue": self.venue, "symbol": request.symbol, "reason": exc.reason,
                       "order_id": order.order_id},
            )
            raise

        rejection = self.faults.should_fire(
            FaultKind.ORDER_REJECTION, venue=self.venue, symbol=request.symbol, leg=leg
        )
        if rejection is not None:
            order.reject(f"injected rejection: {rejection.reason}")
            raise OrderRejected(self.venue, order.reject_reason or "injected rejection")

        await self._simulate_latency()
        order.status = OrderStatus.SUBMITTED
        self._match(order, spec, leg)

        if self.faults.should_fire(
            FaultKind.DUPLICATE_EXECUTION_REPORT, venue=self.venue, symbol=request.symbol, leg=leg
        ) and order.fills:
            # Replay the last execution report verbatim.  ``Order.apply_fill``
            # de-duplicates on exec_id, so the position must not move.
            order.apply_fill(order.fills[-1])
            log.warning(
                "duplicate execution report replayed",
                extra={"order_id": order.order_id, "exec_id": order.fills[-1].exec_id},
            )
        return order

    def _validate_static(self, spec: InstrumentSpec, request: OrderRequest) -> None:
        if not spec.active:
            raise OrderRejected(self.venue, f"{spec.symbol} is not active")
        signed = request.signed_quantity
        if not spec.supports_side_sign(signed):
            direction = "long" if signed > ZERO else "short"
            raise OrderRejected(self.venue, f"{spec.symbol} does not allow {direction} positions")
        if self.config.enforce_trading_hours:
            now = self.simulator.clock
            if not spec.trading_hours.is_open(now.weekday(), now.time()):
                raise OrderRejected(self.venue, f"{spec.symbol} is outside its trading session")
        if request.quantity < spec.min_quantity:
            raise OrderRejected(
                self.venue,
                f"quantity {request.quantity} below minimum {spec.min_quantity}",
            )
        if request.quantity > spec.max_quantity:
            raise OrderRejected(
                self.venue,
                f"quantity {request.quantity} above maximum {spec.max_quantity}",
            )
        remainder = (request.quantity / spec.quantity_step) % 1
        if remainder != 0:
            raise OrderRejected(
                self.venue,
                f"quantity {request.quantity} is not a multiple of step {spec.quantity_step}",
            )
        if request.order_type is OrderType.LIMIT and request.price is not None:
            price_remainder = (request.price / spec.tick_size) % 1
            if price_remainder != 0:
                raise OrderRejected(
                    self.venue,
                    f"price {request.price} is not a multiple of tick {spec.tick_size}",
                )

    def _validate_margin(self, spec: InstrumentSpec, request: OrderRequest, ticker: Ticker) -> None:
        """Reject when the order would need more margin than is free.

        Margin is only charged on the *increase* in exposure: a closing order
        releases margin and must never be blocked, or the risk engine could not
        de-risk a position that is already at the limit.
        """
        position = self.positions.get(spec.key)
        existing_qty = position.quantity if position else ZERO
        new_qty = existing_qty + request.signed_quantity
        if abs(new_qty) <= abs(existing_qty) and (existing_qty == ZERO or (new_qty * existing_qty) >= ZERO):
            return  # pure reduction

        mark = ticker.mid
        converter = QuantityConverter(spec)
        added = abs(new_qty) - abs(existing_qty)
        notional_quote = abs(converter.notional_quote(added, mark))
        required = (
            notional_quote
            * self.fx.try_rate(spec.quote_asset, self.account_currency)
            * spec.effective_initial_margin_rate
        )
        snapshot = self.account()
        if required > snapshot.free_margin:
            raise InsufficientMargin(self.venue, quantize(required, 2), quantize(snapshot.free_margin, 2))

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------
    def _match(self, order: Order, spec: InstrumentSpec, leg: str | None) -> None:
        request = order.request
        is_buy = request.side.sign > 0
        book = self.orderbook(request.symbol)
        target_qty = request.quantity

        # Partial-fill fault: cap the executable quantity.
        partial_kind = FaultKind.LEG1_PARTIAL_FILL if leg == "SOURCE" else FaultKind.LEG2_PARTIAL_FILL
        partial = self.faults.should_fire(
            partial_kind, venue=self.venue, symbol=request.symbol, leg=leg
        )
        if partial is not None:
            capped = quantize(target_qty * partial.magnitude, spec.quantity_precision)
            capped = max(capped, spec.min_quantity)
            target_qty = min(target_qty, capped)

        available, sweep_price = book.sweep(is_buy, target_qty)
        if available <= ZERO:
            order.reject("no liquidity at any level")
            raise OrderRejected(self.venue, "no liquidity at any level")

        if request.time_in_force is TimeInForce.FOK and available < request.quantity:
            # Fill-or-kill is measured against what the *client asked for*, not
            # against whatever the venue was able to offer. Comparing against
            # the reduced amount would let a partial fill satisfy an FOK order,
            # which is the one thing FOK exists to prevent.
            order.reject(
                f"fill-or-kill: only {available} of {request.quantity} available"
            )
            raise OrderRejected(self.venue, order.reject_reason or "fill-or-kill unfilled")

        fill_qty = quantize(min(target_qty, available), spec.quantity_precision)
        if fill_qty < spec.min_quantity:
            order.reject(
                f"available liquidity {available} is below minimum quantity {spec.min_quantity}"
            )
            raise OrderRejected(self.venue, order.reject_reason or "insufficient liquidity")

        fill_price = sweep_price
        slip_fault = self.faults.should_fire(
            FaultKind.HIGH_SLIPPAGE, venue=self.venue, symbol=request.symbol, leg=leg
        )
        if slip_fault is not None:
            direction = Decimal(1) if is_buy else Decimal(-1)
            fill_price = fill_price * (Decimal(1) + direction * slip_fault.magnitude)

        converter = QuantityConverter(spec)
        fill_price = converter.round_price(fill_price)

        if request.order_type is OrderType.LIMIT and request.price is not None:
            crossed = fill_price <= request.price if is_buy else fill_price >= request.price
            if not crossed:
                order.status = OrderStatus.SUBMITTED  # resting, unfilled
                log.info(
                    "limit order rests unfilled",
                    extra={"order_id": order.order_id, "limit": str(request.price),
                           "market": str(fill_price)},
                )
                return

        fee = self._commission(spec, fill_qty, fill_price)
        slippage = (fill_price - order.reference_price) * Decimal(request.side.sign)

        fill = Fill(
            order_id=order.order_id,
            quantity=fill_qty,
            price=fill_price,
            fee=fee,
            is_maker=False,
            timestamp=self.simulator.clock,
            slippage=slippage,
        )
        self._book_fill(order, spec, fill)

        if order.filled_quantity < request.quantity:
            if request.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
                # IOC: fill what is available now, cancel the rest.  Leaving the
                # remainder "working" would strand a terminal order in the open
                # book forever and make reconciliation report a permanent
                # ORDER_MISMATCH after every restart.
                order.cancel()
                log.info(
                    "IOC remainder cancelled",
                    extra={"order_id": order.order_id,
                           "filled": str(order.filled_quantity),
                           "requested": str(request.quantity)},
                )
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
                if not self.config.allow_partial_fills:
                    order.cancel()

    def _commission(self, spec: InstrumentSpec, quantity: Decimal, price: Decimal) -> Decimal:
        """Taker commission in account currency.

        MT5-style brokers are configured with zero commission -- they are paid
        through the spread, which the fill price already reflects.  Double
        charging both would overstate cost by roughly 2x.
        """
        if not self.config.charges_commission:
            return ZERO
        notional_quote = abs(QuantityConverter(spec).notional_quote(quantity, price))
        fee_quote = notional_quote * from_bps(spec.taker_fee_bps)
        return quantize(fee_quote * self.fx.try_rate(spec.quote_asset, self.account_currency), 8)

    def _book_fill(self, order: Order, spec: InstrumentSpec, fill: Fill) -> None:
        if fill.exec_id in self._seen_exec_ids:
            return
        self._seen_exec_ids.add(fill.exec_id)
        order.apply_fill(fill)
        self.fills.append(fill)

        position = self.positions.setdefault(spec.key, Position(venue=self.venue, symbol=spec.symbol))
        signed = fill.quantity * order.request.side.sign
        realized_quote = self._apply_to_position(spec, position, signed, fill.price, fill.fee)

        rate = self.fx.try_rate(spec.quote_asset, self.account_currency)
        self.balance += realized_quote * rate - fill.fee
        self.total_fees += fill.fee

        log.info(
            "paper fill",
            extra={
                "venue": self.venue,
                "symbol": spec.symbol,
                "order_id": order.order_id,
                "exec_id": fill.exec_id,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "slippage": str(fill.slippage),
                "position_after": str(position.quantity),
            },
        )

    def _apply_to_position(
        self,
        spec: InstrumentSpec,
        position: Position,
        signed_quantity: Decimal,
        price: Decimal,
        fee: Decimal,
    ) -> Decimal:
        """Apply a fill and return realized PnL **in quote currency**.

        ``Position.apply`` works in price units; converting to money needs the
        contract size, and for inverse contracts the whole formula differs.
        """
        entry_before = position.average_entry
        qty_before = position.quantity
        position.apply(signed_quantity, price, fee)

        reducing = qty_before != ZERO and (qty_before > ZERO) != (signed_quantity > ZERO)
        if not reducing:
            return ZERO
        closed = min(abs(signed_quantity), abs(qty_before))
        direction = Decimal(1) if qty_before > ZERO else Decimal(-1)

        if spec.settlement_style is SettlementStyle.INVERSE:
            if entry_before <= ZERO or price <= ZERO:
                return ZERO
            notional = closed * spec.units_per_quantity * direction
            pnl_base = notional * (Decimal(1) / entry_before - Decimal(1) / price)
            return pnl_base * price
        return (price - entry_before) * closed * direction * spec.units_per_quantity

    # ------------------------------------------------------------------
    # funding / swaps
    # ------------------------------------------------------------------
    def apply_funding(self, hours: Decimal | None = None) -> list[FundingAccrual]:
        """Charge one funding interval (perps) or one night's swap (MT5).

        Sign convention: a *positive* funding rate means longs pay shorts.
        """
        accruals: list[FundingAccrual] = []
        now = self.simulator.clock
        for key, position in self.positions.items():
            if position.is_flat:
                continue
            spec = self.instruments[key]
            if spec.funding_model is FundingModel.NONE:
                continue
            mark = self._mark(spec)
            converter = QuantityConverter(spec)
            rate_used = ZERO

            if spec.funding_model is FundingModel.PERPETUAL_FUNDING:
                ticker = self.simulator.ticker(key)
                rate_used = (
                    ticker.funding_rate if ticker.funding_rate is not None
                    else spec.baseline_funding_rate
                )
                intervals = (
                    Decimal(1) if hours is None else dec(hours) / spec.funding_interval_hours
                )
                notional_quote = converter.notional_quote(position.quantity, mark)
                amount_quote = -notional_quote * rate_used * intervals
                kind = "FUNDING"
            else:
                points = spec.swap_long_points if position.quantity > ZERO else spec.swap_short_points
                nights = Decimal(1) if hours is None else dec(hours) / Decimal(24)
                if now.weekday() == spec.swap_triple_weekday:
                    nights *= Decimal(3)
                # MT5 quotes swap in *points* per lot per night.  One point is
                # worth ``tick_size * units_per_lot`` in quote currency, so the
                # same point figure means very different money on XAUUSD (100
                # oz/lot) and BTCUSD (1 BTC/lot).  The sign lives in the points
                # themselves: negative means the side pays.
                point_value = spec.tick_size * spec.units_per_quantity
                amount_quote = points * point_value * abs(position.quantity) * nights
                rate_used = points
                kind = "SWAP"

            amount_acct = quantize(
                amount_quote * self.fx.try_rate(spec.quote_asset, self.account_currency), 8
            )
            if amount_acct == ZERO:
                continue
            self.balance += amount_acct
            self.total_funding += amount_acct
            if amount_acct >= ZERO:
                position.funding_received += amount_acct
            else:
                position.funding_paid += -amount_acct

            accrual = FundingAccrual(
                venue=self.venue,
                symbol=spec.symbol,
                rate=rate_used,
                amount=amount_acct,
                currency=self.account_currency,
                timestamp=now,
                kind=kind,
            )
            accruals.append(accrual)
            self.funding_history.append(accrual)
            log.info("funding applied", extra=accrual.to_dict())
        return accruals

    # ------------------------------------------------------------------
    # stop-out
    # ------------------------------------------------------------------
    def check_stop_out(self) -> list[str]:
        """Force-close positions if the margin level breaches the stop-out.

        This models the *venue's own* protection, which fires regardless of
        what the platform's risk engine decides -- and is therefore the last
        thing standing between a paper account and a negative balance.
        """
        snapshot = self.account()
        if snapshot.is_flat or snapshot.margin_level > self.config.stop_out_margin_level:
            return []
        closed: list[str] = []
        for key, position in list(self.positions.items()):
            if position.is_flat:
                continue
            spec = self.instruments[key]
            mark = self._mark(spec)
            realized = self._apply_to_position(spec, position, -position.quantity, mark, ZERO)
            self.balance += realized * self.fx.try_rate(spec.quote_asset, self.account_currency)
            closed.append(key)
            log.error(
                "venue stop-out: position force-closed",
                extra={"venue": self.venue, "symbol": spec.symbol,
                       "margin_level": str(snapshot.margin_level)},
            )
        return closed

    # ------------------------------------------------------------------
    # introspection / reconciliation support
    # ------------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat]

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.is_complete]

    def inject_unexpected_position(self, symbol: str, quantity: Decimal, price: Decimal) -> Position:
        """Create a position the platform's database does not know about.

        Used by the ``UNEXPECTED_POSITION`` fault to prove the reconciliation
        engine detects venue-side state it never created.
        """
        spec = self.spec(symbol)
        position = self.positions.setdefault(spec.key, Position(venue=self.venue, symbol=symbol))
        position.apply(dec(quantity), dec(price))
        log.warning(
            "unexpected position injected",
            extra={"venue": self.venue, "symbol": symbol, "quantity": str(quantity)},
        )
        return position

    def restore_position(self, position: Position) -> None:
        """Reinstate a position after a restart.

        A real venue does not forget what you hold when your client process
        dies -- it is the authority.  The in-memory paper engine has to be told,
        or every restart would look like the venue had flattened the book.
        Restoration is deliberate and audited; it is not a back door for
        creating positions, which only ``_book_fill`` can do.
        """
        key = f"{self.venue}:{position.symbol}"
        if key not in self.instruments:
            log.warning(
                "skipping restore for an instrument this venue does not list",
                extra={"venue": self.venue, "symbol": position.symbol},
            )
            return
        self.positions[key] = Position(
            venue=self.venue,
            symbol=position.symbol,
            quantity=position.quantity,
            average_entry=position.average_entry,
            realized_pnl=position.realized_pnl,
            funding_paid=position.funding_paid,
            funding_received=position.funding_received,
            fees_paid=position.fees_paid,
            updated_at=position.updated_at,
        )

    def restore_balance(self, balance: Decimal) -> None:
        self.balance = dec(balance)

    def reset(self) -> None:
        self.balance = self.starting_balance
        self.positions.clear()
        self.orders.clear()
        self.fills.clear()
        self.funding_history.clear()
        self._seen_exec_ids.clear()
        self.total_fees = ZERO
        self.total_funding = ZERO
        self._forced_disconnect = False
