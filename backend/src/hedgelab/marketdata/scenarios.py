"""Market scenario definitions.

A scenario is a set of multipliers applied on top of the base price process.
Scenarios are data, not branches: the simulator reads these numbers, so a new
scenario needs no simulator change.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from ..domain.enums import MarketScenario
from ..domain.numeric import dec


@dataclass(frozen=True, slots=True)
class ScenarioProfile:
    """Multipliers and switches describing one market regime."""

    scenario: MarketScenario
    description: str
    volatility_multiplier: Decimal = Decimal(1)
    spread_multiplier: Decimal = Decimal(1)
    liquidity_multiplier: Decimal = Decimal(1)
    #: Probability per tick of a discontinuous jump.
    jump_probability: Decimal = Decimal(0)
    #: Jump size as a fraction of price.
    jump_size: Decimal = Decimal(0)
    #: Simulated round-trip latency added to order handling.
    latency_ms: int = 5
    #: When true the feed stops advancing timestamps -> data goes stale.
    freeze_feed: bool = False
    #: When true the venue rejects every request -> disconnect.
    disconnect: bool = False

    def with_overrides(self, **kwargs: object) -> ScenarioProfile:
        coerced = {
            k: (dec(v) if isinstance(v, int | float | str | Decimal) and k not in {"latency_ms"} else v)
            for k, v in kwargs.items()
            if v is not None
        }
        return replace(self, **coerced)  # type: ignore[arg-type]


PROFILES: dict[MarketScenario, ScenarioProfile] = {
    MarketScenario.NORMAL: ScenarioProfile(
        scenario=MarketScenario.NORMAL,
        description="Calm two-sided market with typical spreads and depth.",
    ),
    MarketScenario.HIGH_VOLATILITY: ScenarioProfile(
        scenario=MarketScenario.HIGH_VOLATILITY,
        description="Volatility x5, spreads x3, depth halved, occasional jumps.",
        volatility_multiplier=Decimal(5),
        spread_multiplier=Decimal(3),
        liquidity_multiplier=Decimal("0.5"),
        jump_probability=Decimal("0.02"),
        jump_size=Decimal("0.004"),
        latency_ms=25,
    ),
    MarketScenario.SPREAD_WIDENING: ScenarioProfile(
        scenario=MarketScenario.SPREAD_WIDENING,
        description="Spreads x20 with unchanged volatility -- execution cost shock.",
        spread_multiplier=Decimal(20),
        liquidity_multiplier=Decimal("0.4"),
        latency_ms=15,
    ),
    MarketScenario.PRICE_GAP: ScenarioProfile(
        scenario=MarketScenario.PRICE_GAP,
        description="Certain discontinuous gap on the next tick.",
        volatility_multiplier=Decimal(2),
        spread_multiplier=Decimal(6),
        jump_probability=Decimal(1),
        jump_size=Decimal("0.03"),
        latency_ms=40,
    ),
    MarketScenario.STALE_DATA: ScenarioProfile(
        scenario=MarketScenario.STALE_DATA,
        description="Feed frozen: timestamps stop advancing, prices stop updating.",
        freeze_feed=True,
    ),
    MarketScenario.LIQUIDITY_REDUCTION: ScenarioProfile(
        scenario=MarketScenario.LIQUIDITY_REDUCTION,
        description="Top-of-book depth cut to 10% -- large orders partially fill.",
        liquidity_multiplier=Decimal("0.1"),
        spread_multiplier=Decimal(4),
        latency_ms=20,
    ),
    MarketScenario.EXCHANGE_DISCONNECT: ScenarioProfile(
        scenario=MarketScenario.EXCHANGE_DISCONNECT,
        description="Venue unreachable: every API call raises.",
        disconnect=True,
        freeze_feed=True,
    ),
}


def profile_for(scenario: MarketScenario) -> ScenarioProfile:
    return PROFILES[scenario]


def all_profiles() -> list[ScenarioProfile]:
    return list(PROFILES.values())
