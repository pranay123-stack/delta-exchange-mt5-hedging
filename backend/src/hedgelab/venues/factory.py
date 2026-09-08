"""Venue construction -- the single place a venue object comes into existence.

The factory is the enforcement point for paper-only operation.  There is one
registry of adapters and it contains only paper implementations; requesting
``LIVE`` raises regardless of any environment variable, because there is
nothing registered to build.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from ..config import Settings
from ..domain.enums import TradingMode
from ..domain.instrument import InstrumentSpec
from ..faults.injector import FaultInjector
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from ..marketdata.simulator import MarketSimulator
from .base import LiveTradingDisabledError, TradingVenue
from .paper_delta import DEFAULT_VENUE_NAME as DELTA_VENUE
from .paper_delta import PaperDeltaAdapter
from .paper_mt5 import DEFAULT_VENUE_NAME as MT5_VENUE
from .paper_mt5 import PaperMT5Adapter

log = get_logger(__name__)

#: Only paper adapters are registered.  Adding a live adapter here is the
#: single change that would make live trading reachable -- and it is not made.
PAPER_ADAPTERS: dict[str, Callable[..., TradingVenue]] = {
    DELTA_VENUE: PaperDeltaAdapter,
    MT5_VENUE: PaperMT5Adapter,
}

LIVE_ADAPTERS: dict[str, Callable[..., TradingVenue]] = {}


class VenueFactory:
    """Builds the venue set for a run."""

    def __init__(
        self,
        *,
        settings: Settings,
        instruments: dict[str, InstrumentSpec],
        simulator: MarketSimulator,
        fx: FxService,
        faults: FaultInjector,
    ) -> None:
        self.settings = settings
        self.instruments = instruments
        self.simulator = simulator
        self.fx = fx
        self.faults = faults

    def create(self, venue_name: str) -> TradingVenue:
        mode = self.settings.trading_mode
        if mode is not TradingMode.PAPER:
            raise LiveTradingDisabledError(
                f"trading_mode={mode.value} requested but no live adapter is registered; "
                f"this platform is paper-only (see PAPER_TRADING.md)"
            )
        try:
            adapter_cls = PAPER_ADAPTERS[venue_name]
        except KeyError:
            known = ", ".join(sorted(PAPER_ADAPTERS))
            raise KeyError(f"no paper adapter for venue {venue_name!r}; known: {known}") from None

        venue = adapter_cls(
            instruments=self.instruments,
            simulator=self.simulator,
            fx=self.fx,
            faults=self.faults,
            starting_balance=Decimal(self.settings.paper_starting_balance),
            name=venue_name,
            account_currency=self.settings.account_currency,
        )
        log.info(
            "venue constructed",
            extra={"venue": venue_name, "paper": venue.is_paper, "mode": mode.value},
        )
        return venue

    def create_all(self) -> dict[str, TradingVenue]:
        """One venue per distinct venue name in the instrument catalogue."""
        names = sorted({spec.venue for spec in self.instruments.values()})
        return {name: self.create(name) for name in names}
