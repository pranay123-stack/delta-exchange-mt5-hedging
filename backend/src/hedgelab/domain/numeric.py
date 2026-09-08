"""Decimal helpers.

Money and quantities are ``Decimal`` everywhere.  Binary floats silently
break exchange rounding rules (``0.1 + 0.2 != 0.3``), and a hedge platform
that mis-rounds a quantity by one step submits a rejected order.

Rounding policy:
  * **quantities** round *down toward zero* by default -- never execute more
    than the calculation asked for.
  * **prices** round half-up to the tick.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")

# Guard against absurd inputs that would make Decimal exponent arithmetic explode.
MAX_ABS: Final = Decimal("1e18")


class NumericError(ValueError):
    """Raised for malformed or out-of-range numeric input."""


def dec(value: object) -> Decimal:
    """Coerce to ``Decimal`` without ever going through binary float.

    ``float`` inputs are stringified first so ``dec(0.1)`` is ``0.1`` and not
    ``0.1000000000000000055511151231257827``.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise NumericError("bool is not a numeric value")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise NumericError(f"cannot parse {value!r} as a decimal") from exc
    else:
        raise NumericError(f"unsupported numeric type {type(value).__name__}")
    if not result.is_finite():
        raise NumericError(f"non-finite decimal: {value!r}")
    if abs(result) > MAX_ABS:
        raise NumericError(f"decimal out of supported range: {value!r}")
    return result


def quantize(value: Decimal, places: int, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantize to a fixed number of decimal places."""
    if places < 0:
        raise NumericError("decimal places must be >= 0")
    exp = Decimal(1).scaleb(-places)
    return value.quantize(exp, rounding=rounding)


def round_to_step(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """Round ``value`` onto a lattice of ``step``.

    ``rounding`` is applied to the *number of steps*, so ``ROUND_DOWN`` means
    "toward zero" for both positive and negative values -- the behaviour a
    risk system wants when trimming a quantity.
    """
    if step <= 0:
        raise NumericError("step must be > 0")
    steps = (value / step).quantize(Decimal(1), rounding=rounding)
    return normalize(steps * step)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round toward negative infinity onto the step lattice."""
    return round_to_step(value, step, ROUND_FLOOR)


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round toward positive infinity onto the step lattice."""
    return round_to_step(value, step, ROUND_CEILING)


def nearest_step(value: Decimal, step: Decimal) -> Decimal:
    """Round to the nearest multiple of ``step`` (half away from zero)."""
    return round_to_step(value, step, ROUND_HALF_UP)


def normalize(value: Decimal) -> Decimal:
    """Strip trailing zeros but keep integers readable (``5`` not ``5E+1``)."""
    if value == 0:
        return ZERO
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return normalized.quantize(Decimal(1))
    return normalized


def safe_div(numerator: Decimal, denominator: Decimal, default: Decimal = ZERO) -> Decimal:
    """Divide, returning ``default`` when the denominator is zero.

    Division by zero is routine here (flat positions, zero-price ticks); the
    caller should get a neutral value rather than an exception it must guard.
    """
    if denominator == 0:
        return default
    return numerator / denominator


def clamp(value: Decimal, low: Decimal | None, high: Decimal | None) -> Decimal:
    if low is not None and value < low:
        return low
    if high is not None and value > high:
        return high
    return value


def sign(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def decimal_places(step: Decimal) -> int:
    """Number of decimal places implied by a step size (``0.001`` -> 3)."""
    exponent = step.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - NaN/Inf filtered earlier
        raise NumericError("step has no finite exponent")
    return max(0, -exponent)


def pct(value: Decimal) -> Decimal:
    """Convert a fraction to percent (0.0125 -> 1.25)."""
    return value * Decimal(100)


def bps(value: Decimal) -> Decimal:
    """Convert a fraction to basis points (0.0005 -> 5)."""
    return value * Decimal(10000)


def from_bps(value: Decimal) -> Decimal:
    """Convert basis points to a fraction (5 -> 0.0005)."""
    return value / Decimal(10000)
