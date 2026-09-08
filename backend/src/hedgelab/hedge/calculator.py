"""The hedge calculator.

Takes a source position and produces everything needed to decide whether and
how to hedge it: the quantity, what is left over, what it costs, what margin
it consumes, and what it is expected to be worth.

Two design commitments:

1. **Show the work.**  Every result carries an ordered list of
   :class:`CalculationStep` records with the formula and value at each stage.
   The dashboard renders them; the audit log stores them.  A hedge number
   nobody can reconstruct is not usable in production.
2. **No fabricated numbers.**  Slippage comes from walking the simulated book,
   spread from the actual quotes, funding from the actual rate.  Where a
   number depends on an assumption (volatility, horizon) the assumption is an
   explicit input, not a hidden constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from ..domain.account import AccountSnapshot
from ..domain.enums import FundingModel, HedgeObjective
from ..domain.exposure import Exposure, NetExposure
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, Ticker
from ..domain.numeric import ZERO, from_bps, normalize, quantize, safe_div
from ..domain.quantity import QuantityConverter, RoundedQuantity
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from .objectives import ObjectiveError, RiskParameters, resolve_objective

log = get_logger(__name__)

#: 99% one-tailed normal quantile, used for the worst-case estimate.
Z_99 = Decimal("2.326")


@dataclass(frozen=True, slots=True)
class CalculationStep:
    """One line of the derivation."""

    label: str
    formula: str
    value: str
    unit: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "formula": self.formula, "value": self.value, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class HedgeInputs:
    """Everything the calculator needs.  Explicit so it can be replayed."""

    source_spec: InstrumentSpec
    hedge_spec: InstrumentSpec
    #: Signed source position in source quantity units (+long, -short).
    source_quantity: Decimal
    source_ticker: Ticker
    hedge_ticker: Ticker
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL
    target_ratio: Decimal = Decimal(1)
    #: Existing hedge position, so the rebalance delta can be computed.
    current_hedge_quantity: Decimal = ZERO
    #: Average entry of the source position; matters for inverse delta.
    source_entry_price: Decimal | None = None
    hedge_entry_price: Decimal | None = None
    hedge_book: OrderBook | None = None
    hedge_account: AccountSnapshot | None = None
    risk_params: RiskParameters = RiskParameters()
    account_currency: str = "USD"


@dataclass(frozen=True, slots=True)
class HedgeCalculation:
    """The full, auditable answer."""

    # --- identity ------------------------------------------------------
    source_key: str
    hedge_key: str
    objective: HedgeObjective
    target_ratio: Decimal
    account_currency: str

    # --- quantities ----------------------------------------------------
    required_quantity: Decimal          # exact, unrounded
    rounded_quantity: Decimal           # snapped to the venue lattice
    rebalance_quantity: Decimal         # rounded - current hedge position
    min_executable_quantity: Decimal
    max_safe_quantity: Decimal
    rounding: RoundedQuantity

    # --- ratios and exposures ------------------------------------------
    hedge_ratio: Decimal                # achieved, after rounding
    source_exposure: Exposure
    hedge_exposure: Exposure
    residual: NetExposure
    notional_exposure: Decimal          # gross, account currency
    currency_exposure: dict[str, Decimal]

    # --- costs ---------------------------------------------------------
    estimated_fees: Decimal
    estimated_spread_cost: Decimal
    estimated_slippage: Decimal
    total_execution_cost: Decimal
    funding_impact_per_day: Decimal
    margin_requirement: Decimal

    # --- outcomes ------------------------------------------------------
    expected_pnl: Decimal
    worst_case_pnl: Decimal
    break_even_move_pct: Decimal | None

    # --- diagnostics ---------------------------------------------------
    conversion_ratio: Decimal
    is_executable: bool
    warnings: tuple[str, ...] = ()
    steps: tuple[CalculationStep, ...] = ()
    horizon_days: Decimal = Decimal(1)

    #: Hedge-leg mark used to value residual delta in money terms.
    mark_price: Decimal = Decimal(1)

    @property
    def residual_value(self) -> Decimal:
        """Residual delta expressed as money in the account currency.

        ``account_delta`` is a *sensitivity* (money per unit of price move);
        multiplying by the mark restates it as the notional still at risk.
        """
        return abs(self.residual.account_delta) * self.mark_price

    @property
    def residual_bps(self) -> Decimal:
        """Residual exposure as basis points of the source's account notional."""
        base = abs(self.source_exposure.notional_account)
        if base == ZERO:
            return ZERO
        return safe_div(self.residual_value, base) * Decimal(10000)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source_key,
            "hedge": self.hedge_key,
            "objective": self.objective.value,
            "target_ratio": str(self.target_ratio),
            "required_quantity": str(self.required_quantity),
            "rounded_quantity": str(self.rounded_quantity),
            "rebalance_quantity": str(self.rebalance_quantity),
            "hedge_ratio": str(self.hedge_ratio),
            "residual_quote_delta": str(self.residual.quote_delta),
            "residual_bps": str(quantize(self.residual_bps, 4)),
            "notional_exposure": str(self.notional_exposure),
            "estimated_fees": str(self.estimated_fees),
            "estimated_spread_cost": str(self.estimated_spread_cost),
            "estimated_slippage": str(self.estimated_slippage),
            "total_execution_cost": str(self.total_execution_cost),
            "funding_impact_per_day": str(self.funding_impact_per_day),
            "margin_requirement": str(self.margin_requirement),
            "expected_pnl": str(self.expected_pnl),
            "worst_case_pnl": str(self.worst_case_pnl),
            "break_even_move_pct": (
                str(self.break_even_move_pct)
                if self.break_even_move_pct is not None else None
            ),
            "max_safe_quantity": str(self.max_safe_quantity),
            "min_executable_quantity": str(self.min_executable_quantity),
            "conversion_ratio": str(self.conversion_ratio),
            "is_executable": self.is_executable,
            "warnings": list(self.warnings),
            "steps": [s.to_dict() for s in self.steps],
        }


class HedgeCalculator:
    """Stateless calculator.  One instance can serve every pair."""

    def __init__(self, fx: FxService) -> None:
        self.fx = fx

    # ------------------------------------------------------------------
    def calculate_for_quantity(
        self, inputs: HedgeInputs, quantity: Decimal
    ) -> HedgeCalculation:
        """Evaluate a *specific* hedge quantity instead of solving the objective.

        Used by the optimiser to score neighbouring lattice points, and by the
        API's "what if I traded exactly this" view.  Every downstream number --
        residual, cost, margin, expected P&L -- is computed identically to
        :meth:`calculate`, so candidates are directly comparable.
        """
        return self.calculate(inputs, force_quantity=quantity)

    def calculate(
        self, inputs: HedgeInputs, force_quantity: Decimal | None = None
    ) -> HedgeCalculation:
        steps: list[CalculationStep] = []
        warnings: list[str] = []
        src, hdg = inputs.source_spec, inputs.hedge_spec
        src_conv, hdg_conv = QuantityConverter(src), QuantityConverter(hdg)
        acct = inputs.account_currency

        src_mid = inputs.source_ticker.mid
        hdg_mid = inputs.hedge_ticker.mid
        src_fx = self.fx.try_rate(src.quote_asset, acct)
        hdg_fx = self.fx.try_rate(hdg.quote_asset, acct)

        steps.append(CalculationStep(
            "Market data",
            f"{src.symbol} mid / {hdg.symbol} mid",
            f"{normalize(src_mid)} {src.quote_asset} / {normalize(hdg_mid)} {hdg.quote_asset}",
        ))
        steps.append(CalculationStep(
            "Contract sizing",
            f"{src.describe_sizing()}  |  {hdg.describe_sizing()}",
            f"1 {src.quantity_unit.value.lower()} vs 1 {hdg.quantity_unit.value.lower()}",
        ))
        if src_fx != Decimal(1) or hdg_fx != Decimal(1):
            steps.append(CalculationStep(
                "FX conversion",
                f"{src.quote_asset}/{acct} = {normalize(src_fx)}, "
                f"{hdg.quote_asset}/{acct} = {normalize(hdg_fx)}",
                f"quote -> {acct}",
            ))

        # --- 1. source exposure -----------------------------------------
        source_exposure = src_conv.exposure(
            inputs.source_quantity,
            src_mid,
            account_currency=acct,
            fx_rate=src_fx,
            entry_price=inputs.source_entry_price,
        )
        steps.append(CalculationStep(
            "Source exposure",
            f"{normalize(inputs.source_quantity)} x {normalize(src.units_per_quantity)} "
            f"{'quote units (inverse)' if src.is_inverse else src.base_asset}",
            f"base={normalize(quantize(source_exposure.base_units, 8))} {src.base_asset}, "
            f"notional={normalize(quantize(source_exposure.notional_quote, 2))} {src.quote_asset}, "
            f"delta={normalize(quantize(source_exposure.quote_delta, 8))}",
        ))
        if src.is_inverse and inputs.source_entry_price:
            steps.append(CalculationStep(
                "Inverse delta note",
                "dPnL_quote/dS = notional / entry_price (not / mark)",
                f"{normalize(quantize(source_exposure.notional_quote, 2))} / "
                f"{normalize(inputs.source_entry_price)} = "
                f"{normalize(quantize(source_exposure.quote_delta, 8))}",
            ))

        # --- 2. hedge unit exposure -------------------------------------
        unit_exposure = hdg_conv.exposure(
            Decimal(1), hdg_mid, account_currency=acct, fx_rate=hdg_fx
        )
        steps.append(CalculationStep(
            "Hedge exposure per unit",
            f"1 {hdg.quantity_unit.value.lower()} of {hdg.symbol}",
            f"base={normalize(quantize(unit_exposure.base_units, 8))} {hdg.base_asset}, "
            f"notional={normalize(quantize(unit_exposure.notional_account, 2))} {acct}, "
            f"delta={normalize(quantize(unit_exposure.quote_delta, 8))}",
        ))

        # --- 3. carry inputs for the adaptive objectives ----------------
        hedge_carry_per_day, total_carry_per_day, carry_detail = self._carry_fraction_per_day(
            src, hdg, inputs, src_mid, hdg_mid
        )
        exec_cost_fraction = self._round_trip_cost_fraction(src, hdg)
        if inputs.objective in (HedgeObjective.FUNDING_ADJUSTED, HedgeObjective.COST_ADJUSTED):
            steps.append(CalculationStep(
                "Carry input (marginal: hedge leg only)",
                carry_detail + " -- the source leg's funding is sunk and excluded",
                f"{quantize(hedge_carry_per_day * 10000, 4)} bps/day",
            ))
        if inputs.objective in (
            HedgeObjective.FUNDING_ADJUSTED,
            HedgeObjective.COST_ADJUSTED,
            HedgeObjective.RISK_WEIGHTED,
        ):
            params = inputs.risk_params
            steps.append(CalculationStep(
                "Statistical inputs",
                f"{'measured' if params.estimated else 'ASSUMED'}: {params.provenance}",
                f"sigma_source={quantize(params.source_daily_vol, 6)}/day, "
                f"sigma_hedge={quantize(params.hedge_daily_vol, 6)}/day, "
                f"rho={quantize(params.correlation, 6)}, "
                f"beta={quantize(params.beta, 6)}",
            ))
            if not params.estimated:
                warnings.append(
                    f"volatility and correlation are assumptions, not measurements "
                    f"({params.provenance}); this objective is only as good as they are"
                )

        # --- 4. solve the objective (or accept an imposed quantity) ------
        if force_quantity is not None:
            required = force_quantity
            steps.append(CalculationStep(
                "Imposed quantity",
                f"objective {inputs.objective.value} bypassed; evaluating a specified quantity",
                f"{normalize(force_quantity)} {hdg.quantity_unit.value.lower()}s",
            ))
            return self._finish(
                inputs, steps, warnings, src, hdg, src_conv, hdg_conv, acct,
                src_mid, hdg_mid, src_fx, hdg_fx, source_exposure, required,
                total_carry_per_day, carry_detail,
            )

        try:
            objective_result = resolve_objective(
                inputs.objective,
                source_exposure=source_exposure,
                hedge_unit_exposure=unit_exposure,
                target_ratio=inputs.target_ratio,
                params=inputs.risk_params,
                carry_fraction_per_day=hedge_carry_per_day,
                execution_cost_fraction=exec_cost_fraction,
            )
        except ObjectiveError as exc:
            raise ObjectiveError(f"{src.key} -> {hdg.key}: {exc}") from exc

        warnings.extend(objective_result.warnings)
        required = objective_result.quantity
        steps.append(CalculationStep(
            f"Objective: {inputs.objective.value}",
            objective_result.explanation,
            f"{normalize(quantize(required, 10))} {hdg.quantity_unit.value.lower()}s",
        ))

        return self._finish(
            inputs, steps, warnings, src, hdg, src_conv, hdg_conv, acct,
            src_mid, hdg_mid, src_fx, hdg_fx, source_exposure, required,
            total_carry_per_day, carry_detail,
        )

    def _finish(
        self,
        inputs: HedgeInputs,
        steps: list[CalculationStep],
        warnings: list[str],
        src: InstrumentSpec,
        hdg: InstrumentSpec,
        src_conv: QuantityConverter,
        hdg_conv: QuantityConverter,
        acct: str,
        src_mid: Decimal,
        hdg_mid: Decimal,
        src_fx: Decimal,
        hdg_fx: Decimal,
        source_exposure: Exposure,
        required: Decimal,
        total_carry_per_day: Decimal,
        carry_detail: str,
    ) -> HedgeCalculation:
        """Everything downstream of "we know the target quantity".

        Shared by the objective-driven and quantity-imposed paths so the two
        can never diverge in how they cost, size margin or value the residual.
        """
        # --- 5. round onto the venue lattice ----------------------------
        rounding = hdg_conv.round_quantity(required)
        rounded = rounding.rounded
        steps.append(CalculationStep(
            "Venue rounding",
            f"step={normalize(hdg.quantity_step)}, min={normalize(hdg.min_quantity)}, "
            f"max={normalize(hdg.max_quantity)}, round toward zero",
            f"{normalize(required)} -> {normalize(rounded)} "
            f"(error {normalize(quantize(rounding.rounding_error, 10))})",
        ))
        if rounding.below_minimum:
            warnings.append(
                f"required quantity {normalize(required)} is below the venue minimum "
                f"{normalize(hdg.min_quantity)}; hedge is not executable"
            )
        if rounding.above_maximum:
            warnings.append(
                f"required quantity capped at venue maximum {normalize(hdg.max_quantity)}"
            )

        # --- 6. resulting exposures and residual ------------------------
        hedge_exposure = hdg_conv.exposure(
            rounded, hdg_mid, account_currency=acct, fx_rate=hdg_fx,
            entry_price=inputs.hedge_entry_price,
        )
        residual = NetExposure.combine(source_exposure, hedge_exposure)
        achieved_ratio = self._achieved_ratio(inputs.objective, residual)
        steps.append(CalculationStep(
            "Residual exposure",
            "source + hedge (signed)",
            f"base={normalize(quantize(residual.base_units, 8))}, "
            f"delta={normalize(quantize(residual.quote_delta, 8))}, "
            f"ratio={normalize(quantize(achieved_ratio, 6))}",
        ))

        # --- 7. execution cost ------------------------------------------
        trade_quantity = abs(rounded - inputs.current_hedge_quantity)
        fees = self._fees(hdg, trade_quantity, hdg_mid, hdg_fx)
        spread_cost = self._spread_cost(hdg, trade_quantity, inputs.hedge_ticker, hdg_fx)
        slippage = self._slippage(
            hdg, trade_quantity, inputs.hedge_ticker, inputs.hedge_book,
            is_buy=(rounded - inputs.current_hedge_quantity) > ZERO, fx_rate=hdg_fx,
        )
        total_cost = fees + spread_cost + slippage
        steps.append(CalculationStep(
            "Execution cost",
            f"fees {normalize(quantize(fees, 4))} + half-spread {normalize(quantize(spread_cost, 4))} "
            f"+ slippage {normalize(quantize(slippage, 4))}",
            f"{normalize(quantize(total_cost, 4))} {acct}",
        ))

        # --- 8. funding / carry on the hedged book ----------------------
        funding_per_day = self._funding_impact_per_day(
            src, hdg, inputs, rounded, src_mid, hdg_mid, src_fx, hdg_fx
        )
        steps.append(CalculationStep(
            "Carry (per day)",
            carry_detail,
            f"{normalize(quantize(funding_per_day, 4))} {acct}/day",
        ))

        # --- 9. margin ---------------------------------------------------
        margin = self._margin_requirement(hdg, rounded, hdg_mid, hdg_fx)
        max_safe = self._max_safe_quantity(hdg, hdg_mid, hdg_fx, inputs.hedge_account)
        steps.append(CalculationStep(
            "Margin",
            f"|notional| x initial_margin_rate {normalize(hdg.effective_initial_margin_rate)}",
            f"{normalize(quantize(margin, 2))} {acct} "
            f"(max safe {normalize(max_safe)} {hdg.quantity_unit.value.lower()}s)",
        ))
        if abs(rounded) > max_safe:
            warnings.append(
                f"hedge quantity {normalize(abs(rounded))} exceeds the margin-safe maximum "
                f"{normalize(max_safe)}"
            )

        # --- 10. expected / worst-case outcome ---------------------------
        horizon = inputs.risk_params.horizon_days
        expected_pnl = funding_per_day * horizon - total_cost
        residual_value = abs(residual.account_delta) * hdg_mid
        residual_shock = (
            residual_value
            * inputs.risk_params.source_daily_vol
            * horizon.sqrt()
            * Z_99
        )
        worst_case = expected_pnl - residual_shock
        break_even = self._break_even_move(
            abs(source_exposure.notional_account), total_cost
        )

        steps.append(CalculationStep(
            "Expected P&L",
            f"carry {normalize(quantize(funding_per_day, 4))}/day x {normalize(horizon)}d "
            f"- execution {normalize(quantize(total_cost, 4))}",
            f"{normalize(quantize(expected_pnl, 4))} {acct}",
        ))
        steps.append(CalculationStep(
            "Worst case (99%)",
            f"expected - z99 x |residual| x sigma x sqrt(days) = "
            f"{normalize(quantize(expected_pnl, 4))} - {Z_99} x "
            f"{normalize(quantize(residual_value, 2))} x {inputs.risk_params.source_daily_vol} "
            f"x sqrt({normalize(horizon)})",
            f"{normalize(quantize(worst_case, 4))} {acct}",
        ))
        if break_even is not None:
            steps.append(CalculationStep(
                "Break-even move",
                "execution cost / source notional -- the adverse move that would "
                "have cost the unhedged position what this hedge cost to put on",
                f"{normalize(quantize(break_even, 6))} %",
            ))

        # --- 11. currency exposure ---------------------------------------
        currency_exposure = self._currency_exposure(src, hdg, source_exposure, hedge_exposure)

        conversion_ratio = self._conversion_ratio(src_conv, hdg, src_mid, hdg_mid, warnings)

        if inputs.source_ticker.is_stale or inputs.hedge_ticker.is_stale:
            warnings.append("market data is stale; the calculation is based on a frozen feed")

        is_executable = (
            rounding.is_executable
            and abs(rounded) <= max_safe
            and not (inputs.source_ticker.is_stale or inputs.hedge_ticker.is_stale)
        )

        result = HedgeCalculation(
            source_key=src.key,
            hedge_key=hdg.key,
            objective=inputs.objective,
            target_ratio=inputs.target_ratio,
            account_currency=acct,
            required_quantity=required,
            rounded_quantity=rounded,
            rebalance_quantity=normalize(rounded - inputs.current_hedge_quantity),
            min_executable_quantity=hdg.min_quantity,
            max_safe_quantity=max_safe,
            rounding=rounding,
            hedge_ratio=achieved_ratio,
            source_exposure=source_exposure,
            hedge_exposure=hedge_exposure,
            residual=residual,
            notional_exposure=(
                abs(source_exposure.notional_account)
                + abs(hedge_exposure.notional_account)
            ),
            currency_exposure=currency_exposure,
            estimated_fees=quantize(fees, 8),
            estimated_spread_cost=quantize(spread_cost, 8),
            estimated_slippage=quantize(slippage, 8),
            total_execution_cost=quantize(total_cost, 8),
            funding_impact_per_day=quantize(funding_per_day, 8),
            margin_requirement=quantize(margin, 8),
            expected_pnl=quantize(expected_pnl, 8),
            worst_case_pnl=quantize(worst_case, 8),
            break_even_move_pct=quantize(break_even, 8) if break_even is not None else None,
            conversion_ratio=conversion_ratio,
            is_executable=is_executable,
            warnings=tuple(warnings),
            steps=tuple(steps),
            horizon_days=horizon,
            mark_price=hdg_mid,
        )
        log.info(
            "hedge calculated",
            extra={
                "source": src.key, "hedge": hdg.key, "objective": inputs.objective.value,
                "required": str(normalize(required)), "rounded": str(normalize(rounded)),
                "ratio": str(quantize(achieved_ratio, 6)),
                "residual_delta": str(quantize(residual.quote_delta, 8)),
                "executable": is_executable,
            },
        )
        return result

    # ------------------------------------------------------------------
    # component calculations
    # ------------------------------------------------------------------
    @staticmethod
    def _achieved_ratio(objective: HedgeObjective, residual: NetExposure) -> Decimal:
        if objective is HedgeObjective.BASE_ASSET_NEUTRAL:
            return residual.ratio_base
        if objective is HedgeObjective.NOTIONAL_NEUTRAL:
            return residual.ratio_notional
        if objective is HedgeObjective.ACCOUNT_CCY_PNL_NEUTRAL:
            return residual.ratio_account_delta
        return residual.ratio_quote_delta

    def _fees(
        self, spec: InstrumentSpec, quantity: Decimal, price: Decimal, fx_rate: Decimal
    ) -> Decimal:
        if quantity == ZERO:
            return ZERO
        notional = abs(QuantityConverter(spec).notional_quote(quantity, price))
        return notional * from_bps(spec.taker_fee_bps) * fx_rate

    def _spread_cost(
        self, spec: InstrumentSpec, quantity: Decimal, ticker: Ticker, fx_rate: Decimal
    ) -> Decimal:
        """Cost of crossing: half the quoted spread on the traded notional.

        Half, not full: entering pays half the spread away from mid; the other
        half is only paid if and when the position is closed.
        """
        if quantity == ZERO:
            return ZERO
        delta = abs(QuantityConverter(spec).quote_delta(quantity, ticker.mid))
        return delta * (ticker.spread / Decimal(2)) * fx_rate

    def _slippage(
        self,
        spec: InstrumentSpec,
        quantity: Decimal,
        ticker: Ticker,
        book: OrderBook | None,
        *,
        is_buy: bool,
        fx_rate: Decimal,
    ) -> Decimal:
        """Slippage *beyond* the touch, measured by walking the real book.

        With no book available, fall back to the instrument's configured
        depth-sensitivity rather than pretending slippage is zero.
        """
        if quantity == ZERO:
            return ZERO
        touch = ticker.price_for(is_buy)
        converter = QuantityConverter(spec)
        if book is not None:
            filled, vwap = book.sweep(is_buy, quantity)
            if filled > ZERO:
                # Price paid worse than the touch, per unit of price.
                adverse_move = (vwap - touch) if is_buy else (touch - vwap)
                if adverse_move <= ZERO:
                    return ZERO
                # quote_delta converts a price move into money for this size.
                money_per_price_unit = abs(converter.quote_delta(filled, ticker.mid))
                return adverse_move * money_per_price_unit * fx_rate
        notional = abs(converter.notional_quote(quantity, ticker.mid))
        return notional * from_bps(spec.slippage_bps_per_unit_liquidity) * fx_rate

    def _round_trip_cost_fraction(self, src: InstrumentSpec, hdg: InstrumentSpec) -> Decimal:
        """Approximate one-off cost of putting the hedge on, as a fraction.

        Used only as an input to COST_ADJUSTED; the precise cost is computed
        separately from the live book once a quantity is known.
        """
        return from_bps(hdg.taker_fee_bps + hdg.typical_spread_bps / Decimal(2))

    def _carry_fraction_per_day(
        self,
        src: InstrumentSpec,
        hdg: InstrumentSpec,
        inputs: HedgeInputs,
        src_mid: Decimal,
        hdg_mid: Decimal,
    ) -> tuple[Decimal, Decimal, str]:
        """Carry of the pair as a fraction of notional per day.

        Returns ``(hedge_leg_only, both_legs, explanation)``.  Positive means
        it *costs* money to hold.

        The split matters.  ``both_legs`` is what the book actually pays and is
        what the dashboard reports.  ``hedge_leg_only`` is the *marginal* cost
        of the hedging decision -- the source position's funding is incurred
        whether or not it is hedged, so it must not influence the optimal hedge
        ratio.
        """
        parts: list[str] = []
        source_leg = ZERO
        hedge_leg = ZERO

        # Source leg: funding is paid by longs when the rate is positive.
        if src.funding_model is FundingModel.PERPETUAL_FUNDING:
            rate = inputs.source_ticker.funding_rate
            if rate is None:
                rate = src.baseline_funding_rate
            per_day = rate * (Decimal(24) / src.funding_interval_hours)
            direction = Decimal(1) if inputs.source_quantity > ZERO else Decimal(-1)
            leg = per_day * direction
            source_leg += leg
            parts.append(
                f"source funding {rate} x {Decimal(24) / src.funding_interval_hours}/day "
                f"x {'long' if direction > 0 else 'short'} = {quantize(leg * 10000, 4)} bps/day"
            )

        # Hedge leg: swap points, or funding if the hedge is itself a perp.
        if hdg.funding_model is FundingModel.SWAP_POINTS:
            # The hedge is opposite the source by construction.
            hedge_is_long = inputs.source_quantity < ZERO
            points = hdg.swap_long_points if hedge_is_long else hdg.swap_short_points
            point_value = hdg.tick_size * hdg.units_per_quantity
            per_lot_per_night = points * point_value
            notional_per_lot = hdg.units_per_quantity * hdg_mid
            leg = -safe_div(per_lot_per_night, notional_per_lot)
            hedge_leg += leg
            parts.append(
                f"hedge swap {points} pts x {normalize(point_value)}/pt on "
                f"{normalize(quantize(notional_per_lot, 2))} per lot = {quantize(leg * 10000, 4)} bps/day"
            )
        elif hdg.funding_model is FundingModel.PERPETUAL_FUNDING:
            rate = inputs.hedge_ticker.funding_rate or hdg.baseline_funding_rate
            per_day = rate * (Decimal(24) / hdg.funding_interval_hours)
            direction = Decimal(-1) if inputs.source_quantity > ZERO else Decimal(1)
            leg = per_day * direction
            hedge_leg += leg
            parts.append(f"hedge funding = {quantize(leg * 10000, 4)} bps/day")

        detail = "; ".join(parts) if parts else "no funding or swap on either leg"
        return hedge_leg, source_leg + hedge_leg, detail

    def _funding_impact_per_day(
        self,
        src: InstrumentSpec,
        hdg: InstrumentSpec,
        inputs: HedgeInputs,
        hedge_quantity: Decimal,
        src_mid: Decimal,
        hdg_mid: Decimal,
        src_fx: Decimal,
        hdg_fx: Decimal,
    ) -> Decimal:
        """Money carry per day on the hedged pair, in account currency.

        Sign convention matches P&L: positive means the book *earns*.
        """
        total = ZERO
        src_conv, hdg_conv = QuantityConverter(src), QuantityConverter(hdg)

        if src.funding_model is FundingModel.PERPETUAL_FUNDING and inputs.source_quantity != ZERO:
            rate = inputs.source_ticker.funding_rate
            if rate is None:
                rate = src.baseline_funding_rate
            intervals_per_day = Decimal(24) / src.funding_interval_hours
            notional = src_conv.notional_quote(inputs.source_quantity, src_mid)
            total += -notional * rate * intervals_per_day * src_fx

        if hedge_quantity != ZERO:
            if hdg.funding_model is FundingModel.SWAP_POINTS:
                points = hdg.swap_long_points if hedge_quantity > ZERO else hdg.swap_short_points
                point_value = hdg.tick_size * hdg.units_per_quantity
                total += points * point_value * abs(hedge_quantity) * hdg_fx
            elif hdg.funding_model is FundingModel.PERPETUAL_FUNDING:
                rate = inputs.hedge_ticker.funding_rate or hdg.baseline_funding_rate
                intervals_per_day = Decimal(24) / hdg.funding_interval_hours
                notional = hdg_conv.notional_quote(hedge_quantity, hdg_mid)
                total += -notional * rate * intervals_per_day * hdg_fx

        return total

    def _margin_requirement(
        self, spec: InstrumentSpec, quantity: Decimal, price: Decimal, fx_rate: Decimal
    ) -> Decimal:
        notional = abs(QuantityConverter(spec).notional_quote(quantity, price))
        return notional * fx_rate * spec.effective_initial_margin_rate

    def _max_safe_quantity(
        self,
        spec: InstrumentSpec,
        price: Decimal,
        fx_rate: Decimal,
        account: AccountSnapshot | None,
    ) -> Decimal:
        """Largest quantity the free margin supports, snapped down to the step.

        Falls back to the instrument's own maximum when no account snapshot is
        supplied (a pure "what-if" calculation with no venue attached).
        """
        if account is None or account.free_margin <= ZERO:
            return spec.max_quantity if account is None else ZERO
        unit_notional = abs(QuantityConverter(spec).notional_quote(Decimal(1), price)) * fx_rate
        unit_margin = unit_notional * spec.effective_initial_margin_rate
        if unit_margin <= ZERO:
            return spec.max_quantity
        raw = account.free_margin / unit_margin
        capped = min(raw, spec.max_quantity)
        return QuantityConverter(spec).round_quantity(capped, mode=ROUND_DOWN).rounded

    @staticmethod
    def _break_even_move(source_notional: Decimal, total_cost: Decimal) -> Decimal | None:
        """How far the market must move for the hedge to have paid for itself.

        Expressed as a percentage of the source notional: an adverse move of
        this size would have cost the *unhedged* position exactly what putting
        the hedge on cost.  Undefined when there is no source position.

        Deliberately measured against the source notional rather than the
        residual: a well-constructed hedge has near-zero residual, which would
        make a residual-based break-even diverge and say nothing useful.
        """
        if source_notional <= ZERO:
            return None
        return safe_div(total_cost, source_notional) * Decimal(100)

    @staticmethod
    def _currency_exposure(
        src: InstrumentSpec,
        hdg: InstrumentSpec,
        source_exposure: Exposure,
        hedge_exposure: Exposure,
    ) -> dict[str, Decimal]:
        """Net notional per settlement currency.

        Two legs settling in different currencies leave FX exposure even when
        the price exposure is perfectly flat -- this is what surfaces it.
        """
        exposure: dict[str, Decimal] = {}
        for spec, exp in ((src, source_exposure), (hdg, hedge_exposure)):
            key = spec.settlement_asset
            exposure[key] = exposure.get(key, ZERO) + exp.notional_quote
        return {k: quantize(v, 8) for k, v in sorted(exposure.items())}

    @staticmethod
    def _conversion_ratio(
        src_conv: QuantityConverter,
        hdg: InstrumentSpec,
        src_mid: Decimal,
        hdg_mid: Decimal,
        warnings: list[str],
    ) -> Decimal:
        try:
            return src_conv.quantity_ratio_to(hdg, src_mid, hdg_mid)
        except Exception as exc:
            warnings.append(f"no base-unit conversion between the legs: {exc}")
            return ZERO
