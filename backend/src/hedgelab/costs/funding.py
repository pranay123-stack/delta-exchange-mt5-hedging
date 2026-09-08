"""Funding and financing projection.

Separate from the accrual bookkeeping in the paper engine: this module answers
*forward-looking* questions ("what will this pair cost me over the next 7
days?") that the dashboard and the COST_ADJUSTED objective need.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import FundingModel
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, quantize, safe_div
from ..domain.quantity import QuantityConverter


@dataclass(frozen=True, slots=True)
class FundingProjection:
    """Expected carry over a horizon, per leg and combined."""

    source_per_day: Decimal
    hedge_per_day: Decimal
    net_per_day: Decimal
    horizon_days: Decimal
    net_over_horizon: Decimal
    source_annualized_pct: Decimal
    hedge_annualized_pct: Decimal
    net_annualized_pct: Decimal
    currency: str
    explanation: str

    @property
    def is_positive_carry(self) -> bool:
        return self.net_per_day > ZERO

    def to_dict(self) -> dict[str, object]:
        return {
            "source_per_day": str(quantize(self.source_per_day, 6)),
            "hedge_per_day": str(quantize(self.hedge_per_day, 6)),
            "net_per_day": str(quantize(self.net_per_day, 6)),
            "horizon_days": str(self.horizon_days),
            "net_over_horizon": str(quantize(self.net_over_horizon, 6)),
            "source_annualized_pct": str(quantize(self.source_annualized_pct, 4)),
            "hedge_annualized_pct": str(quantize(self.hedge_annualized_pct, 4)),
            "net_annualized_pct": str(quantize(self.net_annualized_pct, 4)),
            "currency": self.currency,
            "is_positive_carry": self.is_positive_carry,
            "explanation": self.explanation,
        }


def project_funding(
    *,
    source_spec: InstrumentSpec,
    hedge_spec: InstrumentSpec,
    source_quantity: Decimal,
    hedge_quantity: Decimal,
    source_price: Decimal,
    hedge_price: Decimal,
    source_funding_rate: Decimal | None = None,
    hedge_funding_rate: Decimal | None = None,
    source_fx: Decimal = Decimal(1),
    hedge_fx: Decimal = Decimal(1),
    horizon_days: Decimal = Decimal(1),
    currency: str = "USD",
) -> FundingProjection:
    """Project carry for both legs.  Positive means the book earns."""
    notes: list[str] = []

    source_per_day = _leg_carry_per_day(
        source_spec, source_quantity, source_price, source_funding_rate, source_fx, notes, "source"
    )
    hedge_per_day = _leg_carry_per_day(
        hedge_spec, hedge_quantity, hedge_price, hedge_funding_rate, hedge_fx, notes, "hedge"
    )
    net = source_per_day + hedge_per_day

    src_notional = abs(
        QuantityConverter(source_spec).notional_quote(source_quantity, source_price)
    ) * source_fx
    hdg_notional = abs(
        QuantityConverter(hedge_spec).notional_quote(hedge_quantity, hedge_price)
    ) * hedge_fx

    return FundingProjection(
        source_per_day=source_per_day,
        hedge_per_day=hedge_per_day,
        net_per_day=net,
        horizon_days=horizon_days,
        net_over_horizon=net * horizon_days,
        source_annualized_pct=_annualized(source_per_day, src_notional),
        hedge_annualized_pct=_annualized(hedge_per_day, hdg_notional),
        net_annualized_pct=_annualized(net, max(src_notional, hdg_notional)),
        currency=currency,
        explanation="; ".join(notes) if notes else "neither leg carries financing",
    )


def _leg_carry_per_day(
    spec: InstrumentSpec,
    quantity: Decimal,
    price: Decimal,
    rate_override: Decimal | None,
    fx_rate: Decimal,
    notes: list[str],
    label: str,
) -> Decimal:
    if quantity == ZERO or spec.funding_model is FundingModel.NONE:
        return ZERO

    if spec.funding_model is FundingModel.PERPETUAL_FUNDING:
        rate = rate_override if rate_override is not None else spec.baseline_funding_rate
        intervals_per_day = Decimal(24) / spec.funding_interval_hours
        notional = QuantityConverter(spec).notional_quote(quantity, price)
        # Positive rate: longs pay.  Signed notional carries the direction.
        amount = -notional * rate * intervals_per_day * fx_rate
        notes.append(
            f"{label} {spec.symbol}: funding {quantize(rate * 10000, 4)} bps x "
            f"{intervals_per_day}/day on {quantize(notional, 2)} {spec.quote_asset} "
            f"= {quantize(amount, 4)}/day"
        )
        return amount

    points = spec.swap_long_points if quantity > ZERO else spec.swap_short_points
    point_value = spec.tick_size * spec.units_per_quantity
    amount = points * point_value * abs(quantity) * fx_rate
    notes.append(
        f"{label} {spec.symbol}: swap {points} points x {point_value}/point x "
        f"{abs(quantity)} lots = {quantize(amount, 4)}/day"
    )
    return amount


def _annualized(per_day: Decimal, notional: Decimal) -> Decimal:
    if notional <= ZERO:
        return ZERO
    return safe_div(per_day * Decimal(365), notional) * Decimal(100)
