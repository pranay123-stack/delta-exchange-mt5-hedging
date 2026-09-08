"""Quantity conversion between venues that disagree on what "one" means.

This is the module the whole platform rests on.  ``1 perpetual contract`` is
almost never ``1 MT5 lot``; the conversion goes through base-asset units:

    source qty (contracts) -> base units -> hedge qty (lots)

with each direction driven purely by the instrument specification, so a new
instrument with an unseen contract size works without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .enums import SettlementStyle
from .exposure import Exposure
from .instrument import InstrumentSpec
from .numeric import (
    ZERO,
    normalize,
    quantize,
    round_to_step,
    safe_div,
)


class ConversionError(ValueError):
    """Raised when a conversion cannot be performed meaningfully."""


@dataclass(frozen=True, slots=True)
class RoundedQuantity:
    """Result of snapping a raw quantity onto a venue's quantity lattice."""

    raw: Decimal
    rounded: Decimal
    #: ``rounded - raw``; negative means the venue lattice forced us short.
    rounding_error: Decimal
    #: True when |raw| was below ``min_quantity`` and became zero.
    below_minimum: bool
    #: True when |raw| exceeded ``max_quantity`` and was capped.
    above_maximum: bool
    step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal

    @property
    def is_executable(self) -> bool:
        return self.rounded != ZERO


class QuantityConverter:
    """Pure conversions for a single instrument specification.

    Every method is a pure function of ``spec`` plus its arguments; the class
    holds no mutable state, so it is safe to build one per call.
    """

    def __init__(self, spec: InstrumentSpec) -> None:
        self.spec = spec

    # ------------------------------------------------------------------
    # quantity <-> economic units
    # ------------------------------------------------------------------
    def base_units(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Base-asset units carried by ``quantity`` at ``price``.

        Linear:  ``qty * units_per_quantity`` -- price independent.
        Inverse: ``qty * quote_per_contract / price`` -- the mark-to-market
        base equivalent, which *does* move with price.
        """
        spec = self.spec
        if spec.is_inverse:
            if price <= ZERO:
                raise ConversionError(
                    f"{spec.key}: inverse instruments need a positive price to express base units"
                )
            return quantity * spec.units_per_quantity / price
        return quantity * spec.units_per_quantity

    def quantity_from_base_units(self, base_units: Decimal, price: Decimal) -> Decimal:
        """Inverse of :meth:`base_units` (unrounded)."""
        spec = self.spec
        if spec.is_inverse:
            if price <= ZERO:
                raise ConversionError(f"{spec.key}: inverse conversion needs a positive price")
            return safe_div(base_units * price, spec.units_per_quantity)
        return safe_div(base_units, spec.units_per_quantity)

    def notional_quote(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Signed notional in the quote currency.

        Inverse contracts are *defined* in quote units, so their notional is
        price-independent -- the single most common modelling mistake.
        """
        spec = self.spec
        if spec.is_inverse:
            return quantity * spec.units_per_quantity
        return quantity * spec.units_per_quantity * price

    def quantity_from_notional(self, notional_quote: Decimal, price: Decimal) -> Decimal:
        spec = self.spec
        if spec.is_inverse:
            return safe_div(notional_quote, spec.units_per_quantity)
        if price <= ZERO:
            raise ConversionError(f"{spec.key}: notional conversion needs a positive price")
        return safe_div(notional_quote, spec.units_per_quantity * price)

    def quote_delta(self, quantity: Decimal, price: Decimal, entry_price: Decimal | None = None) -> Decimal:
        """``dPnL_quote / dS`` for ``quantity`` at mark ``price``.

        Linear:  equals base units.
        Inverse: equals ``notional / entry`` -- and when no entry price is
        known (a hypothetical new trade) the mark is the entry, so it
        collapses to ``notional / price``.
        """
        spec = self.spec
        if not spec.is_inverse:
            return self.base_units(quantity, price)
        reference = entry_price if entry_price and entry_price > ZERO else price
        if reference <= ZERO:
            raise ConversionError(f"{spec.key}: inverse delta needs a positive reference price")
        return safe_div(quantity * spec.units_per_quantity, reference)

    def tick_value(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Money value in quote currency of a one-tick move on ``quantity``."""
        return self.quote_delta(quantity, price) * self.spec.tick_size

    def exposure(
        self,
        quantity: Decimal,
        price: Decimal,
        *,
        account_currency: str,
        fx_rate: Decimal = Decimal(1),
        entry_price: Decimal | None = None,
    ) -> Exposure:
        """Full exposure projection for a position or a hypothetical trade."""
        spec = self.spec
        notional = self.notional_quote(quantity, price)
        delta = self.quote_delta(quantity, price, entry_price)
        return Exposure(
            base_asset=spec.base_asset,
            quote_asset=spec.quote_asset,
            account_currency=account_currency,
            base_units=self.base_units(quantity, price) if price > ZERO or not spec.is_inverse else ZERO,
            notional_quote=notional,
            quote_delta=delta,
            account_delta=delta * fx_rate,
            fx_rate=fx_rate,
            notional_account=notional * fx_rate,
        )

    # ------------------------------------------------------------------
    # lattice rounding
    # ------------------------------------------------------------------
    def round_quantity(self, quantity: Decimal, *, mode: str = ROUND_DOWN) -> RoundedQuantity:
        """Snap ``quantity`` onto the venue's step/min/max lattice.

        The default rounds toward zero: under-hedging leaves a known residual
        the rebalancer can close, whereas over-hedging creates exposure in the
        opposite direction that nobody asked for.
        """
        spec = self.spec
        magnitude = abs(quantity)
        direction = Decimal(-1) if quantity < ZERO else Decimal(1)

        capped = False
        if magnitude > spec.max_quantity:
            magnitude = spec.max_quantity
            capped = True

        stepped = round_to_step(magnitude, spec.quantity_step, mode)
        stepped = quantize(stepped, spec.quantity_precision, ROUND_DOWN)

        below_min = False
        if stepped < spec.min_quantity:
            # Never silently promote to the minimum: that would execute more
            # than the calculation asked for.
            stepped = ZERO
            below_min = magnitude > ZERO

        rounded = normalize(stepped * direction)
        return RoundedQuantity(
            raw=quantity,
            rounded=rounded,
            rounding_error=rounded - quantity,
            below_minimum=below_min,
            above_maximum=capped,
            step=spec.quantity_step,
            min_quantity=spec.min_quantity,
            max_quantity=spec.max_quantity,
        )

    def round_price(self, price: Decimal, *, mode: str = ROUND_HALF_UP) -> Decimal:
        spec = self.spec
        snapped = round_to_step(price, spec.tick_size, mode)
        return quantize(snapped, spec.price_precision, ROUND_HALF_UP)

    def round_price_conservative(self, price: Decimal, is_buy: bool) -> Decimal:
        """Round a limit price *away* from a fill (buy down, sell up).

        Used when the engine has to place a passive price and must not
        accidentally cross the spread because of rounding.
        """
        mode = ROUND_FLOOR if is_buy else ROUND_CEILING
        return self.round_price(price, mode=mode)

    # ------------------------------------------------------------------
    # cross-venue conversion
    # ------------------------------------------------------------------
    def convert_to(
        self,
        target: InstrumentSpec,
        quantity: Decimal,
        source_price: Decimal,
        target_price: Decimal,
    ) -> Decimal:
        """Convert ``quantity`` of this instrument into an equivalent quantity
        of ``target``, matching **base-asset units**.

        Raises if the two instruments do not track the same underlying -- a
        base-unit conversion between BTC and gold is meaningless and must be
        expressed as a notional or risk-weighted objective instead.
        """
        if self.spec.effective_underlying_key != target.effective_underlying_key:
            raise ConversionError(
                f"cannot convert base units between {self.spec.key} "
                f"({self.spec.effective_underlying_key}) and {target.key} "
                f"({target.effective_underlying_key}): different underlyings"
            )
        units = self.base_units(quantity, source_price)
        return QuantityConverter(target).quantity_from_base_units(units, target_price)

    def quantity_ratio_to(
        self,
        target: InstrumentSpec,
        source_price: Decimal,
        target_price: Decimal,
    ) -> Decimal:
        """How many ``target`` units equal one unit of this instrument.

        The headline "1 contract = X lots" number shown in the dashboard.
        """
        return self.convert_to(target, Decimal(1), source_price, target_price)


def describe_conversion(
    source: InstrumentSpec,
    target: InstrumentSpec,
    source_price: Decimal,
    target_price: Decimal,
) -> str:
    """One-line explanation of the size relationship, for logs and the UI."""
    try:
        ratio = QuantityConverter(source).quantity_ratio_to(target, source_price, target_price)
    except ConversionError as exc:
        return f"no base-unit conversion: {exc}"
    src_unit = source.quantity_unit.value.lower()
    tgt_unit = target.quantity_unit.value.lower()
    # Inverse conversions divide by price and produce a repeating expansion;
    # 12 significant digits is far more than any venue's quantity precision.
    display = normalize(ratio.normalize().quantize(Decimal(1).scaleb(-12)))
    return (
        f"1 {source.symbol} {src_unit} ({source.describe_sizing()}) "
        f"= {display} {target.symbol} {tgt_unit} ({target.describe_sizing()})"
    )


__all__ = [
    "ConversionError",
    "QuantityConverter",
    "RoundedQuantity",
    "SettlementStyle",
    "describe_conversion",
]
