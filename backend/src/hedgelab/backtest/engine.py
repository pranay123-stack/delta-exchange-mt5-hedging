"""Lightweight hedge-performance simulation.

Replays a synthetic (or supplied) price and funding path through the *real*
hedge calculator, rebalancer logic and cost model, and reports what the hedge
actually did: residual exposure over time, funding carry, fees, slippage,
drawdown and margin usage.

No claim of profitability is made anywhere. The report states what the run
produced; interpreting it is the reader's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..domain.enums import HedgeObjective
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, normalize, quantize, safe_div
from ..domain.quantity import QuantityConverter
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from ..marketdata.simulator import MarketSimulator

log = get_logger(__name__)

D = Decimal


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    source_key: str
    hedge_key: str
    #: Signed source position held throughout the run, in source units.
    source_quantity: Decimal
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL
    target_ratio: Decimal = D(1)
    steps: int = 500
    #: Wall-clock seconds one step represents.  The default of one hour makes a
    #: 500-step run about three weeks, which is long enough for carry and basis
    #: drift to matter.  At the simulator's native 0.5s tick a run of this
    #: length would cover four minutes and measure almost nothing.
    step_seconds: Decimal = D(3600)
    #: Rebalance when residual exceeds this, in bps of source notional.
    tolerance_bps: Decimal = D(25)
    #: How often to *check* for a rebalance, in steps.
    rebalance_every: int = 1
    #: Funding is settled every this many steps (8 hourly steps = one interval).
    funding_every: int = 8
    account_currency: str = "USD"
    seed: int = 20260908


@dataclass
class BacktestPoint:
    step: int
    source_price: Decimal
    hedge_price: Decimal
    hedge_quantity: Decimal
    residual_bps: Decimal
    hedge_ratio: Decimal
    net_pnl: Decimal
    cumulative_funding: Decimal
    cumulative_fees: Decimal
    cumulative_slippage: Decimal
    margin_used: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "source_price": str(quantize(self.source_price, 6)),
            "hedge_price": str(quantize(self.hedge_price, 6)),
            "hedge_quantity": str(normalize(self.hedge_quantity)),
            "residual_bps": str(quantize(self.residual_bps, 4)),
            "hedge_ratio": str(quantize(self.hedge_ratio, 6)),
            "net_pnl": str(quantize(self.net_pnl, 4)),
            "cumulative_funding": str(quantize(self.cumulative_funding, 4)),
            "cumulative_fees": str(quantize(self.cumulative_fees, 4)),
            "cumulative_slippage": str(quantize(self.cumulative_slippage, 4)),
            "margin_used": str(quantize(self.margin_used, 2)),
        }


@dataclass
class BacktestReport:
    config: BacktestConfig
    points: list[BacktestPoint] = field(default_factory=list)
    rebalances: int = 0
    funding_settlements: int = 0

    # --- outcome ------------------------------------------------------
    final_net_pnl: Decimal = ZERO
    total_funding: Decimal = ZERO
    total_fees: Decimal = ZERO
    total_slippage: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    #: Drawdown of the *price* P&L only, with carry and costs excluded.
    #: This is the apples-to-apples comparison against the unhedged book:
    #: net P&L declines monotonically under negative carry, so its drawdown
    #: measures the cost of hedging, not the risk that was removed.
    gross_drawdown: Decimal = ZERO
    max_residual_bps: Decimal = ZERO
    mean_residual_bps: Decimal = ZERO
    peak_margin: Decimal = ZERO
    unhedged_pnl: Decimal = ZERO
    unhedged_max_drawdown: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": {
                "source": self.config.source_key, "hedge": self.config.hedge_key,
                "source_quantity": str(self.config.source_quantity),
                "objective": self.config.objective.value,
                "steps": self.config.steps,
            "step_seconds": str(self.config.step_seconds),
            "simulated_days": str(quantize(
                self.config.step_seconds * D(self.config.steps) / D(86400), 3)),
                "tolerance_bps": str(self.config.tolerance_bps),
                "rebalance_every": self.config.rebalance_every,
                "seed": self.config.seed,
            },
            "rebalances": self.rebalances,
            "funding_settlements": self.funding_settlements,
            "final_net_pnl": str(quantize(self.final_net_pnl, 4)),
            "total_funding": str(quantize(self.total_funding, 4)),
            "total_fees": str(quantize(self.total_fees, 4)),
            "total_slippage": str(quantize(self.total_slippage, 4)),
            "max_drawdown": str(quantize(self.max_drawdown, 4)),
            "gross_drawdown": str(quantize(self.gross_drawdown, 4)),
            "max_residual_bps": str(quantize(self.max_residual_bps, 4)),
            "mean_residual_bps": str(quantize(self.mean_residual_bps, 4)),
            "peak_margin": str(quantize(self.peak_margin, 2)),
            "unhedged_pnl": str(quantize(self.unhedged_pnl, 4)),
            "unhedged_max_drawdown": str(quantize(self.unhedged_max_drawdown, 4)),
            "risk_reduction_pct": str(quantize(self.risk_reduction_pct, 2)),
            "points": [p.to_dict() for p in self.points],
        }

    @property
    def risk_reduction_pct(self) -> Decimal:
        """Share of the unhedged price drawdown that the hedge removed.

        Deliberately **not** a profitability claim. It compares price P&L
        against price P&L; carry and execution cost are reported separately,
        and in these runs they are usually negative. A hedge that removes 99%
        of the drawdown while costing money is doing exactly its job.
        """
        if self.unhedged_max_drawdown <= ZERO:
            return ZERO
        removed = self.unhedged_max_drawdown - abs(self.gross_drawdown)
        return safe_div(removed, self.unhedged_max_drawdown) * D(100)

    def summary(self) -> str:
        days = quantize(self.config.step_seconds * D(self.config.steps) / D(86400), 1)
        return (
            f"{self.config.steps} steps (~{days}d), {self.rebalances} rebalance(s): "
            f"net {quantize(self.final_net_pnl, 2)} "
            f"(funding {quantize(self.total_funding, 2)}, fees "
            f"{quantize(self.total_fees, 2)}, slippage "
            f"{quantize(self.total_slippage, 2)}); residual mean "
            f"{quantize(self.mean_residual_bps, 2)} bps, max "
            f"{quantize(self.max_residual_bps, 2)} bps; price drawdown "
            f"{quantize(abs(self.gross_drawdown), 2)} versus "
            f"{quantize(self.unhedged_max_drawdown, 2)} unhedged "
            f"({quantize(self.risk_reduction_pct, 1)}% removed)."
        )


class Backtester:
    """Replays a price path through the real hedge maths."""

    def __init__(
        self,
        instruments: Sequence[InstrumentSpec],
        fx: FxService | None = None,
    ) -> None:
        self.instruments = {spec.key: spec for spec in instruments}
        self.fx = fx or FxService()

    def run(self, config: BacktestConfig) -> BacktestReport:
        source = self.instruments[config.source_key]
        hedge = self.instruments[config.hedge_key]
        simulator = MarketSimulator(
            self.instruments.values(), seed=config.seed,
            tick_seconds=config.step_seconds,
        )
        source_conv, hedge_conv = QuantityConverter(source), QuantityConverter(hedge)

        account = config.account_currency
        source_fx = self.fx.try_rate(source.quote_asset, account)
        hedge_fx = self.fx.try_rate(hedge.quote_asset, account)

        report = BacktestReport(config=config)

        # Open both legs at the first tick.
        source_entry = simulator.ticker(source.key).ask
        hedge_target = self._target_hedge(
            source_conv, hedge_conv, source, hedge, config,
            config.source_quantity, source_entry,
            simulator.ticker(hedge.key).mid, source_fx, hedge_fx,
        )
        hedge_entry = (
            simulator.ticker(hedge.key).bid if hedge_target < ZERO
            else simulator.ticker(hedge.key).ask
        )
        hedge_quantity = hedge_target

        cumulative_fees = self._entry_cost(source, source_conv, config.source_quantity,
                                           source_entry, source_fx)
        cumulative_fees += self._entry_cost(hedge, hedge_conv, hedge_quantity,
                                            hedge_entry, hedge_fx)
        cumulative_slippage = ZERO
        cumulative_funding = ZERO
        peak_pnl = ZERO
        peak_gross = ZERO
        peak_unhedged = ZERO
        max_drawdown = ZERO
        gross_drawdown = ZERO
        unhedged_drawdown = ZERO
        residuals: list[Decimal] = []

        for step in range(1, config.steps + 1):
            simulator.advance(1)
            source_mid = simulator.ticker(source.key).mid
            hedge_mid = simulator.ticker(hedge.key).mid

            source_pnl = self._leg_pnl(source, config.source_quantity,
                                       source_entry, source_mid) * source_fx
            hedge_pnl = self._leg_pnl(hedge, hedge_quantity,
                                      hedge_entry, hedge_mid) * hedge_fx

            if step % config.funding_every == 0:
                # ``_funding`` reports a per-day rate; scale it to the elapsed
                # wall-clock time this settlement actually covers.
                elapsed_days = (
                    config.step_seconds * D(config.funding_every) / D(86400)
                )
                cumulative_funding += self._funding(
                    source, hedge, config.source_quantity, hedge_quantity,
                    source_mid, hedge_mid, simulator, source_fx, hedge_fx,
                ) * elapsed_days
                report.funding_settlements += 1

            net_pnl = source_pnl + hedge_pnl + cumulative_funding - cumulative_fees - cumulative_slippage
            unhedged_pnl = source_pnl

            gross_pnl = source_pnl + hedge_pnl
            peak_pnl = max(peak_pnl, net_pnl)
            peak_gross = max(peak_gross, gross_pnl)
            peak_unhedged = max(peak_unhedged, unhedged_pnl)
            max_drawdown = min(max_drawdown, net_pnl - peak_pnl)
            gross_drawdown = min(gross_drawdown, gross_pnl - peak_gross)
            unhedged_drawdown = min(unhedged_drawdown, unhedged_pnl - peak_unhedged)

            residual_bps = self._residual_bps(
                source_conv, hedge_conv, config.source_quantity, hedge_quantity,
                source_mid, hedge_mid, source_fx, hedge_fx, source_entry, hedge_entry,
            )
            residuals.append(abs(residual_bps))

            # Rebalance on schedule, when out of tolerance.
            if step % config.rebalance_every == 0 and abs(residual_bps) > config.tolerance_bps:
                new_target = self._target_hedge(
                    source_conv, hedge_conv, source, hedge, config,
                    config.source_quantity, source_mid, hedge_mid, source_fx, hedge_fx,
                )
                delta = new_target - hedge_quantity
                rounded = hedge_conv.round_quantity(delta).rounded
                if rounded != ZERO:
                    trade_price = (
                        simulator.ticker(hedge.key).ask if rounded > ZERO
                        else simulator.ticker(hedge.key).bid
                    )
                    cumulative_fees += self._entry_cost(
                        hedge, hedge_conv, rounded, trade_price, hedge_fx
                    )
                    cumulative_slippage += abs(
                        hedge_conv.quote_delta(rounded, hedge_mid)
                    ) * abs(trade_price - hedge_mid) * hedge_fx
                    # Blend the new trade into the weighted entry price.
                    total = hedge_quantity + rounded
                    if total != ZERO:
                        hedge_entry = (
                            hedge_entry * hedge_quantity + trade_price * rounded
                        ) / total
                    hedge_quantity = total
                    report.rebalances += 1

            margin = (
                abs(source_conv.notional_quote(config.source_quantity, source_mid))
                * source_fx * source.effective_initial_margin_rate
                + abs(hedge_conv.notional_quote(hedge_quantity, hedge_mid))
                * hedge_fx * hedge.effective_initial_margin_rate
            )
            report.peak_margin = max(report.peak_margin, margin)

            report.points.append(BacktestPoint(
                step=step, source_price=source_mid, hedge_price=hedge_mid,
                hedge_quantity=hedge_quantity, residual_bps=residual_bps,
                hedge_ratio=self._ratio(source_conv, hedge_conv, config.source_quantity,
                                        hedge_quantity, source_mid, hedge_mid,
                                        source_fx, hedge_fx, source_entry, hedge_entry),
                net_pnl=net_pnl, cumulative_funding=cumulative_funding,
                cumulative_fees=cumulative_fees, cumulative_slippage=cumulative_slippage,
                margin_used=margin,
            ))

        last = report.points[-1] if report.points else None
        report.final_net_pnl = last.net_pnl if last else ZERO
        report.total_funding = cumulative_funding
        report.total_fees = cumulative_fees
        report.total_slippage = cumulative_slippage
        report.max_drawdown = max_drawdown
        report.gross_drawdown = gross_drawdown
        report.unhedged_max_drawdown = abs(unhedged_drawdown)
        report.unhedged_pnl = (
            self._leg_pnl(source, config.source_quantity, source_entry,
                          simulator.ticker(source.key).mid) * source_fx
        )
        report.max_residual_bps = max(residuals) if residuals else ZERO
        report.mean_residual_bps = (
            sum(residuals, ZERO) / D(len(residuals)) if residuals else ZERO
        )
        log.info("backtest complete", extra={"summary": report.summary()})
        return report

    # ------------------------------------------------------------------
    def _target_hedge(
        self,
        source_conv: QuantityConverter,
        hedge_conv: QuantityConverter,
        source: InstrumentSpec,
        hedge: InstrumentSpec,
        config: BacktestConfig,
        source_quantity: Decimal,
        source_price: Decimal,
        hedge_price: Decimal,
        source_fx: Decimal,
        hedge_fx: Decimal,
    ) -> Decimal:
        source_exposure = source_conv.exposure(
            source_quantity, source_price, account_currency=config.account_currency,
            fx_rate=source_fx,
        )
        unit = hedge_conv.exposure(
            D(1), hedge_price, account_currency=config.account_currency, fx_rate=hedge_fx
        )
        from ..hedge.objectives import RiskParameters, resolve_objective

        result = resolve_objective(
            config.objective, source_exposure=source_exposure,
            hedge_unit_exposure=unit, target_ratio=config.target_ratio,
            params=RiskParameters(),
        )
        return hedge_conv.round_quantity(result.quantity).rounded

    @staticmethod
    def _leg_pnl(
        spec: InstrumentSpec, quantity: Decimal, entry: Decimal, mark: Decimal
    ) -> Decimal:
        if quantity == ZERO or entry <= ZERO:
            return ZERO
        if spec.is_inverse:
            notional = quantity * spec.units_per_quantity
            return notional * (D(1) / entry - D(1) / mark) * mark
        return (mark - entry) * quantity * spec.units_per_quantity

    @staticmethod
    def _entry_cost(
        spec: InstrumentSpec, converter: QuantityConverter, quantity: Decimal,
        price: Decimal, fx_rate: Decimal,
    ) -> Decimal:
        from ..domain.numeric import from_bps

        if quantity == ZERO:
            return ZERO
        notional = abs(converter.notional_quote(quantity, price))
        return notional * from_bps(spec.taker_fee_bps) * fx_rate

    def _funding(
        self,
        source: InstrumentSpec,
        hedge: InstrumentSpec,
        source_quantity: Decimal,
        hedge_quantity: Decimal,
        source_mid: Decimal,
        hedge_mid: Decimal,
        simulator: MarketSimulator,
        source_fx: Decimal,
        hedge_fx: Decimal,
    ) -> Decimal:
        from ..costs.funding import project_funding

        projection = project_funding(
            source_spec=source, hedge_spec=hedge,
            source_quantity=source_quantity, hedge_quantity=hedge_quantity,
            source_price=source_mid, hedge_price=hedge_mid,
            source_funding_rate=simulator.ticker(source.key).funding_rate,
            hedge_funding_rate=simulator.ticker(hedge.key).funding_rate,
            source_fx=source_fx, hedge_fx=hedge_fx,
        )
        return projection.net_per_day

    def _residual_bps(
        self,
        source_conv: QuantityConverter,
        hedge_conv: QuantityConverter,
        source_quantity: Decimal,
        hedge_quantity: Decimal,
        source_mid: Decimal,
        hedge_mid: Decimal,
        source_fx: Decimal,
        hedge_fx: Decimal,
        source_entry: Decimal,
        hedge_entry: Decimal,
    ) -> Decimal:
        source_exposure = source_conv.exposure(
            source_quantity, source_mid, account_currency="", fx_rate=source_fx,
            entry_price=source_entry,
        )
        hedge_exposure = hedge_conv.exposure(
            hedge_quantity, hedge_mid, account_currency="", fx_rate=hedge_fx,
            entry_price=hedge_entry,
        )
        residual = abs(
            source_exposure.account_delta + hedge_exposure.account_delta
        ) * hedge_mid
        return safe_div(residual, abs(source_exposure.notional_account)) * D(10000)

    def _ratio(
        self,
        source_conv: QuantityConverter,
        hedge_conv: QuantityConverter,
        source_quantity: Decimal,
        hedge_quantity: Decimal,
        source_mid: Decimal,
        hedge_mid: Decimal,
        source_fx: Decimal,
        hedge_fx: Decimal,
        source_entry: Decimal,
        hedge_entry: Decimal,
    ) -> Decimal:
        source_exposure = source_conv.exposure(
            source_quantity, source_mid, account_currency="", fx_rate=source_fx,
            entry_price=source_entry,
        )
        hedge_exposure = hedge_conv.exposure(
            hedge_quantity, hedge_mid, account_currency="", fx_rate=hedge_fx,
            entry_price=hedge_entry,
        )
        return safe_div(-hedge_exposure.account_delta, source_exposure.account_delta)
