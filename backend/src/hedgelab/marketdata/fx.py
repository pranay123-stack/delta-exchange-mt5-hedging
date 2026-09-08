"""FX conversion.

Two legs of a hedge routinely settle in different currencies (USDT on the
perp, USD at the broker) while the desk reports in a third (INR).  Every
cross-currency number in the platform goes through this service so the
conversion path is explicit and auditable rather than an implicit ``* 1``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.market import utcnow
from ..domain.numeric import ONE, dec


class FxRateUnavailable(KeyError):
    """Raised when no conversion path exists between two currencies."""


@dataclass(frozen=True, slots=True)
class FxQuote:
    base: str
    quote: str
    rate: Decimal
    source: str
    timestamp: datetime

    @property
    def pair(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass
class FxService:
    """Directed rate graph with one-hop triangulation through a pivot.

    Rates are stored as "1 base = ``rate`` quote".  Inverses are derived, and
    an unknown pair is resolved via the pivot currency (USD by default) before
    giving up -- so USDT->INR works from USDT/USD and USD/INR alone.
    """

    pivot: str = "USD"
    _rates: dict[tuple[str, str], FxQuote] = field(default_factory=dict)

    DEFAULTS: tuple[tuple[str, str, str], ...] = (
        ("USDT", "USD", "0.9998"),
        ("USD", "INR", "88.42"),
        ("USD", "EUR", "0.9215"),
        ("BTC", "USD", "102500"),
        ("ETH", "USD", "3850"),
    )

    def __post_init__(self) -> None:
        if not self._rates:
            for base, quote, rate in self.DEFAULTS:
                self.set_rate(base, quote, dec(rate), source="seed")

    def set_rate(self, base: str, quote: str, rate: Decimal, source: str = "manual") -> FxQuote:
        rate = dec(rate)
        if rate <= 0:
            raise ValueError(f"FX rate for {base}/{quote} must be positive")
        quote_obj = FxQuote(base=base, quote=quote, rate=rate, source=source, timestamp=utcnow())
        self._rates[(base, quote)] = quote_obj
        return quote_obj

    def rate(self, base: str, quote: str) -> Decimal:
        """Rate to convert one unit of ``base`` into ``quote``."""
        if base == quote:
            return ONE
        direct = self._rates.get((base, quote))
        if direct is not None:
            return direct.rate
        inverse = self._rates.get((quote, base))
        if inverse is not None:
            return ONE / inverse.rate
        # One hop through the pivot.
        if base != self.pivot and quote != self.pivot:
            try:
                return self.rate(base, self.pivot) * self.rate(self.pivot, quote)
            except FxRateUnavailable:
                pass
        raise FxRateUnavailable(f"no FX path from {base} to {quote}")

    def try_rate(self, base: str, quote: str, default: Decimal = ONE) -> Decimal:
        """Rate, or ``default`` when no path exists.

        Used on presentation paths where a missing exotic rate must not take
        down a risk calculation; execution paths call :meth:`rate` and handle
        the exception.
        """
        try:
            return self.rate(base, quote)
        except FxRateUnavailable:
            return default

    def convert(self, amount: Decimal, base: str, quote: str) -> Decimal:
        return dec(amount) * self.rate(base, quote)

    def all_quotes(self) -> list[FxQuote]:
        return sorted(self._rates.values(), key=lambda q: q.pair)

    def snapshot(self) -> dict[tuple[str, str], Decimal]:
        return {pair: q.rate for pair, q in self._rates.items()}
