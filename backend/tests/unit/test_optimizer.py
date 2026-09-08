"""Hedge optimiser: lattice search and weighted scoring."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.conftest import make_cfd, make_perp

from hedgelab.domain.account import AccountSnapshot
from hedgelab.domain.enums import HedgeObjective, OptimizerMode
from hedgelab.domain.market import OrderBook, OrderBookLevel, Ticker
from hedgelab.hedge.calculator import HedgeCalculator, HedgeInputs
from hedgelab.hedge.optimizer import (
    PRIORITIES,
    HedgeOptimizer,
    OptimizerConfig,
    OptimizerWeights,
)
from hedgelab.marketdata.fx import FxService

D = Decimal


def _ticker(venue: str, symbol: str, mid: str, spread_bps: str = "4") -> Ticker:
    m = D(mid)
    half = m * D(spread_bps) / D(20000)
    return Ticker(
        venue=venue, symbol=symbol, bid=m - half, ask=m + half, last=m,
        volume=D("1000"), timestamp=datetime.now(UTC), funding_rate=D("0.0001"),
        bid_size=D("500"), ask_size=D("500"),
    )


def _book(venue: str, symbol: str, mid: str) -> OrderBook:
    m = D(mid)
    return OrderBook(
        venue=venue, symbol=symbol,
        bids=tuple(OrderBookLevel(m - D(i + 1), D("50")) for i in range(10)),
        asks=tuple(OrderBookLevel(m + D(i + 1), D("50")) for i in range(10)),
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def setup() -> tuple[HedgeOptimizer, HedgeInputs]:
    source = make_perp(contract_size="0.01")           # 1 contract = 0.01 units
    hedge = make_cfd(units_per_lot="10", quantity_step="0.01", min_quantity="0.01")
    calculator = HedgeCalculator(FxService())
    account = AccountSnapshot.build(
        venue="PAPER_MT5", currency="USD", balance=D("500000"),
        used_margin=D("0"), maintenance_margin=D("0"), unrealized_pnl=D("0"),
    )
    inputs = HedgeInputs(
        source_spec=source, hedge_spec=hedge,
        source_quantity=D("3737"),                     # 37.37 units -> 3.737 lots
        source_ticker=_ticker(source.venue, source.symbol, "4000"),
        hedge_ticker=_ticker(hedge.venue, hedge.symbol, "4000", spread_bps="8"),
        objective=HedgeObjective.BASE_ASSET_NEUTRAL,
        hedge_book=_book(hedge.venue, hedge.symbol, "4000"),
        hedge_account=account, account_currency="USD",
    )
    return HedgeOptimizer(calculator), inputs


def test_exact_mode_just_rounds(setup) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(inputs, OptimizerConfig(mode=OptimizerMode.EXACT))
    assert result.exact_quantity == D("-3.737")
    assert result.best.quantity == D("-3.73")     # toward zero
    assert len(result.candidates) == 1
    assert "rounded toward zero" in result.rationale


def test_step_search_beats_naive_rounding_on_residual(setup) -> None:
    """The point of the optimiser: -3.74 is closer than -3.73."""
    optimizer, inputs = setup
    exact = optimizer.optimize(inputs, OptimizerConfig(mode=OptimizerMode.EXACT))
    searched = optimizer.optimize(
        inputs, OptimizerConfig(mode=OptimizerMode.STEP_SEARCH, priority="MIN_RESIDUAL",
                                search_steps=4)
    )
    assert searched.best.quantity == D("-3.74")
    assert searched.best.residual_bps < exact.best.residual_bps


def test_min_cost_prefers_a_smaller_hedge(setup) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(
        inputs, OptimizerConfig(mode=OptimizerMode.STEP_SEARCH, priority="MIN_COST",
                                search_steps=4)
    )
    assert abs(result.best.quantity) < D("3.737")


def test_min_margin_prefers_a_smaller_hedge(setup) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(
        inputs, OptimizerConfig(mode=OptimizerMode.STEP_SEARCH, priority="MIN_MARGIN",
                                search_steps=4)
    )
    smallest = min(result.candidates, key=lambda c: abs(c.quantity))
    assert result.best.margin <= smallest.margin + D("0.01")


@pytest.mark.parametrize("priority", PRIORITIES)
def test_every_priority_returns_an_executable_candidate(setup, priority: str) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(
        inputs, OptimizerConfig(mode=OptimizerMode.STEP_SEARCH, priority=priority,
                                search_steps=3)
    )
    assert result.best.is_executable
    assert result.best.quantity != D("0")
    assert result.calculation.rounded_quantity == result.best.quantity


def test_unknown_priority_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown priority"):
        OptimizerConfig(priority="MAXIMISE_VIBES")


def test_negative_search_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="search_steps"):
        OptimizerConfig(search_steps=-1)


def test_weighted_mode_scores_every_candidate(setup) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(
        inputs, OptimizerConfig(mode=OptimizerMode.WEIGHTED, search_steps=3)
    )
    assert len(result.candidates) == 7
    assert all(c.score >= D("0") for c in result.candidates)
    assert result.best.score == min(c.score for c in result.candidates)


def test_weights_change_the_answer(setup) -> None:
    optimizer, inputs = setup
    residual_first = optimizer.optimize(inputs, OptimizerConfig(
        mode=OptimizerMode.WEIGHTED, search_steps=4,
        weights=OptimizerWeights(residual_exposure=D("100"), execution_cost=D("0"),
                                 funding_benefit=D("0"), margin_usage=D("0"),
                                 liquidation_risk=D("0"), slippage=D("0"),
                                 capital_efficiency=D("0")),
    ))
    cost_first = optimizer.optimize(inputs, OptimizerConfig(
        mode=OptimizerMode.WEIGHTED, search_steps=4,
        weights=OptimizerWeights(residual_exposure=D("0"), execution_cost=D("100"),
                                 funding_benefit=D("0"), margin_usage=D("0"),
                                 liquidation_risk=D("0"), slippage=D("0"),
                                 capital_efficiency=D("0")),
    ))
    assert residual_first.best.quantity == D("-3.74")
    assert abs(cost_first.best.quantity) < abs(residual_first.best.quantity)


def test_residual_cap_filters_candidates(setup) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(inputs, OptimizerConfig(
        mode=OptimizerMode.STEP_SEARCH, priority="MIN_COST", search_steps=5,
        max_residual_bps=D("20"),
    ))
    assert result.best.residual_bps <= D("20")


def test_impossible_residual_cap_falls_back_rather_than_failing(setup) -> None:
    optimizer, inputs = setup
    result = optimizer.optimize(inputs, OptimizerConfig(
        mode=OptimizerMode.STEP_SEARCH, priority="MIN_RESIDUAL", search_steps=2,
        max_residual_bps=D("0.0000001"),
    ))
    assert result.best.quantity != D("0")   # still returns a usable answer


def test_step_search_matches_brute_force_on_a_small_grid(setup) -> None:
    """Cross-check the search against an exhaustive evaluation."""
    optimizer, inputs = setup
    calculator = optimizer.calculator
    step = inputs.hedge_spec.quantity_step
    anchor = calculator.calculate(inputs).rounded_quantity

    brute: list[tuple[Decimal, Decimal]] = []
    for offset in range(-3, 4):
        quantity = anchor + step * D(offset)
        if quantity == 0:
            continue
        result = calculator.calculate_for_quantity(inputs, quantity)
        brute.append((abs(result.residual_bps), quantity))
    expected = min(brute)[1]

    searched = optimizer.optimize(inputs, OptimizerConfig(
        mode=OptimizerMode.STEP_SEARCH, priority="MIN_RESIDUAL", search_steps=3
    ))
    assert searched.best.quantity == expected


def test_result_serialises(setup) -> None:
    optimizer, inputs = setup
    payload = optimizer.optimize(
        inputs, OptimizerConfig(mode=OptimizerMode.WEIGHTED, search_steps=2)
    ).to_dict()
    assert payload["mode"] == "WEIGHTED"
    assert len(payload["candidates"]) == 5
    assert "rationale" in payload


def test_optimizer_never_exceeds_the_margin_safe_maximum(setup) -> None:
    optimizer, inputs = setup
    tiny_account = AccountSnapshot.build(
        venue="PAPER_MT5", currency="USD", balance=D("2000"),
        used_margin=D("0"), maintenance_margin=D("0"), unrealized_pnl=D("0"),
    )
    inputs = replace(inputs, hedge_account=tiny_account)
    result = optimizer.optimize(inputs, OptimizerConfig(
        mode=OptimizerMode.STEP_SEARCH, priority="MIN_RESIDUAL", search_steps=4
    ))
    assert abs(result.best.quantity) <= result.calculation.max_safe_quantity
