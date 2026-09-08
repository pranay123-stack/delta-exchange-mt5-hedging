"""Hedge quantity optimiser.

The exact objective solution almost never lands on the venue's quantity
lattice.  Rounding it is one choice; searching the neighbouring lattice points
for the one that best serves the desk's actual priorities is another.

Three modes:

* ``EXACT``       -- take the objective's answer, round toward zero.  Fastest,
  and correct when residual exposure is the only thing that matters.
* ``STEP_SEARCH`` -- evaluate every lattice point within a window and pick the
  best on a single named priority.
* ``WEIGHTED``    -- evaluate the same window and score each candidate on a
  normalised weighted sum of every priority.

Scoring is on **normalised** metrics (each divided by the worst value in the
candidate set), so weights are comparable across metrics with wildly different
units -- basis points of residual against dollars of margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.enums import OptimizerMode
from ..domain.numeric import ZERO, normalize, quantize, safe_div
from ..domain.quantity import QuantityConverter
from ..logging_setup import get_logger
from .calculator import HedgeCalculation, HedgeCalculator, HedgeInputs

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OptimizerWeights:
    """Relative importance of each priority.  Zero disables a term.

    Defaults encode "residual exposure dominates, cost matters, everything
    else breaks ties" -- the sane default for a hedging desk.
    """

    residual_exposure: Decimal = Decimal("10")
    execution_cost: Decimal = Decimal("3")
    funding_benefit: Decimal = Decimal("2")
    margin_usage: Decimal = Decimal("1")
    liquidation_risk: Decimal = Decimal("1")
    slippage: Decimal = Decimal("1")
    capital_efficiency: Decimal = Decimal("1")

    def total(self) -> Decimal:
        return (
            self.residual_exposure + self.execution_cost + self.funding_benefit
            + self.margin_usage + self.liquidation_risk + self.slippage
            + self.capital_efficiency
        )


#: Single-priority modes available to ``STEP_SEARCH``.
PRIORITIES = (
    "MIN_RESIDUAL",
    "MIN_COST",
    "MAX_FUNDING_BENEFIT",
    "MIN_MARGIN",
    "MIN_LIQUIDATION_RISK",
    "MIN_SLIPPAGE",
    "MAX_CAPITAL_EFFICIENCY",
)


@dataclass(frozen=True, slots=True)
class OptimizerConfig:
    mode: OptimizerMode = OptimizerMode.EXACT
    #: Lattice points to search on each side of the exact solution.
    search_steps: int = 6
    priority: str = "MIN_RESIDUAL"
    weights: OptimizerWeights = OptimizerWeights()
    #: Reject candidates whose residual exceeds this, when any candidate passes.
    max_residual_bps: Decimal | None = None

    def __post_init__(self) -> None:
        if self.priority not in PRIORITIES:
            raise ValueError(f"unknown priority {self.priority!r}; choose from {PRIORITIES}")
        if self.search_steps < 0:
            raise ValueError("search_steps must be >= 0")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One lattice point and the metrics it scores on."""

    quantity: Decimal
    residual_bps: Decimal
    execution_cost: Decimal
    funding_per_day: Decimal
    margin: Decimal
    slippage: Decimal
    liquidation_risk: Decimal
    capital_efficiency: Decimal
    score: Decimal = ZERO
    is_executable: bool = True
    calculation: HedgeCalculation | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "quantity": str(normalize(self.quantity)),
            "residual_bps": str(quantize(self.residual_bps, 4)),
            "execution_cost": str(quantize(self.execution_cost, 4)),
            "funding_per_day": str(quantize(self.funding_per_day, 4)),
            "margin": str(quantize(self.margin, 2)),
            "slippage": str(quantize(self.slippage, 4)),
            "liquidation_risk": str(quantize(self.liquidation_risk, 6)),
            "capital_efficiency": str(quantize(self.capital_efficiency, 6)),
            "score": str(quantize(self.score, 6)),
            "is_executable": self.is_executable,
        }


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    best: Candidate
    candidates: tuple[Candidate, ...]
    mode: OptimizerMode
    priority: str
    exact_quantity: Decimal
    rationale: str

    @property
    def calculation(self) -> HedgeCalculation:
        assert self.best.calculation is not None  # always set by the optimizer
        return self.best.calculation

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "priority": self.priority,
            "exact_quantity": str(normalize(self.exact_quantity)),
            "chosen_quantity": str(normalize(self.best.quantity)),
            "rationale": self.rationale,
            "candidates": [c.to_dict() for c in self.candidates],
        }


class HedgeOptimizer:
    """Searches the quantity lattice for the best hedge."""

    def __init__(self, calculator: HedgeCalculator) -> None:
        self.calculator = calculator

    def optimize(self, inputs: HedgeInputs, config: OptimizerConfig) -> OptimizationResult:
        base = self.calculator.calculate(inputs)
        exact = base.required_quantity

        if config.mode is OptimizerMode.EXACT:
            candidate = self._score_one(base)
            return OptimizationResult(
                best=candidate,
                candidates=(candidate,),
                mode=config.mode,
                priority="EXACT",
                exact_quantity=exact,
                rationale=(
                    f"exact objective solution {normalize(exact)} rounded toward zero to "
                    f"{normalize(base.rounded_quantity)} on a "
                    f"{normalize(inputs.hedge_spec.quantity_step)} step"
                ),
            )

        candidates = self._build_candidates(inputs, base, config)
        if not candidates:
            candidate = self._score_one(base)
            return OptimizationResult(
                best=candidate,
                candidates=(candidate,),
                mode=config.mode,
                priority=config.priority,
                exact_quantity=exact,
                rationale=(
                    "no executable lattice point found in the search window; "
                    "using the rounded solution"
                ),
            )

        viable = [c for c in candidates if c.is_executable] or candidates
        if config.max_residual_bps is not None:
            filtered = [c for c in viable if c.residual_bps <= config.max_residual_bps]
            if filtered:
                viable = filtered

        if config.mode is OptimizerMode.STEP_SEARCH:
            best = self._pick_by_priority(viable, config.priority)
            rationale = (
                f"step search over {len(candidates)} lattice points "
                f"(+/-{config.search_steps} steps) selected {normalize(best.quantity)} "
                f"by {config.priority}"
            )
            scored = tuple(candidates)
        else:
            scored = self._score_weighted(candidates, config.weights)
            viable_scored = [c for c in scored if c.is_executable] or list(scored)
            if config.max_residual_bps is not None:
                limited = [c for c in viable_scored if c.residual_bps <= config.max_residual_bps]
                if limited:
                    viable_scored = limited
            best = min(viable_scored, key=lambda c: c.score)
            rationale = (
                f"weighted score over {len(scored)} lattice points selected "
                f"{normalize(best.quantity)} (score {quantize(best.score, 6)}); "
                f"weights residual={config.weights.residual_exposure}, "
                f"cost={config.weights.execution_cost}, funding={config.weights.funding_benefit}, "
                f"margin={config.weights.margin_usage}, liq={config.weights.liquidation_risk}, "
                f"slip={config.weights.slippage}, capital={config.weights.capital_efficiency}"
            )

        log.info(
            "hedge optimised",
            extra={
                "mode": config.mode.value, "priority": config.priority,
                "exact": str(normalize(exact)), "chosen": str(normalize(best.quantity)),
                "candidates": len(scored),
            },
        )
        return OptimizationResult(
            best=best,
            candidates=tuple(sorted(scored, key=lambda c: c.quantity)),
            mode=config.mode,
            priority=config.priority,
            exact_quantity=exact,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    def _build_candidates(
        self, inputs: HedgeInputs, base: HedgeCalculation, config: OptimizerConfig
    ) -> list[Candidate]:
        spec = inputs.hedge_spec
        converter = QuantityConverter(spec)
        step = spec.quantity_step
        anchor = base.rounded_quantity
        seen: set[Decimal] = set()
        candidates: list[Candidate] = []

        for offset in range(-config.search_steps, config.search_steps + 1):
            raw = anchor + step * Decimal(offset)
            rounded = converter.round_quantity(raw).rounded
            if rounded in seen:
                continue
            seen.add(rounded)
            if rounded == ZERO and anchor != ZERO:
                continue  # not hedging at all is handled by the PARTIAL objective
            trial = HedgeInputs(
                source_spec=inputs.source_spec,
                hedge_spec=spec,
                source_quantity=inputs.source_quantity,
                source_ticker=inputs.source_ticker,
                hedge_ticker=inputs.hedge_ticker,
                objective=inputs.objective,
                target_ratio=inputs.target_ratio,
                current_hedge_quantity=inputs.current_hedge_quantity,
                source_entry_price=inputs.source_entry_price,
                hedge_entry_price=inputs.hedge_entry_price,
                hedge_book=inputs.hedge_book,
                hedge_account=inputs.hedge_account,
                risk_params=inputs.risk_params,
                account_currency=inputs.account_currency,
            )
            calc = self.calculator.calculate_for_quantity(trial, rounded)
            candidates.append(self._score_one(calc))
        return candidates

    def _score_one(self, calc: HedgeCalculation) -> Candidate:
        return Candidate(
            quantity=calc.rounded_quantity,
            residual_bps=abs(calc.residual_bps),
            execution_cost=calc.total_execution_cost,
            funding_per_day=calc.funding_impact_per_day,
            margin=calc.margin_requirement,
            slippage=calc.estimated_slippage,
            liquidation_risk=self._liquidation_risk(calc),
            capital_efficiency=self._capital_efficiency(calc),
            is_executable=calc.is_executable,
            calculation=calc,
        )

    @staticmethod
    def _liquidation_risk(calc: HedgeCalculation) -> Decimal:
        """Margin consumed per unit of notional actually hedged.

        A hedge that eats margin without removing exposure is the dangerous
        kind; this ratio penalises exactly that.
        """
        hedged = abs(calc.hedge_exposure.notional_account)
        if hedged <= ZERO:
            return Decimal(1)
        return safe_div(calc.margin_requirement, hedged)

    @staticmethod
    def _capital_efficiency(calc: HedgeCalculation) -> Decimal:
        """Exposure neutralised per unit of margin consumed.  Higher is better."""
        if calc.margin_requirement <= ZERO:
            return ZERO
        source_notional = abs(calc.source_exposure.notional_account)
        neutralised = source_notional - calc.residual_value
        return safe_div(max(neutralised, ZERO), calc.margin_requirement)

    @staticmethod
    def _pick_by_priority(candidates: list[Candidate], priority: str) -> Candidate:
        keys = {
            "MIN_RESIDUAL": (lambda c: (c.residual_bps, c.execution_cost), False),
            "MIN_COST": (lambda c: (c.execution_cost, c.residual_bps), False),
            "MAX_FUNDING_BENEFIT": (lambda c: (c.funding_per_day, -c.residual_bps), True),
            "MIN_MARGIN": (lambda c: (c.margin, c.residual_bps), False),
            "MIN_LIQUIDATION_RISK": (lambda c: (c.liquidation_risk, c.residual_bps), False),
            "MIN_SLIPPAGE": (lambda c: (c.slippage, c.residual_bps), False),
            "MAX_CAPITAL_EFFICIENCY": (lambda c: (c.capital_efficiency, -c.residual_bps), True),
        }
        key, maximise = keys[priority]
        return max(candidates, key=key) if maximise else min(candidates, key=key)

    @staticmethod
    def _score_weighted(
        candidates: list[Candidate], weights: OptimizerWeights
    ) -> tuple[Candidate, ...]:
        """Normalise every metric to [0, 1] then combine.  Lower score is better.

        Normalisation is against the observed range in the candidate set, so a
        metric where every candidate is identical contributes nothing rather
        than dominating by scale.
        """
        def spread(values: list[Decimal]) -> tuple[Decimal, Decimal]:
            lo, hi = min(values), max(values)
            return lo, (hi - lo)

        residual_lo, residual_range = spread([c.residual_bps for c in candidates])
        cost_lo, cost_range = spread([c.execution_cost for c in candidates])
        funding_lo, funding_range = spread([c.funding_per_day for c in candidates])
        margin_lo, margin_range = spread([c.margin for c in candidates])
        liq_lo, liq_range = spread([c.liquidation_risk for c in candidates])
        slip_lo, slip_range = spread([c.slippage for c in candidates])
        cap_lo, cap_range = spread([c.capital_efficiency for c in candidates])

        def norm(value: Decimal, lo: Decimal, rng: Decimal) -> Decimal:
            return ZERO if rng <= ZERO else (value - lo) / rng

        total_weight = weights.total() or Decimal(1)
        scored: list[Candidate] = []
        for c in candidates:
            score = (
                weights.residual_exposure * norm(c.residual_bps, residual_lo, residual_range)
                + weights.execution_cost * norm(c.execution_cost, cost_lo, cost_range)
                # Funding and capital efficiency are "higher is better": invert.
                + weights.funding_benefit * (Decimal(1) - norm(c.funding_per_day, funding_lo, funding_range))
                + weights.margin_usage * norm(c.margin, margin_lo, margin_range)
                + weights.liquidation_risk * norm(c.liquidation_risk, liq_lo, liq_range)
                + weights.slippage * norm(c.slippage, slip_lo, slip_range)
                + weights.capital_efficiency * (Decimal(1) - norm(c.capital_efficiency, cap_lo, cap_range))
            ) / total_weight
            scored.append(
                Candidate(
                    quantity=c.quantity, residual_bps=c.residual_bps,
                    execution_cost=c.execution_cost, funding_per_day=c.funding_per_day,
                    margin=c.margin, slippage=c.slippage,
                    liquidation_risk=c.liquidation_risk,
                    capital_efficiency=c.capital_efficiency,
                    score=score, is_executable=c.is_executable, calculation=c.calculation,
                )
            )
        return tuple(scored)
