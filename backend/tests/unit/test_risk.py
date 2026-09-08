"""Margin, liquidation, per-pair risk thresholds and portfolio aggregation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.conftest import approx_dec, make_cfd, make_perp

from hedgelab.domain.account import AccountSnapshot
from hedgelab.domain.enums import EmergencyAction, RiskLevel
from hedgelab.domain.market import Ticker
from hedgelab.domain.orders import Position
from hedgelab.marketdata.fx import FxService
from hedgelab.risk.engine import PairRisk, PairRiskInputs, RiskEngine, RiskThresholds
from hedgelab.risk.margin import (
    equity_at_price,
    liquidation_price,
    margin_level,
    margin_requirement,
)
from hedgelab.risk.portfolio import PortfolioLimits, PortfolioRiskEngine

D = Decimal


def _ticker(venue: str, symbol: str, mid: str, spread_bps: str = "4",
            stale: bool = False) -> Ticker:
    m = D(mid)
    half = m * D(spread_bps) / D(20000)
    return Ticker(venue=venue, symbol=symbol, bid=m - half, ask=m + half, last=m,
                  volume=D("1"), timestamp=datetime.now(UTC), funding_rate=D("0.0001"),
                  bid_size=D("100"), ask_size=D("100"), is_stale=stale)


def _account(venue: str, equity: str, used: str) -> AccountSnapshot:
    return AccountSnapshot.build(
        venue=venue, currency="USD", balance=D(equity), used_margin=D(used),
        maintenance_margin=D(used) / 2, unrealized_pnl=D("0"),
    )


# ======================================================================
# margin
# ======================================================================
def test_margin_is_notional_times_rate() -> None:
    spec = make_cfd(units_per_lot="1", initial_margin_rate="0.01",
                    maintenance_margin_rate="0.005")
    requirement = margin_requirement(spec, D("3"), D("100000"))
    assert requirement.notional == D("300000")
    assert requirement.initial == D("3000")
    assert requirement.maintenance == D("1500")
    assert requirement.leverage_used == D("100")


def test_margin_uses_the_fx_rate() -> None:
    spec = make_cfd(units_per_lot="1", quote="EUR")
    requirement = margin_requirement(spec, D("1"), D("100000"), fx_rate=D("1.1"))
    assert requirement.notional == D("110000")


def test_margin_level_is_infinite_when_flat() -> None:
    assert margin_level(D("100000"), D("0")) == D("999999")


def test_margin_level_formula() -> None:
    assert margin_level(D("15000"), D("10000")) == D("150")


# ======================================================================
# liquidation
# ======================================================================
def test_linear_long_liquidation_matches_closed_form() -> None:
    spec = make_cfd(units_per_lot="1", initial_margin_rate="0.01",
                    maintenance_margin_rate="0.005")
    entry = D("100000")
    posted = entry * D("0.01")            # isolated margin on 1 lot
    estimate = liquidation_price(spec, D("1"), entry, posted, entry)
    expected = entry * (D("1") - D("0.01")) / (D("1") - D("0.005"))
    assert approx_dec(estimate.liquidation_price, expected, tol="0.01")
    assert estimate.distance_pct < 0
    assert estimate.is_liquidatable is False


def test_linear_short_liquidation_matches_closed_form() -> None:
    spec = make_cfd(units_per_lot="1", initial_margin_rate="0.01",
                    maintenance_margin_rate="0.005")
    entry = D("100000")
    posted = entry * D("0.01")
    estimate = liquidation_price(spec, D("-1"), entry, posted, entry)
    expected = entry * (D("1") + D("0.01")) / (D("1") + D("0.005"))
    assert approx_dec(estimate.liquidation_price, expected, tol="0.01")
    assert estimate.distance_pct > 0


def test_inverse_liquidation_is_asymmetric() -> None:
    """Inverse contracts are convex: the long and short distances differ."""
    spec = make_perp(inverse=True, contract_size="1", quote="USD", base="BTC",
                     initial_margin_rate="0.02", maintenance_margin_rate="0.005")
    entry = D("100000")
    equity_base = D("0.02")               # 2% of 100k USD notional, held in BTC
    long_est = liquidation_price(spec, D("100000"), entry, equity_base, entry)
    short_est = liquidation_price(spec, D("-100000"), entry, equity_base, entry)
    assert long_est.liquidation_price < entry < short_est.liquidation_price
    assert long_est.max_tolerable_move != short_est.max_tolerable_move


def test_flat_position_has_no_liquidation_price() -> None:
    spec = make_cfd()
    estimate = liquidation_price(spec, D("0"), D("100"), D("100"), D("100"))
    assert estimate.liquidation_price is None
    assert estimate.is_safe is True


def test_fully_funded_position_cannot_be_liquidated() -> None:
    """Equity above the notional means no adverse move liquidates you."""
    spec = make_cfd(units_per_lot="1")
    estimate = liquidation_price(spec, D("1"), D("100000"), D("500000"), D("100000"))
    assert estimate.liquidation_price is None
    assert "no liquidation price" in estimate.note


def test_breached_liquidation_is_detected() -> None:
    spec = make_cfd(units_per_lot="1", initial_margin_rate="0.01",
                    maintenance_margin_rate="0.005")
    estimate = liquidation_price(spec, D("1"), D("100000"), D("1000"), D("50000"))
    assert estimate.is_liquidatable is True


def test_equity_at_price_is_linear_for_linear_instruments() -> None:
    spec = make_cfd(units_per_lot="1")
    base = equity_at_price(spec, D("2"), D("100"), D("1000"), D("100"))
    up = equity_at_price(spec, D("2"), D("100"), D("1000"), D("110"))
    assert base == D("1000")
    assert up == D("1020")


def test_equity_at_price_handles_inverse_curvature() -> None:
    spec = make_perp(inverse=True, contract_size="1")
    up = equity_at_price(spec, D("100000"), D("100000"), D("0"), D("110000"))
    down = equity_at_price(spec, D("100000"), D("100000"), D("0"), D("90000"))
    # Gains and losses are not symmetric for an inverse contract.
    assert abs(up) != abs(down)
    assert up > 0 > down


# ======================================================================
# pair risk
# ======================================================================
def _pair_inputs(
    *, source_qty: str = "5000", hedge_qty: str = "-5",
    source_equity: str = "250000", source_used: str = "10000",
    hedge_equity: str = "250000", hedge_used: str = "5000",
    daily_pnl: str = "0", source_stale: bool = False, spread_bps: str = "4",
    source_mid: str = "100000", hedge_mid: str = "100000",
) -> PairRiskInputs:
    source = make_perp(contract_size="0.001", base="BTC")
    hedge = make_cfd(units_per_lot="1", base="BTC")
    return PairRiskInputs(
        pair_name="test pair", source_spec=source, hedge_spec=hedge,
        source_position=Position(venue=source.venue, symbol=source.symbol,
                                 quantity=D(source_qty), average_entry=D(source_mid)),
        hedge_position=Position(venue=hedge.venue, symbol=hedge.symbol,
                                quantity=D(hedge_qty), average_entry=D(hedge_mid)),
        source_ticker=_ticker(source.venue, source.symbol, source_mid,
                              spread_bps, source_stale),
        hedge_ticker=_ticker(hedge.venue, hedge.symbol, hedge_mid, spread_bps),
        source_account=_account(source.venue, source_equity, source_used),
        hedge_account=_account(hedge.venue, hedge_equity, hedge_used),
        daily_pnl=D(daily_pnl), account_currency="USD",
    )


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(FxService(), RiskThresholds())


def test_balanced_pair_is_normal(engine: RiskEngine) -> None:
    risk = engine.evaluate(_pair_inputs())
    assert risk.level is RiskLevel.NORMAL
    assert risk.is_tradable is True
    assert abs(risk.hedge_ratio - D("1")) < D("0.01")


def test_unhedged_pair_breaches_residual(engine: RiskEngine) -> None:
    risk = engine.evaluate(_pair_inputs(hedge_qty="0"))
    assert risk.level is RiskLevel.EMERGENCY
    assert any(b.metric == "residual_exposure_bps" for b in risk.breaches)
    assert risk.is_tradable is False


def test_partial_hedge_breaches_proportionally(engine: RiskEngine) -> None:
    warning = engine.evaluate(_pair_inputs(hedge_qty="-4.995"))
    danger = engine.evaluate(_pair_inputs(hedge_qty="-4.9"))
    assert warning.level.rank < danger.level.rank


def test_low_margin_level_escalates(engine: RiskEngine) -> None:
    for used, expected in (
        ("1300", RiskLevel.WARNING),      # level ~192%
        ("1700", RiskLevel.DANGER),       # level ~147%
        ("2100", RiskLevel.EMERGENCY),    # level ~119%
        ("2600", RiskLevel.KILL_SWITCH),  # level ~96%
    ):
        risk = engine.evaluate(_pair_inputs(hedge_equity="2500", hedge_used=used))
        assert risk.level.rank >= expected.rank, f"used={used} gave {risk.level}"


def test_stale_data_is_a_danger(engine: RiskEngine) -> None:
    risk = engine.evaluate(_pair_inputs(source_stale=True))
    assert risk.level.rank >= RiskLevel.DANGER.rank
    assert any("stale_data" in b.metric for b in risk.breaches)


def test_wide_spread_warns(engine: RiskEngine) -> None:
    risk = engine.evaluate(_pair_inputs(spread_bps="200"))
    assert any(b.metric.startswith("spread") for b in risk.breaches)


def test_basis_is_measured_between_the_legs(engine: RiskEngine) -> None:
    risk = engine.evaluate(_pair_inputs(source_mid="100000", hedge_mid="101000"))
    assert risk.basis_bps < 0                     # source cheaper than hedge
    assert any(b.metric == "basis_bps" for b in risk.breaches)


def test_daily_loss_escalates(engine: RiskEngine) -> None:
    thresholds = RiskThresholds(max_daily_loss=D("10000"))
    engine = RiskEngine(FxService(), thresholds)
    assert engine.evaluate(_pair_inputs(daily_pnl="-7000")).level.rank >= RiskLevel.WARNING.rank
    assert engine.evaluate(_pair_inputs(daily_pnl="-9000")).level.rank >= RiskLevel.DANGER.rank
    assert engine.evaluate(_pair_inputs(daily_pnl="-11000")).level is RiskLevel.KILL_SWITCH


def test_actions_escalate_with_the_level(engine: RiskEngine) -> None:
    normal = engine.evaluate(_pair_inputs())
    assert normal.actions == ()

    emergency = engine.evaluate(_pair_inputs(hedge_qty="0"))
    assert EmergencyAction.STOP_NEW_TRADES in emergency.actions
    assert EmergencyAction.ENTER_EMERGENCY_MODE in emergency.actions

    kill = engine.evaluate(_pair_inputs(hedge_equity="2500", hedge_used="2600"))
    assert EmergencyAction.FLATTEN_POSITIONS in kill.actions


def test_risk_serialises(engine: RiskEngine) -> None:
    payload = engine.evaluate(_pair_inputs()).to_dict()
    for key in ("level", "residual_bps", "hedge_ratio", "total_margin",
                "max_tolerable_move", "basis_bps", "is_tradable"):
        assert key in payload


# ======================================================================
# portfolio risk
# ======================================================================
def _pair_risk(name: str, source_notional: str, residual: str,
               underlying_mark: str = "100000") -> PairRisk:
    from hedgelab.risk.margin import LiquidationEstimate

    return PairRisk(
        pair_name=name, source_key=f"PAPER_DELTA:{name}", hedge_key=f"PAPER_MT5:{name}",
        level=RiskLevel.NORMAL, breaches=(), actions=(),
        source_notional=D(source_notional), hedge_notional=D(source_notional),
        net_notional=D("0"), residual_delta=D(residual), residual_bps=D("1"),
        hedge_ratio=D("1"), source_margin=D("1000"), hedge_margin=D("1000"),
        total_margin=D("2000"), source_margin_level=D("500"), hedge_margin_level=D("500"),
        source_liquidation=None,
        hedge_liquidation=LiquidationEstimate(
            liquidation_price=D("50000"), mark_price=D(underlying_mark),
            distance_absolute=D("-50000"), distance_pct=D("-50"),
            max_tolerable_move=D("0.5"), is_liquidatable=False,
        ),
        max_tolerable_move=D("0.5"), funding_risk_per_day=D("-10"),
        basis_bps=D("2"), liquidity_risk_bps=D("4"),
        unrealized_pnl=D("0"), realized_pnl=D("0"), daily_pnl=D("0"),
    )


def test_portfolio_sums_exposure_across_pairs() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits())
    # Split evenly so the concentration limit (60%) is not the thing under test.
    risks = [_pair_risk("A", "400000", "0"), _pair_risk("B", "400000", "0")]
    accounts = [_account("PAPER_DELTA", "250000", "10000"),
                _account("PAPER_MT5", "250000", "10000")]
    result = engine.evaluate(risks, accounts,
                             underlying_by_pair={"A": "BTC", "B": "ETH"})
    assert result.gross_notional == D("1600000")
    assert result.pair_count == 2
    assert result.level is RiskLevel.NORMAL
    assert result.exposure_by_venue["PAPER_DELTA"] == D("800000")


def test_opposing_residuals_net_off() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits())
    risks = [_pair_risk("A", "500000", "1"), _pair_risk("B", "500000", "-1")]
    result = engine.evaluate(risks, [_account("PAPER_MT5", "250000", "0")],
                             underlying_by_pair={"A": "BTC", "B": "ETH"})
    assert result.net_exposure == D("0")


def test_notional_limit_escalates() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits(max_total_notional=D("1000000")))
    risks = [_pair_risk("A", "600000", "0")]
    result = engine.evaluate(risks, [_account("PAPER_MT5", "250000", "0")],
                             underlying_by_pair={"A": "BTC"})
    assert result.level.rank >= RiskLevel.DANGER.rank
    assert result.allows_new_trades is False


def test_single_pair_is_not_flagged_for_concentration() -> None:
    """One open pair is trivially 100% concentrated; that is not a breach."""
    engine = PortfolioRiskEngine(PortfolioLimits(max_concentration_pct=D("60")))
    result = engine.evaluate([_pair_risk("A", "100000", "0")],
                             [_account("PAPER_MT5", "250000", "0")],
                             underlying_by_pair={"A": "BTC"})
    assert result.concentration_pct == D("100")
    assert not any(b.metric == "concentration" for b in result.breaches)


def test_concentration_is_flagged_across_multiple_underlyings() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits(max_concentration_pct=D("60")))
    risks = [_pair_risk("A", "900000", "0"), _pair_risk("B", "100000", "0")]
    result = engine.evaluate(risks, [_account("PAPER_MT5", "250000", "0")],
                             underlying_by_pair={"A": "BTC", "B": "ETH"})
    assert any(b.metric == "concentration" for b in result.breaches)
    # Capped at DANGER: a concentrated book is not a liquidation event.
    assert result.level.rank <= RiskLevel.DANGER.rank


def test_a_pair_in_emergency_escalates_the_portfolio() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits())
    risk = _pair_risk("A", "100000", "0")
    escalated = replace(risk, level=RiskLevel.EMERGENCY)
    result = engine.evaluate([escalated], [_account("PAPER_MT5", "250000", "0")],
                             underlying_by_pair={"A": "BTC"})
    assert result.level.rank >= RiskLevel.EMERGENCY.rank
    assert EmergencyAction.ENTER_EMERGENCY_MODE in result.actions


def test_currency_exposure_limit_is_checked() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits(max_currency_exposure=D("100000")))
    result = engine.evaluate(
        [_pair_risk("A", "100000", "0")], [_account("PAPER_MT5", "250000", "0")],
        underlying_by_pair={"A": "BTC"},
        currency_exposure={"USDT": D("500000")},
    )
    assert any("currency_exposure" in b.metric for b in result.breaches)


def test_portfolio_serialises() -> None:
    engine = PortfolioRiskEngine(PortfolioLimits())
    payload = engine.evaluate([_pair_risk("A", "100000", "0")],
                              [_account("PAPER_MT5", "250000", "0")],
                              underlying_by_pair={"A": "BTC"}).to_dict()
    for key in ("level", "gross_notional", "net_exposure", "margin_utilization_pct",
                "concentration_pct", "portfolio_max_loss", "exposure_by_venue"):
        assert key in payload
