"""P&L attribution.

A hedged book's P&L is small relative to its gross legs, so "we made $40" is
useless without knowing it was *$2,100 of funding received, minus $1,600 of
swap paid, minus $310 of fees, minus $150 of spread*.  This module produces
that decomposition and guarantees the parts sum to the whole.

Sign convention throughout: **positive is money in**.  A cost is therefore a
negative number, not a positive number that gets subtracted somewhere else --
which is the mistake that makes attribution stop adding up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.instrument import InstrumentSpec
from ..domain.market import Ticker, utcnow
from ..domain.numeric import ZERO, quantize
from ..domain.orders import Fill, Position
from ..domain.quantity import QuantityConverter
from ..marketdata.fx import FxService


@dataclass(frozen=True, slots=True)
class PnLComponent:
    """One line of the attribution."""

    name: str
    amount: Decimal
    currency: str
    explanation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "amount": str(quantize(self.amount, 8)),
            "currency": self.currency,
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class PnLBreakdown:
    """Full decomposition.  ``net`` is the sum of every component."""

    scope: str
    currency: str
    funding_received: Decimal
    funding_paid: Decimal
    swap_financing: Decimal
    trading_fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    fx_conversion_impact: Decimal
    gross_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    expected_pnl: Decimal
    worst_case_pnl: Decimal
    break_even_cost: Decimal
    components: tuple[PnLComponent, ...] = ()
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def net_funding(self) -> Decimal:
        return self.funding_received - self.funding_paid + self.swap_financing

    @property
    def total_costs(self) -> Decimal:
        """All execution costs as a negative number."""
        return self.trading_fees + self.spread_cost + self.slippage_cost

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl + self.net_funding + self.total_costs + self.fx_conversion_impact

    def check_consistency(self) -> bool:
        """The components must sum to ``net_pnl``.  Guards against drift."""
        total = sum((c.amount for c in self.components), ZERO)
        return abs(total - self.net_pnl) < Decimal("0.00000001")

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "currency": self.currency,
            "funding_received": str(quantize(self.funding_received, 8)),
            "funding_paid": str(quantize(self.funding_paid, 8)),
            "swap_financing": str(quantize(self.swap_financing, 8)),
            "net_funding": str(quantize(self.net_funding, 8)),
            "trading_fees": str(quantize(self.trading_fees, 8)),
            "spread_cost": str(quantize(self.spread_cost, 8)),
            "slippage_cost": str(quantize(self.slippage_cost, 8)),
            "total_costs": str(quantize(self.total_costs, 8)),
            "fx_conversion_impact": str(quantize(self.fx_conversion_impact, 8)),
            "gross_pnl": str(quantize(self.gross_pnl, 8)),
            "net_pnl": str(quantize(self.net_pnl, 8)),
            "realized_pnl": str(quantize(self.realized_pnl, 8)),
            "unrealized_pnl": str(quantize(self.unrealized_pnl, 8)),
            "expected_pnl": str(quantize(self.expected_pnl, 8)),
            "worst_case_pnl": str(quantize(self.worst_case_pnl, 8)),
            "break_even_cost": str(quantize(self.break_even_cost, 8)),
            "components": [c.to_dict() for c in self.components],
            "consistent": self.check_consistency(),
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class LegAccounting:
    """Running cost tallies for one leg, fed by the execution engine."""

    spec: InstrumentSpec
    position: Position
    fills: list[Fill] = field(default_factory=list)
    funding_received: Decimal = ZERO
    funding_paid: Decimal = ZERO
    swap_financing: Decimal = ZERO

    def add_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    @property
    def fees(self) -> Decimal:
        return sum((f.fee for f in self.fills), ZERO)

    def realized_slippage(self, converter: QuantityConverter, mark: Decimal) -> Decimal:
        """Money lost to fills printing away from the reference price."""
        total = ZERO
        for fill in self.fills:
            money_per_price_unit = abs(converter.quote_delta(fill.quantity, mark))
            total += fill.slippage * money_per_price_unit
        return total


class PnLCalculator:
    """Builds :class:`PnLBreakdown` records from positions and cost tallies."""

    def __init__(self, fx: FxService, account_currency: str = "USD") -> None:
        self.fx = fx
        self.account_currency = account_currency

    def for_pair(
        self,
        *,
        scope: str,
        source: LegAccounting,
        hedge: LegAccounting,
        source_ticker: Ticker,
        hedge_ticker: Ticker,
        expected_pnl: Decimal = ZERO,
        worst_case_pnl: Decimal = ZERO,
    ) -> PnLBreakdown:
        acct = self.account_currency
        components: list[PnLComponent] = []

        gross_ex_fx = ZERO      # price P&L valued as if every leg quoted in `acct`
        fx_impact = ZERO        # the part that exists only because it does not
        realized = ZERO
        unrealized = ZERO
        fees = ZERO
        spread = ZERO
        slippage = ZERO

        for label, leg, ticker in (("source", source, source_ticker), ("hedge", hedge, hedge_ticker)):
            spec = leg.spec
            rate = self.fx.try_rate(spec.quote_asset, acct)
            converter = QuantityConverter(spec)
            mark = ticker.mid

            # Split the leg's price P&L into a "price" part (quote units taken
            # at face value) and an "FX" part (the effect of the conversion
            # rate).  price + fx == the account-currency P&L exactly, so the
            # components always sum to the net without any reconciliation step.
            leg_pnl_quote = self._unrealized(spec, leg.position, mark) + leg.position.realized_pnl
            price_part = leg_pnl_quote
            fx_part = leg_pnl_quote * (rate - Decimal(1))

            gross_ex_fx += price_part
            fx_impact += fx_part
            realized += leg.position.realized_pnl * rate
            unrealized += self._unrealized(spec, leg.position, mark) * rate

            leg_fees = -leg.fees
            leg_slippage = -leg.realized_slippage(converter, mark)
            leg_spread = -self._spread_cost(spec, leg, ticker, rate)
            fees += leg_fees
            spread += leg_spread
            slippage += leg_slippage

            components.extend([
                PnLComponent(
                    f"{label}_price_pnl", price_part, acct,
                    f"{spec.symbol}: price P&L {quantize(price_part, 4)} {spec.quote_asset} "
                    f"at mark {mark} (FX shown separately)",
                ),
                PnLComponent(
                    f"{label}_fees", leg_fees, acct,
                    f"{spec.symbol}: {len(leg.fills)} fills at {spec.taker_fee_bps} bps taker",
                ),
                PnLComponent(
                    f"{label}_spread", leg_spread, acct,
                    f"{spec.symbol}: half of the {quantize(ticker.spread_bps, 2)} bps quoted "
                    f"spread on the traded notional",
                ),
                PnLComponent(
                    f"{label}_slippage", leg_slippage, acct,
                    f"{spec.symbol}: fills printing away from the reference price",
                ),
            ])

        funding_received = source.funding_received + hedge.funding_received
        funding_paid = source.funding_paid + hedge.funding_paid
        swap = source.swap_financing + hedge.swap_financing

        components.extend([
            PnLComponent("funding_received", funding_received, acct,
                         "perpetual funding credited to the book"),
            PnLComponent("funding_paid", -funding_paid, acct,
                         "perpetual funding debited from the book"),
            PnLComponent("swap_financing", swap, acct,
                         "broker overnight swap/rollover on the hedge leg"),
            PnLComponent("fx_conversion", fx_impact, acct,
                         f"effect of restating non-{acct} legs into {acct}"),
        ])

        # What the book must earn just to break even: every cost, made positive.
        break_even = -(fees + spread + slippage) + funding_paid - funding_received - swap

        return PnLBreakdown(
            scope=scope,
            currency=acct,
            funding_received=funding_received,
            funding_paid=funding_paid,
            swap_financing=swap,
            trading_fees=fees,
            spread_cost=spread,
            slippage_cost=slippage,
            fx_conversion_impact=fx_impact,
            gross_pnl=gross_ex_fx,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            expected_pnl=expected_pnl,
            worst_case_pnl=worst_case_pnl,
            break_even_cost=break_even,
            components=tuple(components),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _unrealized(spec: InstrumentSpec, position: Position, mark: Decimal) -> Decimal:
        if position.is_flat:
            return ZERO
        if spec.is_inverse:
            return position.unrealized_pnl_inverse(mark, spec.units_per_quantity)
        return position.unrealized_pnl_linear(mark, spec.units_per_quantity)

    @staticmethod
    def _spread_cost(
        spec: InstrumentSpec, leg: LegAccounting, ticker: Ticker, fx_rate: Decimal
    ) -> Decimal:
        """Half-spread paid on everything that traded."""
        traded = sum((f.quantity for f in leg.fills), ZERO)
        if traded <= ZERO:
            return ZERO
        delta = abs(QuantityConverter(spec).quote_delta(traded, ticker.mid))
        return delta * (ticker.spread / Decimal(2)) * fx_rate
