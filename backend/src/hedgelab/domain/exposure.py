"""Exposure: the common language two incompatible venues are translated into.

A perpetual position is quoted in contracts, an MT5 position in lots.  They are
never directly comparable.  Both are projected onto four measures:

===================  =====================================================
``base_units``       signed holding expressed in base-asset units
``notional_quote``   signed notional value in the instrument's quote currency
``quote_delta``      dPnL_quote / dS -- the *hedgeable* price sensitivity
``account_delta``    dPnL_account / dS -- sensitivity in the account currency
===================  =====================================================

For a **linear** instrument ``base_units == quote_delta``, so the distinction
looks academic.  For an **inverse** instrument it is not::

    long N quote-units of an inverse perp entered at E, marked at S
        PnL_base  = N * (1/E - 1/S)
        PnL_quote = PnL_base * S = N * (S/E - 1)
        d PnL_quote / d S = N / E          <-- depends on the *entry* price

while its mark-to-market base holding is ``N / S``.  Quantity-matching an
inverse perp against a linear CFD therefore leaves residual exposure whenever
the price has moved away from entry.  This module makes that explicit rather
than hiding it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .numeric import ZERO, safe_div


@dataclass(frozen=True, slots=True)
class Exposure:
    """Projection of a position onto comparable economic measures."""

    base_asset: str
    quote_asset: str
    account_currency: str
    base_units: Decimal
    notional_quote: Decimal
    quote_delta: Decimal
    account_delta: Decimal
    #: FX rate applied to move quote-currency amounts into account currency.
    fx_rate: Decimal = Decimal(1)
    #: Notional restated in the account currency, for cross-currency netting.
    notional_account: Decimal = ZERO

    @classmethod
    def zero(cls, base: str = "", quote: str = "", account: str = "") -> Exposure:
        return cls(
            base_asset=base,
            quote_asset=quote,
            account_currency=account,
            base_units=ZERO,
            notional_quote=ZERO,
            quote_delta=ZERO,
            account_delta=ZERO,
            fx_rate=Decimal(1),
            notional_account=ZERO,
        )

    @property
    def is_flat(self) -> bool:
        return self.base_units == ZERO and self.quote_delta == ZERO

    def scaled(self, factor: Decimal) -> Exposure:
        """Linear scaling -- exposure is homogeneous of degree 1 in quantity."""
        return Exposure(
            base_asset=self.base_asset,
            quote_asset=self.quote_asset,
            account_currency=self.account_currency,
            base_units=self.base_units * factor,
            notional_quote=self.notional_quote * factor,
            quote_delta=self.quote_delta * factor,
            account_delta=self.account_delta * factor,
            fx_rate=self.fx_rate,
            notional_account=self.notional_account * factor,
        )

    def negated(self) -> Exposure:
        return self.scaled(Decimal(-1))


@dataclass(frozen=True, slots=True)
class NetExposure:
    """Residual left after combining a source leg with its hedge leg.

    ``ratio_*`` fields are the achieved hedge ratios on each measure: 1.0 means
    fully hedged on that measure, 0.0 unhedged, >1.0 over-hedged.
    """

    base_units: Decimal
    notional_quote: Decimal
    quote_delta: Decimal
    account_delta: Decimal
    ratio_base: Decimal
    ratio_notional: Decimal
    ratio_quote_delta: Decimal
    ratio_account_delta: Decimal
    account_currency: str = ""

    @classmethod
    def combine(cls, source: Exposure, hedge: Exposure) -> NetExposure:
        """Net a source exposure against a hedge exposure.

        Ratios are ``-hedge/source`` so that a hedge in the opposite direction
        of the source yields a positive ratio.
        """
        return cls(
            base_units=source.base_units + hedge.base_units,
            notional_quote=source.notional_quote + hedge.notional_quote,
            quote_delta=source.quote_delta + hedge.quote_delta,
            account_delta=source.account_delta + hedge.account_delta,
            ratio_base=safe_div(-hedge.base_units, source.base_units),
            ratio_notional=safe_div(-hedge.notional_account, source.notional_account),
            ratio_quote_delta=safe_div(-hedge.quote_delta, source.quote_delta),
            ratio_account_delta=safe_div(-hedge.account_delta, source.account_delta),
            account_currency=source.account_currency or hedge.account_currency,
        )

    @property
    def is_flat(self) -> bool:
        return self.quote_delta == ZERO and self.base_units == ZERO
