"""Hedge objectives -- what "hedged" is defined to mean.

Each objective answers one question: *given the source exposure, how much of
the hedge instrument neutralises it?*  They differ in which measure they
neutralise, and the differences are real money:

* **BASE_ASSET_NEUTRAL** matches base-asset holdings.  Right when you care
  about how much BTC you own.
* **NOTIONAL_NEUTRAL** matches notional value in the account currency.  The
  only objective that works when the two legs track different underlyings.
* **QUOTE_PNL_NEUTRAL** matches ``dPnL/dS``.  Differs from base-neutral for
  inverse contracts, where delta depends on the entry price.
* **ACCOUNT_CCY_PNL_NEUTRAL** matches ``dPnL/dS`` *after* FX conversion.
  Differs from quote-neutral when the legs settle in different currencies --
  a USDT-settled perp hedged with a USD CFD, reported in INR.
* **FUNDING_ADJUSTED / COST_ADJUSTED** solve a mean-variance trade-off: carry
  and execution cost are certain, residual risk is not, so the optimal ratio
  is below 1 when hedging is expensive.
* **RISK_WEIGHTED** applies a hedge beta from realised vol and correlation.
* **CUSTOM_RATIO / PARTIAL** scale an exact objective by an explicit factor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from ..domain.enums import HedgeObjective
from ..domain.exposure import Exposure
from ..domain.numeric import ZERO, clamp, safe_div


class ObjectiveError(ValueError):
    """The objective cannot be evaluated for this instrument pair."""


@dataclass(frozen=True, slots=True)
class RiskParameters:
    """Statistical inputs for the risk- and cost-aware objectives.

    Defaults describe two instruments tracking the same underlying with a
    small, non-zero basis.  ``marketdata.stats.RollingStats`` estimates these
    from observed prices; they are not assumed constants.
    """

    #: Daily volatility of the source instrument, as a fraction.
    source_daily_vol: Decimal = Decimal("0.03")
    #: Daily volatility of the hedge instrument, as a fraction.
    hedge_daily_vol: Decimal = Decimal("0.03")
    #: Correlation of daily returns between the two legs.
    correlation: Decimal = Decimal("0.995")
    #: Mean-variance risk aversion, in units of 1/(daily variance fraction).
    #: Calibration against the corrected formula ``h* = beta - k/(lambda*sigma_h^2)``:
    #: with a hedge-leg vol near 3%/day, lambda = 250 means a hedge costing
    #: 1 bp/day trims the ratio by about 0.4 percentage points below beta.
    #: Raise it to hedge more mechanically, lower it to let carry dominate.
    risk_aversion: Decimal = Decimal("250")
    #: Holding horizon in days, used to amortise one-off execution cost.
    horizon_days: Decimal = Decimal("1")
    #: Hedge ratio is never pushed outside this band by an adaptive objective.
    min_ratio: Decimal = Decimal("0")
    max_ratio: Decimal = Decimal("1.25")
    #: True when the volatilities and correlation came from observed prices
    #: rather than from these defaults.  Surfaced so a caller can tell a
    #: measurement from an assumption.
    estimated: bool = False
    #: How the statistics were arrived at, for the audit trail.
    provenance: str = "configured defaults"

    @property
    def beta(self) -> Decimal:
        """Hedge beta: ``corr * sigma_source / sigma_hedge``.

        The regression coefficient of source returns on hedge returns -- how
        many units of hedge move for one unit of source.
        """
        if self.hedge_daily_vol <= ZERO:
            return Decimal(1)
        return self.correlation * self.source_daily_vol / self.hedge_daily_vol

    @property
    def residual_daily_vol(self) -> Decimal:
        """Daily vol left after a beta hedge: ``sigma_s * sqrt(1 - rho^2)``.

        This is a *reporting* measure -- how much risk survives a perfect beta
        hedge.  It is deliberately **not** what the cost-aware objectives divide
        by; see :func:`mean_variance_ratio` for why using it there was wrong.
        """
        rho2 = self.correlation * self.correlation
        remainder = Decimal(1) - rho2
        if remainder <= ZERO:
            return ZERO
        return self.source_daily_vol * remainder.sqrt()


    def with_estimates(
        self,
        *,
        source_daily_vol: Decimal,
        hedge_daily_vol: Decimal,
        correlation: Decimal,
        provenance: str,
    ) -> RiskParameters:
        """Replace the assumed statistics, keeping the operator's preferences.

        Risk aversion, horizon and the ratio band are *policy* and stay as the
        caller set them; only the measured quantities are overwritten.
        """
        return replace(
            self,
            source_daily_vol=source_daily_vol,
            hedge_daily_vol=hedge_daily_vol,
            correlation=correlation,
            estimated=True,
            provenance=provenance,
        )


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    """Target hedge quantity plus a human-readable derivation."""

    quantity: Decimal
    effective_ratio: Decimal
    measure: str
    explanation: str
    warnings: tuple[str, ...] = ()


def _unit_measure(objective: HedgeObjective, exposure: Exposure) -> Decimal:
    """Pick the exposure measure an objective neutralises."""
    if objective is HedgeObjective.BASE_ASSET_NEUTRAL:
        return exposure.base_units
    if objective is HedgeObjective.NOTIONAL_NEUTRAL:
        return exposure.notional_account
    if objective is HedgeObjective.ACCOUNT_CCY_PNL_NEUTRAL:
        return exposure.account_delta
    return exposure.quote_delta


def _measure_name(objective: HedgeObjective) -> str:
    return {
        HedgeObjective.BASE_ASSET_NEUTRAL: "base_units",
        HedgeObjective.NOTIONAL_NEUTRAL: "notional_account",
        HedgeObjective.ACCOUNT_CCY_PNL_NEUTRAL: "account_delta",
    }.get(objective, "quote_delta")


def mean_variance_ratio(
    cost_fraction_per_day: Decimal,
    params: RiskParameters,
) -> tuple[Decimal, str]:
    """Optimal hedge ratio when carry is traded off against variance.

    Minimise ``U(h) = h*k + (lambda/2) * Var(h)`` where the variance of a
    position hedged at ratio ``h`` is the full quadratic::

        Var(h) = sigma_s^2 - 2*h*rho*sigma_s*sigma_h + h^2*sigma_h^2

    Setting ``dU/dh = k - lambda*rho*sigma_s*sigma_h + lambda*h*sigma_h^2 = 0``::

        h* = beta - k / (lambda * sigma_h^2)          beta = rho*sigma_s/sigma_h

    So the optimum is the minimum-variance ratio **beta**, pulled down by a
    carry term scaled by the *hedge leg's* variance.  This also makes the
    objectives consistent with one another: ``RISK_WEIGHTED`` is exactly the
    ``k = 0`` case.

    ``k`` is the **marginal** cost of hedging per day as a fraction of
    notional.  Only the hedge leg's carry belongs there: the source position's
    funding is paid whether or not it is hedged, so charging it against the
    hedge would argue for not hedging a position precisely because its own
    funding is expensive -- which is backwards.

    Negative ``k`` (the hedge *earns* carry) pushes the ratio above beta, which
    is why the result is clamped to the configured band.

    The denominator is ``sigma_h^2`` and not the residual variance.  An earlier
    version used the residual, which is roughly 300x smaller once volatility is
    estimated from real prices rather than assumed -- it made the carry penalty
    300x too large and drove the ratio to zero on a pair whose hedge demonstrably
    removes 98.8% of drawdown.  The error was invisible against the shipped
    default parameters and obvious the moment the statistics were measured.
    """
    sigma_hedge = params.hedge_daily_vol
    beta = params.beta
    if sigma_hedge <= ZERO:
        # No hedge-leg variance means no quadratic term to trade against, so
        # there is no interior optimum.  Fall back to the ratio matching.
        return clamp(beta, params.min_ratio, params.max_ratio), (
            "hedge volatility is zero; no risk/cost trade-off, ratio = beta"
        )

    variance = sigma_hedge * sigma_hedge
    adjustment = safe_div(cost_fraction_per_day, params.risk_aversion * variance)
    raw = beta - adjustment
    ratio = clamp(raw, params.min_ratio, params.max_ratio)
    explanation = (
        f"h* = beta - k/(lambda*sigma_hedge^2) = {beta:.6f} - "
        f"{cost_fraction_per_day:.8f}/({params.risk_aversion} * {sigma_hedge:.6f}^2) "
        f"= {raw:.6f}"
        + (f" -> clamped to {ratio}" if ratio != raw else "")
    )
    return ratio, explanation


def resolve_objective(
    objective: HedgeObjective,
    *,
    source_exposure: Exposure,
    hedge_unit_exposure: Exposure,
    target_ratio: Decimal,
    params: RiskParameters,
    carry_fraction_per_day: Decimal = ZERO,
    execution_cost_fraction: Decimal = ZERO,
) -> ObjectiveResult:
    """Compute the raw (unrounded) hedge quantity for ``objective``.

    ``hedge_unit_exposure`` is the exposure of **one** quantity unit of the
    hedge instrument, so the answer is a simple ratio of measures.  Exposure is
    homogeneous of degree one in quantity, which is what makes this exact.
    """
    warnings: list[str] = []
    measure_name = _measure_name(objective)
    source_measure = _unit_measure(objective, source_exposure)
    unit_measure = _unit_measure(objective, hedge_unit_exposure)

    if unit_measure == ZERO:
        raise ObjectiveError(
            f"hedge instrument has zero {measure_name} per unit; "
            f"objective {objective.value} cannot be solved"
        )

    if objective is HedgeObjective.BASE_ASSET_NEUTRAL and (
        source_exposure.base_asset != hedge_unit_exposure.base_asset
    ):
        warnings.append(
            f"base assets differ ({source_exposure.base_asset} vs "
            f"{hedge_unit_exposure.base_asset}); base-unit matching assumes they are fungible"
        )
    if objective is HedgeObjective.QUOTE_PNL_NEUTRAL and (
        source_exposure.quote_asset != hedge_unit_exposure.quote_asset
    ):
        warnings.append(
            f"quote currencies differ ({source_exposure.quote_asset} vs "
            f"{hedge_unit_exposure.quote_asset}); use ACCOUNT_CCY_PNL_NEUTRAL to include FX"
        )

    ratio = target_ratio
    explanation: str

    if objective in (
        HedgeObjective.BASE_ASSET_NEUTRAL,
        HedgeObjective.NOTIONAL_NEUTRAL,
        HedgeObjective.QUOTE_PNL_NEUTRAL,
        HedgeObjective.ACCOUNT_CCY_PNL_NEUTRAL,
    ):
        explanation = (
            f"qty = -source.{measure_name} * ratio / hedge_unit.{measure_name} "
            f"= -({source_measure}) * {ratio} / ({unit_measure})"
        )
    elif objective in (HedgeObjective.CUSTOM_RATIO, HedgeObjective.PARTIAL):
        explanation = (
            f"explicit ratio {ratio} applied to {measure_name}: "
            f"qty = -({source_measure}) * {ratio} / ({unit_measure})"
        )
        if objective is HedgeObjective.PARTIAL and ratio >= Decimal(1):
            warnings.append(f"PARTIAL objective with ratio {ratio} >= 1 is a full hedge")
    elif objective is HedgeObjective.FUNDING_ADJUSTED:
        solved, detail = mean_variance_ratio(carry_fraction_per_day, params)
        ratio = solved * target_ratio
        explanation = f"funding-adjusted: {detail}; scaled by target ratio {target_ratio}"
    elif objective is HedgeObjective.COST_ADJUSTED:
        amortised = safe_div(execution_cost_fraction, max(params.horizon_days, Decimal("0.0001")))
        total_cost = carry_fraction_per_day + amortised
        solved, detail = mean_variance_ratio(total_cost, params)
        ratio = solved * target_ratio
        explanation = (
            f"cost-adjusted: k = carry {carry_fraction_per_day:.8f} + execution "
            f"{execution_cost_fraction:.8f}/{params.horizon_days}d = {total_cost:.8f}; {detail}"
        )
    elif objective is HedgeObjective.RISK_WEIGHTED:
        beta = params.beta
        ratio = beta * target_ratio
        explanation = (
            f"risk-weighted: beta = rho * sigma_src/sigma_hedge = {params.correlation} * "
            f"{params.source_daily_vol}/{params.hedge_daily_vol} = {beta:.6f}; "
            f"ratio = beta * {target_ratio}"
        )
        if params.correlation < Decimal("0.8"):
            warnings.append(
                f"correlation {params.correlation} is low; a beta hedge leaves "
                f"substantial residual risk"
            )
    else:  # pragma: no cover - exhaustive over the enum
        raise ObjectiveError(f"unhandled objective {objective}")

    quantity = -source_measure * ratio / unit_measure
    return ObjectiveResult(
        quantity=quantity,
        effective_ratio=ratio,
        measure=measure_name,
        explanation=explanation,
        warnings=tuple(warnings),
    )
