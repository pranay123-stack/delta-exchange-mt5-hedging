"""Margin and liquidation mathematics.

Closed-form throughout -- no search loops -- so the numbers are exact and
testable against hand calculations.  Both settlement styles are handled
separately because their P&L functions have different shapes: linear P&L is
affine in price, inverse P&L is affine in *1/price*.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import SettlementStyle
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, quantize, safe_div
from ..domain.quantity import QuantityConverter


@dataclass(frozen=True, slots=True)
class MarginRequirement:
    initial: Decimal
    maintenance: Decimal
    notional: Decimal
    leverage_used: Decimal
    currency: str

    @property
    def buffer(self) -> Decimal:
        return self.initial - self.maintenance


def margin_requirement(
    spec: InstrumentSpec,
    quantity: Decimal,
    price: Decimal,
    fx_rate: Decimal = Decimal(1),
    currency: str = "USD",
) -> MarginRequirement:
    """Initial and maintenance margin for a position, in account currency."""
    notional_quote = abs(QuantityConverter(spec).notional_quote(quantity, price))
    notional = notional_quote * fx_rate
    return MarginRequirement(
        initial=notional * spec.effective_initial_margin_rate,
        maintenance=notional * spec.effective_maintenance_margin_rate,
        notional=notional,
        leverage_used=safe_div(Decimal(1), spec.effective_initial_margin_rate),
        currency=currency,
    )


@dataclass(frozen=True, slots=True)
class LiquidationEstimate:
    """Where the position dies, and how far away that is."""

    liquidation_price: Decimal | None
    mark_price: Decimal
    distance_absolute: Decimal | None
    distance_pct: Decimal | None
    #: Largest adverse move the position survives, as a positive fraction.
    max_tolerable_move: Decimal | None
    is_liquidatable: bool
    note: str = ""

    @property
    def is_safe(self) -> bool:
        return self.liquidation_price is None or not self.is_liquidatable


def liquidation_price(
    spec: InstrumentSpec,
    quantity: Decimal,
    entry_price: Decimal,
    available_equity: Decimal,
    mark_price: Decimal | None = None,
) -> LiquidationEstimate:
    """Price at which equity falls to maintenance margin.

    ``available_equity`` is the balance backing *this* position: the posted
    isolated margin, or the whole account equity under cross margin.  It is an
    explicit argument rather than a lookup so the same function serves both
    margin models and can be unit-tested in isolation.

    Linear, long::

        equity(S) = eq0 + (S - E) * N          maintenance(S) = N * S * mmr
        =>  S_liq = (E*N - eq0) / (N * (1 - mmr))

    Linear, short (M = |N|)::

        S_liq = (eq0 + E*M) / (M * (1 + mmr))

    Inverse, long (N in quote units)::

        equity_base(S) = eq0 + N*(1/E - 1/S)   maintenance_base(S) = N * mmr / S
        =>  S_liq = N * (1 + mmr) / (eq0 + N/E)
    """
    mark = mark_price if mark_price is not None else entry_price
    if quantity == ZERO:
        return LiquidationEstimate(None, mark, None, None, None, False, "flat position")
    if entry_price <= ZERO:
        return LiquidationEstimate(None, mark, None, None, None, False, "no entry price")

    mmr = spec.effective_maintenance_margin_rate
    is_long = quantity > ZERO
    magnitude = abs(quantity)

    if spec.settlement_style is SettlementStyle.INVERSE:
        notional_quote = magnitude * spec.units_per_quantity
        if is_long:
            denominator = available_equity + notional_quote / entry_price
            liq = None if denominator <= ZERO else notional_quote * (Decimal(1) + mmr) / denominator
        else:
            denominator = notional_quote / entry_price - available_equity
            liq = None if denominator <= ZERO else notional_quote * (Decimal(1) - mmr) / denominator
        note = "inverse contract: liquidation is affine in 1/S"
    else:
        units = magnitude * spec.units_per_quantity
        if is_long:
            denominator = units * (Decimal(1) - mmr)
            liq = None if denominator <= ZERO else (entry_price * units - available_equity) / denominator
        else:
            denominator = units * (Decimal(1) + mmr)
            liq = None if denominator <= ZERO else (available_equity + entry_price * units) / denominator
        note = "linear contract"

    if liq is not None and liq <= ZERO:
        # Equity covers the entire notional: the position cannot be liquidated
        # by an adverse move, only by the price going through zero.
        return LiquidationEstimate(
            None, mark, None, None, None, False,
            f"{note}; equity exceeds notional, no liquidation price",
        )

    if liq is None:
        return LiquidationEstimate(None, mark, None, None, None, False, f"{note}; unbounded")

    distance = liq - mark
    distance_pct = safe_div(distance, mark) * Decimal(100)
    tolerable = abs(safe_div(distance, mark))
    # A long is liquidated on the way down, a short on the way up.
    breached = mark <= liq if is_long else mark >= liq

    return LiquidationEstimate(
        liquidation_price=quantize(liq, spec.price_precision),
        mark_price=mark,
        distance_absolute=quantize(distance, spec.price_precision),
        distance_pct=quantize(distance_pct, 6),
        max_tolerable_move=quantize(tolerable, 8),
        is_liquidatable=breached,
        note=note,
    )


def margin_level(equity: Decimal, used_margin: Decimal) -> Decimal:
    """MT5-style margin level percentage.  Flat accounts return a large sentinel."""
    if used_margin <= ZERO:
        return Decimal("999999")
    return safe_div(equity, used_margin) * Decimal(100)


def equity_at_price(
    spec: InstrumentSpec,
    quantity: Decimal,
    entry_price: Decimal,
    balance: Decimal,
    price: Decimal,
    fx_rate: Decimal = Decimal(1),
) -> Decimal:
    """Account equity if the mark were ``price``.  Used for stress scenarios."""
    if quantity == ZERO:
        return balance
    if spec.settlement_style is SettlementStyle.INVERSE:
        notional = quantity * spec.units_per_quantity
        pnl_base = notional * (Decimal(1) / entry_price - Decimal(1) / price)
        pnl_quote = pnl_base * price
    else:
        pnl_quote = (price - entry_price) * quantity * spec.units_per_quantity
    return balance + pnl_quote * fx_rate
