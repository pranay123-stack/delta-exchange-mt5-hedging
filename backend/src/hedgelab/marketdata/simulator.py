"""Deterministic market-data simulator.

Design goals, in priority order:

1. **Deterministic.**  Given a seed, the price at step *n* is always the same,
   so a failing scenario can be replayed exactly.  Randomness is derived from
   ``hash(seed, key, step)`` rather than from a stateful generator, so the
   sequence does not depend on how many other instruments are subscribed.
2. **Correlated but not identical.**  Instruments sharing an
   ``underlying_key`` are driven by one price process, then offset by a
   mean-reverting *basis*.  Without this, a perp and a CFD would be
   numerically identical and basis risk -- the whole point of cross-venue
   hedging -- would be invisible.
3. **Scenario driven.**  Volatility, spread, depth, jumps, staleness and
   disconnects come from :mod:`.scenarios`, not from branches in this file.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..domain.enums import FundingModel, MarketScenario
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, OrderBookLevel, Ticker
from ..domain.numeric import ZERO, dec, quantize
from ..logging_setup import get_logger
from .scenarios import ScenarioProfile, profile_for

log = get_logger(__name__)

#: Annualised volatility used when an underlying has no explicit configuration.
DEFAULT_ANNUAL_VOL = Decimal("0.55")
SECONDS_PER_YEAR = Decimal(365 * 24 * 3600)


@dataclass(frozen=True, slots=True)
class UnderlyingConfig:
    """Price-process parameters for one underlying."""

    key: str
    initial_price: Decimal
    annual_volatility: Decimal = DEFAULT_ANNUAL_VOL
    annual_drift: Decimal = Decimal(0)
    #: Depth at the touch, in base-asset units.
    base_liquidity: Decimal = Decimal(50)


DEFAULT_UNDERLYINGS: tuple[UnderlyingConfig, ...] = (
    UnderlyingConfig("BTC", Decimal("102500"), Decimal("0.55"), Decimal("0.05"), Decimal(60)),
    UnderlyingConfig("ETH", Decimal("3850"), Decimal("0.68"), Decimal("0.03"), Decimal(900)),
    UnderlyingConfig("SOL", Decimal("182.40"), Decimal("0.92"), Decimal("0.0"), Decimal(9000)),
    UnderlyingConfig("XAU", Decimal("2418.50"), Decimal("0.14"), Decimal("0.02"), Decimal(4000)),
)


@dataclass
class _UnderlyingState:
    config: UnderlyingConfig
    price: Decimal
    step: int = 0


@dataclass
class _InstrumentState:
    """Per-instrument overlay on the shared underlying process."""

    spec: InstrumentSpec
    basis_bps: Decimal = ZERO
    funding_rate: Decimal = ZERO
    last_ticker: Ticker | None = None
    sequence: int = 0


class MarketSimulator:
    """Generates tickers and order books for a set of instruments."""

    def __init__(
        self,
        instruments: Iterable[InstrumentSpec],
        *,
        seed: int = 20260908,
        tick_seconds: Decimal | float = 0.5,
        underlyings: Iterable[UnderlyingConfig] = DEFAULT_UNDERLYINGS,
        start_time: datetime | None = None,
    ) -> None:
        self.seed = seed
        self.tick_seconds = dec(tick_seconds)
        self._start_time = start_time or datetime.now(UTC)
        self._clock = self._start_time
        self._underlyings: dict[str, _UnderlyingState] = {}
        for cfg in underlyings:
            self._underlyings[cfg.key] = _UnderlyingState(config=cfg, price=cfg.initial_price)
        self._instruments: dict[str, _InstrumentState] = {}
        self._profiles: dict[str, ScenarioProfile] = {}
        self._global_profile = profile_for(MarketScenario.NORMAL)
        for spec in instruments:
            self.add_instrument(spec)
        self._prime()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def add_instrument(self, spec: InstrumentSpec) -> None:
        underlying = spec.effective_underlying_key
        if underlying not in self._underlyings:
            # Unknown underlying: synthesise a process so a brand-new
            # instrument works without touching this file.
            rng = random.Random(f"{self.seed}:underlying:{underlying}")
            price = dec(round(rng.uniform(10, 5000), 2))
            self._underlyings[underlying] = _UnderlyingState(
                config=UnderlyingConfig(underlying, price),
                price=price,
            )
            log.info(
                "synthesised price process for unconfigured underlying",
                extra={"underlying": underlying, "initial_price": str(price)},
            )
        state = _InstrumentState(spec=spec, funding_rate=spec.baseline_funding_rate)
        # Give each venue a distinct starting basis so cross-venue prices differ.
        rng = random.Random(f"{self.seed}:basis:{spec.key}")
        state.basis_bps = dec(round(rng.uniform(-6, 6), 3))
        self._instruments[spec.key] = state

    def _prime(self) -> None:
        for key in self._instruments:
            self._instruments[key].last_ticker = self._build_ticker(key)

    # ------------------------------------------------------------------
    # scenario control
    # ------------------------------------------------------------------
    def set_scenario(
        self, scenario: MarketScenario, venue: str | None = None, **overrides: object
    ) -> ScenarioProfile:
        """Apply a scenario globally, or to one venue only."""
        profile = profile_for(scenario)
        if overrides:
            profile = profile.with_overrides(**overrides)
        if venue is None:
            self._global_profile = profile
            self._profiles.clear()
        else:
            self._profiles[venue] = profile
        log.info(
            "market scenario applied",
            extra={"scenario": scenario.value, "venue": venue or "ALL"},
        )
        return profile

    def clear_scenario(self, venue: str | None = None) -> None:
        if venue is None:
            self._global_profile = profile_for(MarketScenario.NORMAL)
            self._profiles.clear()
        else:
            self._profiles.pop(venue, None)

    def profile_for_venue(self, venue: str) -> ScenarioProfile:
        return self._profiles.get(venue, self._global_profile)

    def active_scenarios(self) -> dict[str, str]:
        result = {"GLOBAL": self._global_profile.scenario.value}
        result.update({venue: p.scenario.value for venue, p in self._profiles.items()})
        return result

    def is_disconnected(self, venue: str) -> bool:
        return self.profile_for_venue(venue).disconnect

    # ------------------------------------------------------------------
    # randomness (stateless, reproducible)
    # ------------------------------------------------------------------
    def _rng(self, channel: str, key: str, step: int) -> random.Random:
        return random.Random(f"{self.seed}:{channel}:{key}:{step}")

    def _gauss(self, channel: str, key: str, step: int) -> Decimal:
        return dec(round(self._rng(channel, key, step).gauss(0.0, 1.0), 10))

    # ------------------------------------------------------------------
    # advancing the clock
    # ------------------------------------------------------------------
    def advance(self, steps: int = 1) -> None:
        """Advance the price processes by ``steps`` ticks."""
        for _ in range(max(0, steps)):
            self._advance_once()

    def _advance_once(self) -> None:
        frozen_everywhere = self._global_profile.freeze_feed and not self._profiles
        if not frozen_everywhere:
            self._clock += timedelta(seconds=float(self.tick_seconds))

        dt = self.tick_seconds / SECONDS_PER_YEAR
        vol_multiplier = self._global_profile.volatility_multiplier

        for key, state in self._underlyings.items():
            state.step += 1
            cfg = state.config
            sigma = cfg.annual_volatility * vol_multiplier
            z = self._gauss("price", key, state.step)
            drift = (cfg.annual_drift - sigma * sigma / Decimal(2)) * dt
            diffusion = sigma * dec(math.sqrt(float(dt))) * z
            growth = dec(math.exp(float(drift + diffusion)))
            new_price = state.price * growth

            jump_p = self._global_profile.jump_probability
            if jump_p > ZERO:
                draw = dec(round(self._rng("jump", key, state.step).random(), 10))
                if draw < jump_p:
                    up = self._rng("jumpdir", key, state.step).random() < 0.5
                    direction = Decimal(1) if up else Decimal(-1)
                    new_price *= Decimal(1) + direction * self._global_profile.jump_size
                    log.warning(
                        "price gap injected",
                        extra={
                            "underlying": key,
                            "from": str(state.price),
                            "to": str(new_price),
                            "size_pct": str(self._global_profile.jump_size * 100),
                        },
                    )
            state.price = max(new_price, cfg.initial_price / Decimal(1000))

        for key, inst in self._instruments.items():
            step = self._underlyings[inst.spec.effective_underlying_key].step
            # Basis mean-reverts to zero with noise: cross-venue prices drift
            # apart and back, which is exactly what basis risk looks like.
            shock = self._gauss("basis", key, step) * Decimal("0.35")
            inst.basis_bps = inst.basis_bps * Decimal("0.985") + shock
            if inst.spec.funding_model is FundingModel.PERPETUAL_FUNDING:
                fshock = self._gauss("funding", key, step) * Decimal("0.000012")
                target = inst.spec.baseline_funding_rate
                updated = inst.funding_rate + (target - inst.funding_rate) * Decimal("0.05") + fshock
                # Venues publish funding to a fixed precision; keeping full
                # Decimal expansion would leak 30-digit numbers into the API.
                inst.funding_rate = quantize(updated, 8)

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def underlying_price(self, underlying: str) -> Decimal:
        return self._underlyings[underlying].price

    def set_underlying_price(self, underlying: str, price: Decimal) -> None:
        """Force a price -- used by scenarios and the acceptance demo."""
        if underlying not in self._underlyings:
            raise KeyError(f"unknown underlying {underlying!r}")
        self._underlyings[underlying].price = dec(price)

    def apply_shock(self, underlying: str, pct_move: Decimal) -> Decimal:
        """Apply an instantaneous relative move, returning the new price."""
        state = self._underlyings[underlying]
        state.price = state.price * (Decimal(1) + dec(pct_move))
        log.warning(
            "market shock applied",
            extra={"underlying": underlying, "pct_move": str(pct_move), "price": str(state.price)},
        )
        return state.price

    def set_funding_rate(self, instrument_key: str, rate: Decimal) -> None:
        self._instruments[instrument_key].funding_rate = dec(rate)

    def _build_ticker(self, instrument_key: str) -> Ticker:
        inst = self._instruments[instrument_key]
        spec = inst.spec
        profile = self.profile_for_venue(spec.venue)

        base_price = self._underlyings[spec.effective_underlying_key].price
        mid = base_price * (Decimal(1) + inst.basis_bps / Decimal(10000))

        spread_bps = spec.typical_spread_bps * profile.spread_multiplier
        half_spread = mid * spread_bps / Decimal(20000)
        bid = mid - half_spread
        ask = mid + half_spread

        tick = spec.tick_size
        bid = (bid / tick).quantize(Decimal(1), rounding="ROUND_FLOOR") * tick
        ask = (ask / tick).quantize(Decimal(1), rounding="ROUND_CEILING") * tick
        if ask <= bid:
            ask = bid + tick

        depth_base = self._underlyings[spec.effective_underlying_key].config.base_liquidity
        depth_units = depth_base * profile.liquidity_multiplier
        # Convert the underlying's base-unit depth into this venue's quantity unit.
        units_per_qty = spec.units_per_quantity
        depth_qty = (
            depth_units * mid / units_per_qty if spec.is_inverse
            else depth_units / units_per_qty
        )
        depth_qty = quantize(max(depth_qty, spec.min_quantity), spec.quantity_precision)

        step = self._underlyings[spec.effective_underlying_key].step
        turnover = dec(round(self._rng("volume", instrument_key, step).uniform(0.5, 1.5), 6))
        volume = turnover * depth_qty * Decimal(20)

        funding = inst.funding_rate if spec.funding_model is FundingModel.PERPETUAL_FUNDING else None
        next_funding = None
        if funding is not None:
            interval = int(spec.funding_interval_hours)
            hour_block = ((self._clock.hour // interval) + 1) * interval
            next_funding = (self._clock.replace(minute=0, second=0, microsecond=0)
                            + timedelta(hours=hour_block - self._clock.hour))

        inst.sequence += 1
        ticker = Ticker(
            venue=spec.venue,
            symbol=spec.symbol,
            bid=bid,
            ask=ask,
            last=mid,
            volume=quantize(volume, 4),
            timestamp=self._clock,
            funding_rate=funding,
            next_funding_time=next_funding,
            bid_size=depth_qty,
            ask_size=depth_qty,
            is_stale=profile.freeze_feed,
            sequence=inst.sequence,
        )
        return ticker

    def ticker(self, instrument_key: str) -> Ticker:
        """Current top of book.

        Under a frozen feed the *previous* ticker is returned unchanged, with
        its original timestamp -- which is what makes staleness detectable.
        """
        inst = self._instruments[instrument_key]
        profile = self.profile_for_venue(inst.spec.venue)
        if profile.freeze_feed and inst.last_ticker is not None:
            stale = inst.last_ticker
            return Ticker(
                venue=stale.venue,
                symbol=stale.symbol,
                bid=stale.bid,
                ask=stale.ask,
                last=stale.last,
                volume=stale.volume,
                timestamp=stale.timestamp,
                funding_rate=stale.funding_rate,
                next_funding_time=stale.next_funding_time,
                bid_size=stale.bid_size,
                ask_size=stale.ask_size,
                is_stale=True,
                sequence=stale.sequence,
            )
        ticker = self._build_ticker(instrument_key)
        inst.last_ticker = ticker
        return ticker

    def tickers(self, venue: str | None = None) -> dict[str, Ticker]:
        return {
            key: self.ticker(key)
            for key, state in self._instruments.items()
            if venue is None or state.spec.venue == venue
        }

    def orderbook(self, instrument_key: str, levels: int = 10) -> OrderBook:
        """Synthetic depth built outward from the touch.

        Level sizes decay geometrically, so a large order walks progressively
        worse prices -- this is what produces realistic slippage.
        """
        inst = self._instruments[instrument_key]
        spec = inst.spec
        ticker = self.ticker(instrument_key)
        tick = spec.tick_size
        step = self._underlyings[spec.effective_underlying_key].step

        bids: list[OrderBookLevel] = []
        asks: list[OrderBookLevel] = []
        for level in range(levels):
            decay = Decimal("1.6") ** level
            jitter = dec(round(self._rng(f"depth{level}", instrument_key, step).uniform(0.7, 1.3), 6))
            size = quantize(ticker.bid_size * decay * jitter, spec.quantity_precision)
            if size <= ZERO:
                size = spec.quantity_step
            gap = tick * Decimal(level) * (Decimal(1) + Decimal(level) / Decimal(4))
            bid_price = ticker.bid - gap
            ask_price = ticker.ask + gap
            if bid_price > ZERO:
                bids.append(OrderBookLevel(price=bid_price, size=size))
            asks.append(OrderBookLevel(price=ask_price, size=size))

        return OrderBook(
            venue=spec.venue,
            symbol=spec.symbol,
            bids=tuple(bids),
            asks=tuple(asks),
            timestamp=ticker.timestamp,
            sequence=ticker.sequence,
        )

    @property
    def clock(self) -> datetime:
        return self._clock

    def state_digest(self) -> dict[str, str]:
        """Compact snapshot used by tests to assert determinism."""
        return {key: str(state.price) for key, state in sorted(self._underlyings.items())}
