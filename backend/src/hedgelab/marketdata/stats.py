"""Rolling volatility, correlation and beta estimated from observed prices.

The risk- and cost-aware hedge objectives need three statistics: the daily
volatility of each leg and the correlation between them.  Supplying them as
constants makes ``RISK_WEIGHTED`` a parameterised guess; this module estimates
them from prices the platform has actually seen.

Three decisions worth stating, because each is a real trade-off:

**Statistics are computed in float, not Decimal.**  Everywhere else this
platform uses ``Decimal``, because a quantity that misses a venue's lattice by
one ULP is a rejected order.  A volatility estimate is not that kind of number:
its sampling error after 200 observations is several percent, which is around
fourteen orders of magnitude larger than float precision.  Paying Decimal's
cost for ``sqrt`` and covariance accumulation would buy nothing.  The boundary
is explicit -- floats live inside this module and results leave it as
``Decimal``.

**Sampling is time-based, not tick-based.**  Sampling every 500 ms tick and
scaling by ``sqrt(172800)`` amplifies microstructure noise into a wildly
overstated daily volatility.  Real desks sample coarsely for exactly this
reason.  The default is one sample per five simulated minutes.

**An estimate is refused until it is worth having.**  Below
``min_samples`` the estimator returns the caller's fallback and says why,
rather than emitting a confident-looking number derived from eight
observations.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.numeric import ZERO, dec
from ..logging_setup import get_logger

log = get_logger(__name__)

SECONDS_PER_DAY = 86_400.0
#: Below this the correlation estimate is too noisy to drive a hedge ratio.
DEFAULT_MIN_SAMPLES = 30
#: Long enough to be stable, short enough to track a regime change.
DEFAULT_WINDOW = 500
#: Sampling interval. Five minutes is coarse enough to suppress the basis
#: process's tick-level noise without discarding a day's information.
DEFAULT_SAMPLE_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class Estimate:
    """One instrument's realised volatility, with the evidence behind it."""

    key: str
    samples: int
    daily_volatility: Decimal
    #: Mean log return per sample, annualised to a daily drift.
    daily_drift: Decimal
    sample_seconds: float
    is_reliable: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "samples": self.samples,
            "daily_volatility": str(self.daily_volatility),
            "daily_drift": str(self.daily_drift),
            "sample_seconds": self.sample_seconds,
            "is_reliable": self.is_reliable,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PairEstimate:
    """Joint statistics for a hedge pair."""

    source: Estimate
    hedge: Estimate
    correlation: Decimal
    beta: Decimal
    #: Standard error of the regression slope. A beta whose distance from 1.0
    #: is smaller than this is not distinguishable from a one-for-one hedge,
    #: and reading a hedge ratio off it is reading noise.
    beta_standard_error: Decimal
    overlapping_samples: int
    is_reliable: bool
    note: str

    @property
    def beta_is_distinguishable_from_one(self) -> bool:
        """True when beta differs from 1.0 by more than two standard errors."""
        if self.beta_standard_error <= ZERO:
            return False
        return abs(self.beta - Decimal(1)) > self.beta_standard_error * Decimal(2)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.to_dict(),
            "hedge": self.hedge.to_dict(),
            "correlation": str(self.correlation),
            "beta": str(self.beta),
            "beta_standard_error": str(self.beta_standard_error),
            "beta_is_distinguishable_from_one": self.beta_is_distinguishable_from_one,
            "overlapping_samples": self.overlapping_samples,
            "is_reliable": self.is_reliable,
            "note": self.note,
        }


@dataclass
class _Series:
    """Log returns for one instrument, tagged with the sample index."""

    last_price: float | None = None
    last_sample_at: datetime | None = None
    #: ``(sample_index, log_return, elapsed_seconds)``.  The index aligns two
    #: series exactly; the elapsed time is what the return is normalised by.
    returns: deque[tuple[int, float, float]] = field(default_factory=deque)


class RollingStats:
    """Estimates volatility, correlation and beta from observed mid prices.

    Feed it tickers; ask it for a :class:`PairEstimate`.  It is a pure
    accumulator with no I/O, so it can be driven from the market-data loop, a
    backtest replay or a test with equal ease.
    """

    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ) -> None:
        if window < 2:
            raise ValueError("window must be at least 2")
        if sample_seconds <= 0:
            raise ValueError("sample_seconds must be positive")
        self.window = window
        self.sample_seconds = sample_seconds
        self.min_samples = min_samples
        self._series: dict[str, _Series] = {}
        #: Shared counter so returns from different instruments recorded in the
        #: same pass carry the same index and can be paired without guessing.
        self._sample_index = 0

    # ------------------------------------------------------------------
    # ingest
    # ------------------------------------------------------------------
    def observe(self, key: str, price: Decimal, at: datetime) -> bool:
        """Record a price. Returns True when it produced a new sample.

        Prices arriving faster than ``sample_seconds`` are ignored rather than
        averaged: sub-sampling is what keeps the estimate free of the tick-level
        noise that would otherwise dominate it.
        """
        value = float(price)
        if value <= 0 or not math.isfinite(value):
            return False

        series = self._series.setdefault(key, _Series())
        if series.last_sample_at is None or series.last_price is None:
            series.last_price = value
            series.last_sample_at = at
            return False

        elapsed = (at - series.last_sample_at).total_seconds()
        if elapsed < self.sample_seconds:
            return False

        series.returns.append(
            (self._sample_index, math.log(value / series.last_price), elapsed)
        )
        while len(series.returns) > self.window:
            series.returns.popleft()
        series.last_price = value
        series.last_sample_at = at
        return True

    def observe_many(self, prices: dict[str, Decimal], at: datetime) -> int:
        """Record a batch of prices as one aligned sample.

        The shared index only advances when at least one series actually
        sampled, so a burst of ignored ticks does not open gaps that would
        prevent later returns from pairing up.
        """
        recorded = sum(1 for key, price in prices.items() if self.observe(key, price, at))
        if recorded:
            self._sample_index += 1
        return recorded

    def reset(self) -> None:
        self._series.clear()
        self._sample_index = 0

    # ------------------------------------------------------------------
    # estimate
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise(entries: list[tuple[int, float, float]]) -> list[float]:
        """Scale each return to a common one-second horizon.

        ``sample_seconds`` is a *minimum* spacing, not a guarantee: if the feed
        ticks more slowly than that, every observation covers more time than
        configured.  Scaling by the configured interval then overstates
        volatility by ``sqrt(actual / configured)`` -- a silent 3.4x error when
        a five-minute setting meets an hourly feed.

        Dividing each return by ``sqrt(elapsed)`` removes the dependence on
        spacing entirely, and handles a heterogeneous feed as a free
        consequence.  With uniform spacing it reduces to the naive formula.
        """
        return [r / math.sqrt(elapsed) for _, r, elapsed in entries if elapsed > 0]

    def estimate(self, key: str) -> Estimate:
        """Realised daily volatility for one instrument."""
        series = self._series.get(key)
        entries = list(series.returns) if series else []
        count = len(entries)

        if count < self.min_samples:
            return Estimate(
                key=key, samples=count, daily_volatility=ZERO, daily_drift=ZERO,
                sample_seconds=self.sample_seconds, is_reliable=False,
                note=(
                    f"only {count} samples; {self.min_samples} needed before the "
                    f"estimate is worth using"
                ),
            )

        per_second = self._normalise(entries)
        mean = sum(per_second) / len(per_second)
        # Sample variance (n-1): with a few hundred observations the bias from
        # dividing by n is small, but it is free to be correct.
        variance = sum((r - mean) ** 2 for r in per_second) / (len(per_second) - 1)
        # Variance scales linearly in time, so volatility scales as its square
        # root -- and drift scales linearly.
        daily_vol = math.sqrt(max(variance, 0.0)) * math.sqrt(SECONDS_PER_DAY)
        observed_spacing = sum(e for _, _, e in entries) / count

        return Estimate(
            key=key, samples=count,
            daily_volatility=dec(round(daily_vol, 10)),
            daily_drift=dec(round(
                sum(r for _, r, _ in entries) / sum(e for _, _, e in entries)
                * SECONDS_PER_DAY, 10)),
            sample_seconds=observed_spacing, is_reliable=True,
            note=(
                f"{count} samples averaging {observed_spacing:g}s apart, "
                f"normalised to one day"
            ),
        )

    def correlation(self, source_key: str, hedge_key: str) -> tuple[Decimal, int]:
        """Pearson correlation over samples the two series share.

        Returns ``(correlation, overlapping_sample_count)``.  Only indices
        present in both series are used -- an instrument that started quoting
        later must not silently shift the other's returns.
        """
        source = self._series.get(source_key)
        hedge = self._series.get(hedge_key)
        if source is None or hedge is None:
            return ZERO, 0

        hedge_by_index = {
            index: value / math.sqrt(elapsed)
            for index, value, elapsed in hedge.returns
            if elapsed > 0
        }
        paired = [
            (value / math.sqrt(elapsed), hedge_by_index[index])
            for index, value, elapsed in source.returns
            if elapsed > 0 and index in hedge_by_index
        ]
        if len(paired) < self.min_samples:
            return ZERO, len(paired)

        count = len(paired)
        source_values = [a for a, _ in paired]
        hedge_values = [b for _, b in paired]
        source_mean = sum(source_values) / count
        hedge_mean = sum(hedge_values) / count

        covariance = sum(
            (a - source_mean) * (b - hedge_mean) for a, b in paired
        )
        source_ss = sum((a - source_mean) ** 2 for a in source_values)
        hedge_ss = sum((b - hedge_mean) ** 2 for b in hedge_values)
        denominator = math.sqrt(source_ss * hedge_ss)
        if denominator <= 0:
            return ZERO, count

        # Clamp: floating-point accumulation can land a hair outside [-1, 1],
        # and a correlation above 1 makes residual vol imaginary downstream.
        rho = max(-1.0, min(1.0, covariance / denominator))
        return dec(round(rho, 10)), count

    def pair_estimate(self, source_key: str, hedge_key: str) -> PairEstimate:
        """Joint estimate for a hedge pair, including the regression beta."""
        source = self.estimate(source_key)
        hedge = self.estimate(hedge_key)
        rho, overlap = self.correlation(source_key, hedge_key)

        reliable = source.is_reliable and hedge.is_reliable and overlap >= self.min_samples
        beta = ZERO
        standard_error = ZERO
        if reliable and hedge.daily_volatility > ZERO:
            beta = rho * source.daily_volatility / hedge.daily_volatility
            # ``beta`` is the OLS slope of source returns on hedge returns, so
            # its standard error is the usual one:
            #     SE = sigma_residual / (sigma_hedge * sqrt(n))
            #        = sigma_source * sqrt(1 - rho^2) / (sigma_hedge * sqrt(n))
            # Reporting the point estimate alone invites reading a 1% deviation
            # from parity as a signal when it is within sampling error.
            remainder = Decimal(1) - rho * rho
            if remainder > ZERO:
                residual = source.daily_volatility * remainder.sqrt()
                standard_error = residual / (
                    hedge.daily_volatility * Decimal(overlap).sqrt()
                )

        if reliable:
            note = (
                f"estimated from {overlap} paired samples at "
                f"{self.sample_seconds:g}s intervals"
            )
        else:
            note = (
                f"insufficient data: {overlap} paired samples "
                f"(need {self.min_samples})"
            )

        return PairEstimate(
            source=source, hedge=hedge, correlation=rho, beta=beta,
            beta_standard_error=standard_error,
            overlapping_samples=overlap, is_reliable=reliable, note=note,
        )

    # ------------------------------------------------------------------
    def tracked_keys(self) -> list[str]:
        return sorted(self._series)

    def sample_counts(self) -> dict[str, int]:
        return {key: len(series.returns) for key, series in sorted(self._series.items())}
