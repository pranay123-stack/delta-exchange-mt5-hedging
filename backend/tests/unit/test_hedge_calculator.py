"""Hedge calculation across all nine objectives."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.conftest import approx_dec, make_cfd, make_perp

from hedgelab.domain.enums import HedgeObjective
from hedgelab.domain.market import Ticker
from hedgelab.hedge.calculator import HedgeCalculator, HedgeInputs
from hedgelab.hedge.objectives import ObjectiveError, RiskParameters, mean_variance_ratio
from hedgelab.marketdata.fx import FxService

D = Decimal


def ticker(
    venue: str, symbol: str, mid: str, spread_bps: str = "2", funding: str | None = None
) -> Ticker:
    m = D(mid)
    half = m * D(spread_bps) / D(20000)
    return Ticker(
        venue=venue, symbol=symbol, bid=m - half, ask=m + half, last=m,
        volume=D("1000"), timestamp=datetime.now(UTC),
        funding_rate=D(funding) if funding is not None else None,
        bid_size=D("1000"), ask_size=D("1000"),
    )


def build(
    *,
    source_quantity: str,
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL,
    source=None,
    hedge=None,
    source_mid: str = "100000",
    hedge_mid: str = "100000",
    target_ratio: str = "1",
    source_entry: str | None = None,
    account_currency: str = "USD",
    risk_params: RiskParameters | None = None,
    fx: FxService | None = None,
) -> tuple[HedgeCalculator, HedgeInputs]:
    src = source or make_perp(contract_size="0.001")
    hdg = hedge or make_cfd(units_per_lot="1")
    calc = HedgeCalculator(fx or FxService())
    inputs = HedgeInputs(
        source_spec=src, hedge_spec=hdg, source_quantity=D(source_quantity),
        source_ticker=ticker(src.venue, src.symbol, source_mid, funding="0.0001"),
        hedge_ticker=ticker(hdg.venue, hdg.symbol, hedge_mid, spread_bps="8"),
        objective=objective, target_ratio=D(target_ratio),
        source_entry_price=D(source_entry) if source_entry else None,
        risk_params=risk_params or RiskParameters(),
        account_currency=account_currency,
    )
    return calc, inputs


# ----------------------------------------------------------------------
# exact objectives
# ----------------------------------------------------------------------
def test_base_asset_neutral_matches_base_units() -> None:
    calc, inputs = build(source_quantity="5000", objective=HedgeObjective.BASE_ASSET_NEUTRAL)
    result = calc.calculate(inputs)
    assert result.required_quantity == D("-5")
    assert result.residual.base_units == D("0")


def test_quote_pnl_neutral_matches_delta() -> None:
    calc, inputs = build(source_quantity="5000", objective=HedgeObjective.QUOTE_PNL_NEUTRAL)
    result = calc.calculate(inputs)
    assert result.required_quantity == D("-5")
    assert result.residual.quote_delta == D("0")


def test_short_source_produces_a_long_hedge() -> None:
    calc, inputs = build(source_quantity="-5000")
    result = calc.calculate(inputs)
    assert result.required_quantity == D("5")


def test_flat_source_produces_no_hedge() -> None:
    calc, inputs = build(source_quantity="0")
    result = calc.calculate(inputs)
    assert result.required_quantity == D("0")
    assert result.rounded_quantity == D("0")


def test_notional_neutral_uses_prices_not_units() -> None:
    """With a basis between the legs, notional-matching != unit-matching."""
    calc, inputs = build(
        source_quantity="5000",
        objective=HedgeObjective.NOTIONAL_NEUTRAL,
        source_mid="100000", hedge_mid="101000",
    )
    result = calc.calculate(inputs)
    # 5 BTC x 100000 USDT x 0.9998 USDT/USD = 499,900 USD of notional;
    # at a 101,000 USD hedge price that is 4.94950 lots -- the basis *and* the
    # FX rate both move it away from a flat 5.
    assert approx_dec(result.required_quantity, "-4.9495049504950495")
    assert result.required_quantity != D("-5")


def test_account_ccy_neutral_includes_the_fx_rate() -> None:
    """A USDT-quoted leg hedged with a USD leg is not 1:1 in account terms."""
    fx = FxService()
    fx.set_rate("USDT", "USD", D("0.99"))
    calc, inputs = build(
        source_quantity="5000",
        objective=HedgeObjective.ACCOUNT_CCY_PNL_NEUTRAL,
        fx=fx,
    )
    result = calc.calculate(inputs)
    assert result.required_quantity == D("-4.95")
    assert abs(result.residual.account_delta) < D("1e-24")


def test_quote_pnl_neutral_warns_when_quote_currencies_differ() -> None:
    calc, inputs = build(source_quantity="5000", objective=HedgeObjective.QUOTE_PNL_NEUTRAL)
    result = calc.calculate(inputs)
    assert any("quote currencies differ" in w for w in result.warnings)


def test_custom_ratio_scales_linearly() -> None:
    calc, inputs = build(
        source_quantity="5000", objective=HedgeObjective.CUSTOM_RATIO, target_ratio="0.75"
    )
    assert calc.calculate(inputs).required_quantity == D("-3.75")


def test_partial_objective_leaves_deliberate_residual() -> None:
    calc, inputs = build(
        source_quantity="5000", objective=HedgeObjective.PARTIAL, target_ratio="0.4"
    )
    result = calc.calculate(inputs)
    assert result.required_quantity == D("-2")
    assert result.residual.quote_delta == D("3")   # 60% of 5 BTC still exposed


def test_partial_objective_warns_when_ratio_is_a_full_hedge() -> None:
    calc, inputs = build(
        source_quantity="5000", objective=HedgeObjective.PARTIAL, target_ratio="1"
    )
    assert any("is a full hedge" in w for w in calc.calculate(inputs).warnings)


# ----------------------------------------------------------------------
# inverse contracts
# ----------------------------------------------------------------------
def test_inverse_base_neutral_and_delta_neutral_diverge_when_in_profit() -> None:
    """The inverse-contract trap, as a test."""
    inverse = make_perp(inverse=True, contract_size="1", quote="USD", base="BTC")
    hedge = make_cfd(units_per_lot="1", base="BTC")

    base_calc, base_inputs = build(
        source_quantity="500000", objective=HedgeObjective.BASE_ASSET_NEUTRAL,
        source=inverse, hedge=hedge, source_mid="125000", hedge_mid="125000",
        source_entry="100000",
    )
    delta_calc, delta_inputs = build(
        source_quantity="500000", objective=HedgeObjective.QUOTE_PNL_NEUTRAL,
        source=inverse, hedge=hedge, source_mid="125000", hedge_mid="125000",
        source_entry="100000",
    )
    base_result = base_calc.calculate(base_inputs)
    delta_result = delta_calc.calculate(delta_inputs)

    # MTM holding is 500k/125k = 4 BTC; true delta is 500k/100k = 5.
    assert base_result.required_quantity == D("-4")
    assert delta_result.required_quantity == D("-5")


def test_inverse_at_entry_price_the_two_objectives_agree() -> None:
    inverse = make_perp(inverse=True, contract_size="1", quote="USD", base="BTC")
    hedge = make_cfd(units_per_lot="1", base="BTC")
    for objective in (HedgeObjective.BASE_ASSET_NEUTRAL, HedgeObjective.QUOTE_PNL_NEUTRAL):
        calc, inputs = build(
            source_quantity="500000", objective=objective, source=inverse, hedge=hedge,
            source_mid="100000", hedge_mid="100000", source_entry="100000",
        )
        assert calc.calculate(inputs).required_quantity == D("-5")


# ----------------------------------------------------------------------
# adaptive objectives
# ----------------------------------------------------------------------
def test_risk_weighted_applies_beta() -> None:
    params = RiskParameters(
        source_daily_vol=D("0.04"), hedge_daily_vol=D("0.05"), correlation=D("0.9")
    )
    calc, inputs = build(
        source_quantity="5000", objective=HedgeObjective.RISK_WEIGHTED, risk_params=params
    )
    result = calc.calculate(inputs)
    # beta = 0.9 * 0.04 / 0.05 = 0.72
    assert result.required_quantity == D("-3.6")


def test_risk_weighted_warns_on_low_correlation() -> None:
    params = RiskParameters(correlation=D("0.5"))
    calc, inputs = build(
        source_quantity="5000", objective=HedgeObjective.RISK_WEIGHTED, risk_params=params
    )
    assert any("correlation" in w for w in calc.calculate(inputs).warnings)


def test_funding_adjusted_trims_the_hedge_when_carry_costs() -> None:
    calc, inputs = build(source_quantity="5000", objective=HedgeObjective.FUNDING_ADJUSTED)
    result = calc.calculate(inputs)
    # The hedge leg pays swap, so a full hedge is not optimal.
    assert D("0") < abs(result.required_quantity) < D("5")


def test_cost_adjusted_trims_further_than_funding_adjusted() -> None:
    """Execution cost is a strictly additional reason to hedge less."""
    funding_calc, funding_inputs = build(
        source_quantity="5000", objective=HedgeObjective.FUNDING_ADJUSTED
    )
    cost_calc, cost_inputs = build(
        source_quantity="5000", objective=HedgeObjective.COST_ADJUSTED
    )
    funding_qty = abs(funding_calc.calculate(funding_inputs).required_quantity)
    cost_qty = abs(cost_calc.calculate(cost_inputs).required_quantity)
    assert cost_qty < funding_qty


def test_adaptive_objectives_ignore_the_source_leg_funding() -> None:
    """Source funding is sunk: it must not change the optimal hedge ratio."""
    cheap = make_perp(contract_size="0.001", funding_rate="0.00001")
    expensive = make_perp(contract_size="0.001", funding_rate="0.005")
    results = []
    for source in (cheap, expensive):
        calc, inputs = build(
            source_quantity="5000", objective=HedgeObjective.FUNDING_ADJUSTED, source=source
        )
        # Override the ticker's funding to match the spec baseline.
        inputs = replace(
            inputs,
            source_ticker=ticker(source.venue, source.symbol, "100000",
                                 funding=str(source.baseline_funding_rate)),
        )
        results.append(calc.calculate(inputs).required_quantity)
    assert results[0] == results[1]


def test_mean_variance_ratio_is_beta_when_hedging_is_free() -> None:
    """With no carry the optimum is exactly the minimum-variance ratio."""
    params = RiskParameters()
    ratio, _ = mean_variance_ratio(D("0"), params)
    assert ratio == params.beta


def test_mean_variance_ratio_exceeds_beta_for_positive_carry() -> None:
    """If the hedge pays you, the optimum over-hedges -- up to the clamp."""
    params = RiskParameters()
    ratio, explanation = mean_variance_ratio(D("-0.0005"), params)
    assert ratio > params.beta
    assert "clamped" in explanation or ratio <= params.max_ratio


def test_carry_penalty_scales_with_hedge_variance_not_residual_variance() -> None:
    """Regression: the denominator is sigma_hedge^2, not the residual's.

    Using the residual made the penalty ~300x too large once volatility was
    estimated from real prices rather than assumed, driving the ratio to zero
    on a pair whose hedge removes almost all of the drawdown.
    """
    # Correlation near 1 makes the residual vol tiny while the hedge vol is
    # unchanged -- the exact case that exposed the bug.
    params = RiskParameters(
        source_daily_vol=D("0.02763"), hedge_daily_vol=D("0.02764"),
        correlation=D("0.9999795"),
    )
    assert params.residual_daily_vol < params.hedge_daily_vol / D("100")

    ratio, _ = mean_variance_ratio(D("0.000176"), params)
    # A realistic swap cost must trim the ratio slightly, not annihilate it.
    assert params.beta - ratio < D("0.01")
    assert ratio > D("0.99")


def test_carry_penalty_is_proportional_to_carry() -> None:
    params = RiskParameters()
    small, _ = mean_variance_ratio(D("0.0001"), params)
    large, _ = mean_variance_ratio(D("0.001"), params)
    # Ten times the carry, ten times the trim below beta.
    assert approx_dec(params.beta - large, (params.beta - small) * 10, tol="1e-12")


def test_mean_variance_ratio_clamps_to_the_band() -> None:
    params = RiskParameters(max_ratio=D("1.1"), min_ratio=D("0.2"))
    high, _ = mean_variance_ratio(D("-1"), params)
    low, _ = mean_variance_ratio(D("1"), params)
    assert high == D("1.1")
    assert low == D("0.2")


def test_perfect_correlation_still_trades_carry_against_variance() -> None:
    """Correlation of 1 makes beta the whole answer, but carry still bites.

    The hedge leg's variance is what the carry is weighed against, and that is
    unaffected by correlation -- so unlike the earlier residual-based formula,
    rho = 1 does not remove the trade-off.
    """
    params = RiskParameters(correlation=D("1"))
    assert params.beta == D("1")
    assert params.residual_daily_vol == D("0")
    ratio, _ = mean_variance_ratio(D("0.001"), params)
    assert D("0") < ratio < D("1")


def test_zero_hedge_volatility_falls_back_to_beta() -> None:
    """No hedge variance means no interior optimum; do not divide by zero."""
    params = RiskParameters(hedge_daily_vol=D("0"))
    ratio, explanation = mean_variance_ratio(D("0.001"), params)
    assert ratio == params.beta
    assert "no risk/cost trade-off" in explanation


def test_objective_raises_when_the_hedge_has_no_exposure_per_unit() -> None:
    from hedgelab.domain.exposure import Exposure
    from hedgelab.hedge.objectives import resolve_objective

    with pytest.raises(ObjectiveError, match="zero"):
        resolve_objective(
            HedgeObjective.QUOTE_PNL_NEUTRAL,
            source_exposure=Exposure.zero("BTC", "USD", "USD").scaled(D("1")),
            hedge_unit_exposure=Exposure.zero("BTC", "USD", "USD"),
            target_ratio=D("1"), params=RiskParameters(),
        )


# ----------------------------------------------------------------------
# costs, margin, outcome
# ----------------------------------------------------------------------
def test_costs_are_reported_separately_and_sum_to_the_total() -> None:
    calc, inputs = build(source_quantity="5000")
    result = calc.calculate(inputs)
    assert result.total_execution_cost == (
        result.estimated_fees + result.estimated_spread_cost + result.estimated_slippage
    )


def test_mt5_leg_pays_spread_but_no_commission() -> None:
    calc, inputs = build(source_quantity="5000")
    result = calc.calculate(inputs)
    assert result.estimated_fees == D("0")        # broker takes the spread
    assert result.estimated_spread_cost > D("0")


def test_perp_hedge_leg_pays_commission() -> None:
    """Swap the roles: hedge on the perp venue, which does charge fees."""
    calc, inputs = build(
        source_quantity="5",
        source=make_cfd(units_per_lot="1"),
        hedge=make_perp(contract_size="0.001"),
    )
    assert calc.calculate(inputs).estimated_fees > D("0")


def test_margin_requirement_matches_notional_times_rate() -> None:
    hedge = make_cfd(units_per_lot="1", initial_margin_rate="0.01")
    calc, inputs = build(source_quantity="5000", hedge=hedge)
    result = calc.calculate(inputs)
    assert result.margin_requirement == D("5") * D("100000") * D("0.01")


def test_break_even_move_is_cost_over_source_notional() -> None:
    calc, inputs = build(source_quantity="5000")
    result = calc.calculate(inputs)
    expected = result.total_execution_cost / abs(
        result.source_exposure.notional_account
    ) * D(100)
    assert approx_dec(result.break_even_move_pct, expected, tol="1e-8")


def test_break_even_is_undefined_without_a_source_position() -> None:
    calc, inputs = build(source_quantity="0")
    assert calc.calculate(inputs).break_even_move_pct is None


def test_worst_case_is_never_better_than_expected() -> None:
    calc, inputs = build(source_quantity="5000")
    result = calc.calculate(inputs)
    assert result.worst_case_pnl <= result.expected_pnl


def test_currency_exposure_is_reported_per_settlement_asset() -> None:
    calc, inputs = build(source_quantity="5000")
    result = calc.calculate(inputs)
    assert set(result.currency_exposure) == {"USD", "USDT"}
    assert result.currency_exposure["USDT"] > 0
    assert result.currency_exposure["USD"] < 0


def test_stale_market_data_makes_the_hedge_non_executable() -> None:
    calc, inputs = build(source_quantity="5000")
    inputs = replace(inputs, source_ticker=replace(inputs.source_ticker, is_stale=True))
    result = calc.calculate(inputs)
    assert result.is_executable is False
    assert any("stale" in w for w in result.warnings)


def test_sub_minimum_hedge_is_flagged_not_silently_rounded_up() -> None:
    hedge = make_cfd(units_per_lot="1", min_quantity="1", quantity_step="1")
    calc, inputs = build(source_quantity="100", hedge=hedge)  # 0.1 BTC -> 0.1 lots
    result = calc.calculate(inputs)
    assert result.rounded_quantity == D("0")
    assert result.is_executable is False
    assert any("below the venue minimum" in w for w in result.warnings)


# ----------------------------------------------------------------------
# auditability
# ----------------------------------------------------------------------
def test_every_calculation_shows_its_working() -> None:
    calc, inputs = build(source_quantity="5000")
    result = calc.calculate(inputs)
    labels = [s.label for s in result.steps]
    for expected in (
        "Market data", "Contract sizing", "Source exposure", "Hedge exposure per unit",
        "Venue rounding", "Residual exposure", "Execution cost", "Margin", "Expected P&L",
    ):
        assert any(expected in label for label in labels), f"missing step: {expected}"
    assert all(step.formula and step.value for step in result.steps)


def test_result_serialises_completely() -> None:
    calc, inputs = build(source_quantity="5000")
    payload = calc.calculate(inputs).to_dict()
    for key in (
        "required_quantity", "rounded_quantity", "rebalance_quantity", "hedge_ratio",
        "residual_quote_delta", "notional_exposure", "estimated_fees",
        "estimated_spread_cost", "estimated_slippage", "funding_impact_per_day",
        "margin_requirement", "expected_pnl", "worst_case_pnl", "break_even_move_pct",
        "max_safe_quantity", "min_executable_quantity", "conversion_ratio", "steps",
    ):
        assert key in payload, f"missing field: {key}"


def test_calculate_for_quantity_overrides_the_objective() -> None:
    calc, inputs = build(source_quantity="5000")
    forced = calc.calculate_for_quantity(inputs, D("-3.5"))
    assert forced.rounded_quantity == D("-3.5")
    assert any("Imposed quantity" in s.label for s in forced.steps)
