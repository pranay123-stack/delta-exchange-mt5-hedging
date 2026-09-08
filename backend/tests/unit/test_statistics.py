"""Rolling volatility, correlation and beta estimation.

The load-bearing property is that an estimate recovers what the simulator was
actually configured with, independently of how often the feed happens to tick.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import SPEC_DIR

from hedgelab.hedge.objectives import RiskParameters
from hedgelab.instruments.registry import InstrumentRegistry
from hedgelab.marketdata.simulator import DEFAULT_UNDERLYINGS, MarketSimulator
from hedgelab.marketdata.stats import RollingStats

D = Decimal
BTC_PERP = "PAPER_DELTA:BTCUSDT-PERP"
BTC_CFD = "PAPER_MT5:BTCUSD"
GOLD_CFD = "PAPER_MT5:XAUUSD"

EXPECTED_DAILY_VOL = {
    u.key: float(u.annual_volatility) / math.sqrt(365) for u in DEFAULT_UNDERLYINGS
}


def drive(
    *, tick_seconds: int, sample_seconds: float, steps: int = 600, seed: int = 4321,
    window: int = 900,
) -> RollingStats:
    """Run the simulator and feed every tick to a fresh estimator."""
    registry = InstrumentRegistry.from_directory(SPEC_DIR)
    simulator = MarketSimulator(
        registry.all(), seed=seed, tick_seconds=D(str(tick_seconds))
    )
    stats = RollingStats(window=window, sample_seconds=sample_seconds, min_samples=30)
    for _ in range(steps):
        simulator.advance(1)
        stats.observe_many(
            {key: t.mid for key, t in simulator.tickers().items()}, simulator.clock
        )
    return stats


# ======================================================================
# accuracy
# ======================================================================
def test_estimate_recovers_the_configured_volatility() -> None:
    stats = drive(tick_seconds=3600, sample_seconds=300.0)
    estimate = stats.estimate(BTC_PERP)
    assert estimate.is_reliable
    ratio = float(estimate.daily_volatility) / EXPECTED_DAILY_VOL["BTC"]
    assert 0.85 < ratio < 1.15


@pytest.mark.parametrize("underlying,key", [
    ("BTC", BTC_PERP),
    ("ETH", "PAPER_DELTA:ETHUSDT-PERP"),
    ("SOL", "PAPER_DELTA:SOLUSDT-PERP"),
    ("XAU", GOLD_CFD),
])
def test_estimate_tracks_volatility_across_a_wide_range(
    underlying: str, key: str
) -> None:
    """Gold and SOL differ by more than 6x; the estimator must follow."""
    stats = drive(tick_seconds=3600, sample_seconds=300.0)
    estimate = stats.estimate(key)
    ratio = float(estimate.daily_volatility) / EXPECTED_DAILY_VOL[underlying]
    assert 0.85 < ratio < 1.15, f"{underlying}: ratio {ratio}"


@pytest.mark.parametrize("tick_seconds", [300, 900, 1800, 3600, 7200])
def test_estimate_is_independent_of_the_feed_rate(tick_seconds: int) -> None:
    """The regression this module exists to prevent.

    ``sample_seconds`` is a minimum spacing, not a guarantee. Scaling by the
    configured interval rather than the observed one overstates volatility by
    ``sqrt(actual / configured)`` -- a silent 3.4x error when a five-minute
    setting meets an hourly feed.
    """
    stats = drive(tick_seconds=tick_seconds, sample_seconds=300.0)
    estimate = stats.estimate(BTC_PERP)
    ratio = float(estimate.daily_volatility) / EXPECTED_DAILY_VOL["BTC"]
    assert 0.85 < ratio < 1.15, f"tick={tick_seconds}s gave ratio {ratio}"


def test_estimate_is_independent_of_the_configured_sample_interval() -> None:
    coarse = drive(tick_seconds=3600, sample_seconds=3600.0).estimate(BTC_PERP)
    fine = drive(tick_seconds=3600, sample_seconds=300.0).estimate(BTC_PERP)
    assert abs(
        float(coarse.daily_volatility) - float(fine.daily_volatility)
    ) < 1e-9


# ======================================================================
# correlation and beta
# ======================================================================
def test_same_underlying_legs_are_almost_perfectly_correlated() -> None:
    stats = drive(tick_seconds=3600, sample_seconds=300.0)
    rho, overlap = stats.correlation(BTC_PERP, BTC_CFD)
    assert overlap > 100
    assert rho > D("0.99")


def test_unrelated_underlyings_are_uncorrelated() -> None:
    stats = drive(tick_seconds=3600, sample_seconds=300.0)
    rho, _ = stats.correlation(BTC_PERP, GOLD_CFD)
    assert abs(rho) < D("0.2")


def test_correlation_is_bounded() -> None:
    """Floating-point accumulation must never produce |rho| > 1.

    A correlation above one makes the residual volatility imaginary
    downstream.
    """
    stats = drive(tick_seconds=3600, sample_seconds=300.0)
    for source, hedge in ((BTC_PERP, BTC_CFD), (BTC_PERP, GOLD_CFD)):
        rho, _ = stats.correlation(source, hedge)
        assert D("-1") <= rho <= D("1")


def test_beta_is_near_one_for_a_matched_pair() -> None:
    stats = drive(tick_seconds=3600, sample_seconds=300.0)
    estimate = stats.pair_estimate(BTC_PERP, BTC_CFD)
    assert estimate.is_reliable
    assert D("0.9") < estimate.beta < D("1.1")


def test_correlation_only_pairs_overlapping_samples() -> None:
    """An instrument that starts quoting later must not shift the other."""
    stats = RollingStats(window=100, sample_seconds=60.0, min_samples=5)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for step in range(40):
        at = start + timedelta(seconds=60 * step)
        prices = {"A": D(100 + step)}
        if step >= 20:                      # B joins halfway through
            prices["B"] = D(200 + step)
        stats.observe_many(prices, at)
    _, overlap = stats.correlation("A", "B")
    assert 0 < overlap < 20


# ======================================================================
# refusing to guess
# ======================================================================
def test_estimate_is_refused_without_enough_samples() -> None:
    stats = RollingStats(min_samples=30)
    estimate = stats.estimate("NOPE:NOPE")
    assert estimate.is_reliable is False
    assert estimate.daily_volatility == D("0")
    assert "30 needed" in estimate.note


def test_pair_estimate_reports_why_it_is_unreliable() -> None:
    stats = drive(tick_seconds=3600, sample_seconds=300.0, steps=5)
    estimate = stats.pair_estimate(BTC_PERP, BTC_CFD)
    assert estimate.is_reliable is False
    assert estimate.beta == D("0")
    assert "insufficient data" in estimate.note


def test_first_observation_produces_no_return() -> None:
    stats = RollingStats(sample_seconds=1.0)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert stats.observe("A", D("100"), at) is False
    assert stats.observe("A", D("101"), at + timedelta(seconds=2)) is True


def test_ticks_faster_than_the_sample_interval_are_ignored() -> None:
    stats = RollingStats(sample_seconds=60.0)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    stats.observe("A", D("100"), at)
    assert stats.observe("A", D("101"), at + timedelta(seconds=5)) is False
    assert stats.observe("A", D("102"), at + timedelta(seconds=90)) is True


def test_non_positive_prices_are_ignored() -> None:
    stats = RollingStats(sample_seconds=1.0)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert stats.observe("A", D("0"), at) is False
    assert stats.observe("A", D("-5"), at) is False


def test_window_is_bounded() -> None:
    stats = RollingStats(window=50, sample_seconds=1.0, min_samples=5)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    for step in range(200):
        stats.observe("A", D(100 + step % 7), at + timedelta(seconds=2 * step))
    assert stats.sample_counts()["A"] == 50


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="window"):
        RollingStats(window=1)
    with pytest.raises(ValueError, match="sample_seconds"):
        RollingStats(sample_seconds=0)


# ======================================================================
# integration with RiskParameters
# ======================================================================
def test_with_estimates_replaces_measurements_but_keeps_policy() -> None:
    base = RiskParameters(risk_aversion=D("400"), horizon_days=D("5"),
                          max_ratio=D("1.1"))
    updated = base.with_estimates(
        source_daily_vol=D("0.02"), hedge_daily_vol=D("0.021"),
        correlation=D("0.98"), provenance="200 samples",
    )
    # Measurements replaced.
    assert updated.source_daily_vol == D("0.02")
    assert updated.correlation == D("0.98")
    assert updated.estimated is True
    assert updated.provenance == "200 samples"
    # Policy preserved.
    assert updated.risk_aversion == D("400")
    assert updated.horizon_days == D("5")
    assert updated.max_ratio == D("1.1")


def test_defaults_declare_themselves_as_assumptions() -> None:
    params = RiskParameters()
    assert params.estimated is False
    assert "default" in params.provenance


# ======================================================================
# uncertainty
# ======================================================================
def test_beta_standard_error_shrinks_with_the_square_root_of_samples() -> None:
    """SE = sigma_residual / (sigma_hedge * sqrt(n)), so 4x the data halves it."""
    small = drive(tick_seconds=300, sample_seconds=300.0, steps=250, seed=31337)
    large = drive(tick_seconds=300, sample_seconds=300.0, steps=1000, seed=31337)
    se_small = float(small.pair_estimate(BTC_PERP, BTC_CFD).beta_standard_error)
    se_large = float(large.pair_estimate(BTC_PERP, BTC_CFD).beta_standard_error)
    assert se_small > se_large > 0
    # Four times the samples should roughly halve the standard error.
    assert 1.6 < se_small / se_large < 2.6


def test_beta_is_not_called_significant_on_thin_data() -> None:
    """A 1% deviation from parity on 200 samples is noise, and must say so.

    Reporting a point estimate without its uncertainty invites reading
    sampling error as signal -- which for a hedge ratio means trading it.
    """
    stats = drive(tick_seconds=300, sample_seconds=300.0, steps=250, seed=31337)
    estimate = stats.pair_estimate(BTC_PERP, BTC_CFD)
    assert estimate.is_reliable
    assert abs(estimate.beta - D("1")) < estimate.beta_standard_error * D("2")
    assert estimate.beta_is_distinguishable_from_one is False


def test_beta_becomes_significant_with_enough_data() -> None:
    """The simulator's basis does put true beta slightly below 1.

    With enough samples that becomes measurable rather than merely asserted.
    """
    stats = drive(tick_seconds=300, sample_seconds=300.0, steps=4200, seed=31337,
                  window=5000)
    estimate = stats.pair_estimate(BTC_PERP, BTC_CFD)
    assert estimate.overlapping_samples > 3000
    assert estimate.beta < D("1")
    assert estimate.beta_is_distinguishable_from_one is True


def test_unreliable_estimate_reports_no_standard_error() -> None:
    stats = drive(tick_seconds=300, sample_seconds=300.0, steps=5)
    estimate = stats.pair_estimate(BTC_PERP, BTC_CFD)
    assert estimate.is_reliable is False
    assert estimate.beta_standard_error == D("0")
    assert estimate.beta_is_distinguishable_from_one is False


def test_perfect_correlation_gives_a_zero_standard_error() -> None:
    """Identical series have no residual, so the slope has no sampling error."""
    stats = RollingStats(window=200, sample_seconds=1.0, min_samples=10)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for step in range(60):
        at = start + timedelta(seconds=2 * step)
        price = D(100) + D(step) * D("0.5")
        stats.observe_many({"A": price, "B": price * D(3)}, at)
    estimate = stats.pair_estimate("A", "B")
    assert estimate.correlation == D("1")
    assert estimate.beta_standard_error == D("0")
    assert estimate.beta_is_distinguishable_from_one is False
