"""Application settings.

Everything is environment-driven (12-factor).  There is deliberately **no
setting that holds an exchange API secret** -- the platform has no live order
path, so it has nothing to authenticate with.  See ``PAPER_TRADING.md``.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.enums import TradingMode

PACKAGE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parent.parent
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEDGELAB_",
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- identity -------------------------------------------------------
    app_name: str = "HedgeLab"
    environment: str = "local"
    version: str = "1.0.0"

    # --- safety ---------------------------------------------------------
    #: PAPER is the only mode with an implementation.  See ``venues/factory``.
    trading_mode: TradingMode = TradingMode.PAPER
    #: Must be explicitly true *and* a live adapter registered for LIVE to be
    #: constructible.  No live adapter exists in this repository.
    allow_live: bool = False
    require_confirmation_for_dangerous_actions: bool = True

    # --- storage --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://hedgelab:hedgelab@localhost:5468/hedgelab"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6479/0"
    #: When Redis is unreachable the event bus degrades to in-process only.
    redis_required: bool = False

    # --- market data ----------------------------------------------------
    market_data_source: str = "simulator"  # simulator | external_readonly
    simulator_seed: int = 20260908
    #: How often the background loop advances the market, in *real* time.
    #: This is a presentation choice: it sets how lively the dashboard looks.
    simulator_tick_ms: int = 500
    #: How much *simulated* time one tick represents.  Deliberately decoupled
    #: from the loop cadence: welding them together makes the simulated clock
    #: run at exactly real time, so an 8-hour funding interval takes 8 real
    #: hours and the volatility estimator needs 2.5 real hours of data before
    #: it can say anything.  At the default the market runs 120x real time.
    simulator_seconds_per_tick: Decimal = Decimal("60")
    stale_data_threshold_seconds: float = 5.0
    #: Rolling-statistics estimator. Sampling coarsely is deliberate: sampling
    #: every tick and scaling to a day amplifies microstructure noise into a
    #: badly overstated volatility.
    stats_window: int = 500
    stats_sample_seconds: float = 300.0
    stats_min_samples: int = 30
    max_spread_bps_for_execution: Decimal = Decimal("50")

    # --- accounting -----------------------------------------------------
    account_currency: str = "USD"
    #: Starting paper balances, keyed by venue name.
    paper_starting_balance: Decimal = Decimal("250000")

    # --- risk defaults (overridable per hedge config) --------------------
    warning_margin_level: Decimal = Decimal("200")
    danger_margin_level: Decimal = Decimal("150")
    emergency_margin_level: Decimal = Decimal("120")
    kill_switch_margin_level: Decimal = Decimal("100")
    max_daily_loss: Decimal = Decimal("25000")
    max_portfolio_notional: Decimal = Decimal("5000000")
    max_residual_exposure_bps: Decimal = Decimal("50")
    max_concentration_pct: Decimal = Decimal("60")

    # --- execution ------------------------------------------------------
    default_rebalance_tolerance_bps: Decimal = Decimal("25")
    execution_timeout_seconds: float = 10.0
    max_execution_retries: int = 2

    # --- api ------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:4173"])
    log_level: str = "INFO"
    log_json: bool = True

    #: Directory of YAML instrument specifications.
    instrument_spec_dir: Path = PACKAGE_ROOT / "instruments" / "specs"

    @field_validator("trading_mode", mode="after")
    @classmethod
    def _paper_only(cls, value: TradingMode) -> TradingMode:
        return value

    @property
    def is_paper(self) -> bool:
        return self.trading_mode is TradingMode.PAPER

    @property
    def sync_database_url(self) -> str:
        """Alembic needs a synchronous driver."""
        return (
            self.database_url.replace("+asyncpg", "+psycopg2")
            .replace("+aiosqlite", "")
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch the environment."""
    get_settings.cache_clear()
