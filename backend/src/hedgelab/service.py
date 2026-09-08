"""Application service -- the composition root.

Everything the platform can do is a method here.  The FastAPI routers, the CLI
and the scenario runner are all thin shells over this class, so behaviour
cannot drift between "what the API does" and "what the CLI does".

Construction is explicit rather than magical: the service builds the registry,
simulator, FX service, fault injector, venues, calculators and engines in
dependency order, and every one of them is an attribute you can reach in a
test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import Settings, get_settings
from .costs.funding import FundingProjection, project_funding
from .costs.pnl import LegAccounting, PnLBreakdown, PnLCalculator
from .db.recorder import DatabaseRecorder
from .db.repositories import (
    CycleRepository,
    InstrumentRepository,
    MappingRepository,
    ObservabilityRepository,
    OrderRepository,
    PositionRepository,
)
from .db.session import Database
from .domain.account import AccountSnapshot
from .domain.enums import (
    EmergencyAction,
    FaultKind,
    HedgeObjective,
    MarketScenario,
    RiskLevel,
    Side,
)
from .domain.instrument import InstrumentSpec
from .domain.market import Ticker
from .domain.numeric import ZERO, dec, quantize
from .domain.orders import Order, OrderRequest, Position
from .execution.coordinator import ExecutionResult, HedgeCoordinator, HedgeRequest
from .execution.ports import EventPublisher, NullPublisher
from .execution.rebalancer import RebalanceAssessment, Rebalancer
from .faults.injector import ArmedFault, FaultInjector
from .hedge.calculator import HedgeCalculation, HedgeCalculator, HedgeInputs
from .hedge.objectives import RiskParameters
from .hedge.optimizer import HedgeOptimizer, OptimizationResult, OptimizerConfig
from .instruments.registry import HedgeMapping, InstrumentRegistry
from .logging_setup import correlation_scope, get_logger, new_correlation_id
from .marketdata.fx import FxService
from .marketdata.simulator import MarketSimulator
from .marketdata.stats import PairEstimate, RollingStats
from .reconciliation.engine import ReconciliationEngine, ReconciliationReport, RecoveryPlan
from .risk.engine import PairRisk, PairRiskInputs, RiskEngine, RiskThresholds
from .risk.portfolio import PortfolioLimits, PortfolioRisk, PortfolioRiskEngine
from .venues.base import TradingVenue, VenueError
from .venues.factory import VenueFactory

log = get_logger(__name__)


@dataclass
class ServiceState:
    """Mutable operational state, separate from the wiring."""

    emergency_mode: bool = False
    trading_paused: bool = False
    paused_pairs: set[str] = field(default_factory=set)
    kill_switch_engaged: bool = False
    last_reconciliation: ReconciliationReport | None = None
    started_at_step: int = 0


class HedgeLabService:
    """The whole platform, assembled."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        database: Database | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.state = ServiceState()

        # --- reference data ---------------------------------------------
        self.registry = InstrumentRegistry.from_directory(self.settings.instrument_spec_dir)
        self.specs: dict[str, InstrumentSpec] = {s.key: s for s in self.registry.all()}

        # --- market data ------------------------------------------------
        self.fx = FxService()
        self.simulator = MarketSimulator(
            self.registry.all(),
            seed=self.settings.simulator_seed,
            tick_seconds=self.settings.simulator_seconds_per_tick,
        )
        self.faults = FaultInjector(seed=self.settings.simulator_seed)
        #: Realised volatility and correlation, measured from the prices this
        #: process has actually seen.  Fed by ``advance_market``.
        self.stats = RollingStats(
            window=self.settings.stats_window,
            sample_seconds=self.settings.stats_sample_seconds,
            min_samples=self.settings.stats_min_samples,
        )

        # --- venues -----------------------------------------------------
        self.venue_factory = VenueFactory(
            settings=self.settings, instruments=self.specs,
            simulator=self.simulator, fx=self.fx, faults=self.faults,
        )
        self.venues: dict[str, TradingVenue] = self.venue_factory.create_all()

        # --- engines ----------------------------------------------------
        self.calculator = HedgeCalculator(self.fx)
        self.optimizer = HedgeOptimizer(self.calculator)
        self.risk_engine = RiskEngine(self.fx, self._risk_thresholds())
        self.portfolio_engine = PortfolioRiskEngine(self._portfolio_limits())
        self.pnl_calculator = PnLCalculator(self.fx, self.settings.account_currency)
        self.reconciler = ReconciliationEngine(registry=self.registry, venues=self.venues)

        # --- persistence ------------------------------------------------
        self.database = database if database is not None else Database.from_settings(self.settings)
        self.recorder = DatabaseRecorder(self.database)
        self.publisher: EventPublisher = publisher or NullPublisher()

        self.coordinator = HedgeCoordinator(
            settings=self.settings, registry=self.registry, venues=self.venues,
            calculator=self.calculator, optimizer=self.optimizer,
            risk_engine=self.risk_engine, fx=self.fx,
            recorder=self.recorder, publisher=self.publisher,
        )
        self.rebalancer = Rebalancer(
            settings=self.settings, registry=self.registry, venues=self.venues,
            calculator=self.calculator, fx=self.fx, recorder=self.recorder,
        )
        self._ticker_task: asyncio.Task[None] | None = None

    # ==================================================================
    # lifecycle
    # ==================================================================
    async def startup(
        self,
        *,
        create_schema: bool = False,
        seed_reference_data: bool = True,
        rehydrate: bool = True,
    ) -> None:
        if create_schema:
            await self.database.create_all()
        if seed_reference_data:
            await self.sync_reference_data()
        if rehydrate:
            restored = await self.rehydrate_paper_venues()
            if restored:
                log.info("paper venue state restored after restart", extra=restored)
        log.info(
            "service started",
            extra={
                "mode": self.settings.trading_mode.value,
                "venues": sorted(self.venues),
                "instruments": len(self.specs),
                "mappings": len(self.registry.mappings()),
            },
        )

    async def shutdown(self) -> None:
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            self._ticker_task = None
        await self.database.dispose()

    async def rehydrate_paper_venues(self) -> dict[str, Any]:
        """Reload venue state from the database after a process restart.

        The paper venues live in memory, so without this every restart would
        report the venue as flat and reconciliation would (correctly, but
        uselessly) flag every position as MISSING.  A real venue remembers; this
        makes the paper one behave the same way, which is what lets restart
        recovery test something meaningful.
        """
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            obs = ObservabilityRepository(session)
            latest_balance: dict[str, Decimal] = {}
            for venue_name in self.venues:
                snapshots = await obs.margin_snapshots(venue=venue_name, limit=1)
                if snapshots:
                    latest_balance[venue_name] = snapshots[0].balance

        restored = 0
        for position in positions:
            engine = getattr(self.venues.get(position.venue), "engine", None)
            if engine is None or position.is_flat:
                continue
            engine.restore_position(position)
            restored += 1
        for venue_name, balance in latest_balance.items():
            engine = getattr(self.venues[venue_name], "engine", None)
            if engine is not None:
                engine.restore_balance(balance)
        if restored or latest_balance:
            async with self.database.session() as session:
                await ObservabilityRepository(session).record_system_event(
                    "VENUE_REHYDRATED", "INFO", "service",
                    f"restored {restored} position(s) and {len(latest_balance)} balance(s) "
                    f"into the paper venues after restart",
                    {"positions": restored, "balances": sorted(latest_balance)},
                )
        return {"positions_restored": restored, "balances_restored": len(latest_balance)}

    async def sync_reference_data(self) -> None:
        """Write the YAML catalogue into the database."""
        async with self.database.session() as session:
            instruments = InstrumentRepository(session)
            mappings = MappingRepository(session)
            rows: dict[str, Any] = {}
            for spec in self.registry.all():
                rows[spec.key] = await instruments.upsert(spec)
            for mapping in self.registry.mappings():
                await mappings.upsert(
                    mapping.name, rows[mapping.source_key], rows[mapping.hedge_key],
                    enabled=mapping.enabled,
                )
            obs = ObservabilityRepository(session)
            for quote in self.fx.all_quotes():
                await obs.record_fx(quote.base, quote.quote, quote.rate, quote.source)
            await obs.record_system_event(
                "REFERENCE_DATA_SYNC", "INFO", "service",
                f"synchronised {len(rows)} instruments and {len(self.registry.mappings())} mappings",
                {"instruments": sorted(rows)},
            )

    # ==================================================================
    # market data
    # ==================================================================
    def advance_market(self, steps: int = 1) -> None:
        """Advance the simulated market and sample it for statistics.

        Sampling happens here rather than in the simulator so the estimator
        sees exactly the prices the rest of the platform sees, and so a
        backtest or a test can drive it the same way.
        """
        for _ in range(max(0, steps)):
            self.simulator.advance(1)
            self.stats.observe_many(
                {key: ticker.mid for key, ticker in self.simulator.tickers().items()},
                self.simulator.clock,
            )

    def tickers(self, venue: str | None = None) -> dict[str, Ticker]:
        return self.simulator.tickers(venue)

    def ticker(self, key: str) -> Ticker:
        return self.simulator.ticker(key)

    def set_scenario(
        self, scenario: MarketScenario, venue: str | None = None, **overrides: object
    ) -> dict[str, Any]:
        profile = self.simulator.set_scenario(scenario, venue, **overrides)
        return {
            "scenario": profile.scenario.value,
            "description": profile.description,
            "venue": venue or "ALL",
            "active": self.simulator.active_scenarios(),
        }

    def apply_shock(self, underlying: str, pct_move: Decimal) -> dict[str, str]:
        price = self.simulator.apply_shock(underlying, dec(pct_move))
        return {"underlying": underlying, "price": str(price), "pct_move": str(pct_move)}

    def set_funding_rate(self, instrument_key: str, rate: Decimal) -> dict[str, str]:
        self.simulator.set_funding_rate(instrument_key, dec(rate))
        return {"instrument": instrument_key, "funding_rate": str(rate)}

    async def persist_market_snapshot(self) -> int:
        async with self.database.session() as session:
            obs = ObservabilityRepository(session)
            tickers = self.simulator.tickers()
            for ticker in tickers.values():
                await obs.record_ticker(ticker, source=self.settings.market_data_source)
            return len(tickers)

    # ==================================================================
    # hedge calculation
    # ==================================================================
    async def build_inputs(
        self,
        *,
        source_key: str,
        hedge_key: str,
        source_quantity: Decimal,
        objective: HedgeObjective,
        target_ratio: Decimal = Decimal(1),
        risk_params: RiskParameters | None = None,
        use_live_positions: bool = True,
    ) -> HedgeInputs:
        source_spec = self.registry.get(source_key)
        hedge_spec = self.registry.get(hedge_key)
        hedge_venue = self.venues[hedge_spec.venue]

        source_position = Position(venue=source_spec.venue, symbol=source_spec.symbol)
        hedge_position = Position(venue=hedge_spec.venue, symbol=hedge_spec.symbol)
        hedge_account: AccountSnapshot | None = None
        if use_live_positions:
            source_position = await self._position(source_spec)
            hedge_position = await self._position(hedge_spec)
            hedge_account = await hedge_venue.get_balance()

        return HedgeInputs(
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_quantity=source_quantity,
            source_ticker=self.simulator.ticker(source_key),
            hedge_ticker=self.simulator.ticker(hedge_key),
            objective=objective,
            target_ratio=target_ratio,
            current_hedge_quantity=hedge_position.quantity,
            source_entry_price=source_position.average_entry or None,
            hedge_entry_price=hedge_position.average_entry or None,
            hedge_book=self.simulator.orderbook(hedge_key, 12),
            hedge_account=hedge_account,
            risk_params=self.risk_parameters_for(source_key, hedge_key, risk_params)[0],
            account_currency=self.settings.account_currency,
        )

    def risk_parameters_for(
        self,
        source_key: str,
        hedge_key: str,
        base: RiskParameters | None = None,
    ) -> tuple[RiskParameters, PairEstimate]:
        """Overlay measured statistics onto the caller's parameters.

        Falls back to the supplied (or default) assumptions when there is not
        yet enough data, and says so through ``provenance`` -- a number derived
        from twelve observations should not be presented as a measurement.
        """
        params = base or RiskParameters()
        estimate = self.stats.pair_estimate(source_key, hedge_key)
        if not estimate.is_reliable:
            return params, estimate
        return (
            params.with_estimates(
                source_daily_vol=estimate.source.daily_volatility,
                hedge_daily_vol=estimate.hedge.daily_volatility,
                correlation=estimate.correlation,
                provenance=estimate.note,
            ),
            estimate,
        )

    async def calculate_hedge(self, **kwargs: Any) -> HedgeCalculation:
        inputs = await self.build_inputs(**kwargs)
        return self.calculator.calculate(inputs)

    async def optimize_hedge(
        self, config: OptimizerConfig, **kwargs: Any
    ) -> OptimizationResult:
        inputs = await self.build_inputs(**kwargs)
        return self.optimizer.optimize(inputs, config)

    # ==================================================================
    # execution
    # ==================================================================
    async def execute_hedge(
        self,
        mapping_name: str,
        source_quantity: Decimal,
        *,
        objective: HedgeObjective | None = None,
        target_ratio: Decimal | None = None,
        optimizer: OptimizerConfig | None = None,
        risk_params: RiskParameters | None = None,
        reason: str = "operator request",
    ) -> ExecutionResult:
        """Run a full hedge cycle.  Blocked when trading is paused."""
        mapping = self.registry.mapping_by_name(mapping_name)
        self._guard_trading(mapping)
        request = HedgeRequest(
            mapping=mapping,
            source_quantity=dec(source_quantity),
            objective=objective,
            target_ratio=target_ratio,
            optimizer=optimizer,
            risk_params=risk_params or RiskParameters(),
            reason=reason,
        )
        result = await self.coordinator.execute(request)
        await self._persist_positions()
        await self._audit(
            action="EXECUTE_HEDGE", entity_type="hedge_cycle",
            entity_id=result.cycle.cycle_id,
            after={"state": result.cycle.state.value, "pair": mapping.name,
                   "source_quantity": str(source_quantity), "reason": reason},
        )
        return result

    async def assess_rebalance(self, mapping_name: str) -> RebalanceAssessment:
        mapping = self.registry.mapping_by_name(mapping_name)
        return await self.rebalancer.assess(mapping=mapping)

    async def rebalance(self, mapping_name: str) -> dict[str, Any]:
        mapping = self.registry.mapping_by_name(mapping_name)
        self._guard_trading(mapping)
        with correlation_scope(new_correlation_id()):
            before = await self.rebalancer.assess(mapping=mapping)
            if not before.needs_rebalance:
                return {"rebalanced": False, "assessment": before.to_dict()}
            orders = await self.rebalancer.execute(before)
            after = await self.rebalancer.assess(mapping=mapping)
            await self._persist_positions()
            await self._audit(
                action="REBALANCE", entity_type="hedge_pair", entity_id=mapping.name,
                before={"residual_bps": str(quantize(before.residual_bps, 4))},
                after={"residual_bps": str(quantize(after.residual_bps, 4))},
            )
            return {
                "rebalanced": True,
                "orders": [o.order_id for o in orders],
                "before": before.to_dict(),
                "after": after.to_dict(),
            }

    async def open_source_position(
        self, instrument_key: str, quantity: Decimal
    ) -> Order:
        """Take a naked position on the source venue.

        Used by the demo to create something that *needs* hedging.  It goes
        through the same paper venue as everything else -- there is no
        back-door that writes a position directly.
        """
        spec = self.registry.get(instrument_key)
        signed = dec(quantity)
        request = OrderRequest(
            venue=spec.venue, symbol=spec.symbol,
            side=Side.BUY if signed > ZERO else Side.SELL,
            quantity=abs(signed),
        )
        order = await self.venues[spec.venue].place_order(request)
        await self.recorder.record_order(order, None)
        await self._persist_positions()
        return order

    # ==================================================================
    # risk
    # ==================================================================
    async def pair_risk(self, mapping_name: str) -> PairRisk:
        mapping = self.registry.mapping_by_name(mapping_name)
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_position = await self._position(source_spec)
        hedge_position = await self._position(hedge_spec)
        funding = await self.funding_projection(mapping_name)
        return self.risk_engine.evaluate(PairRiskInputs(
            pair_name=mapping.name,
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_position=source_position,
            hedge_position=hedge_position,
            source_ticker=self.simulator.ticker(source_spec.key),
            hedge_ticker=self.simulator.ticker(hedge_spec.key),
            source_account=await self.venues[source_spec.venue].get_balance(),
            hedge_account=await self.venues[hedge_spec.venue].get_balance(),
            funding_per_day=funding.net_per_day,
            daily_pnl=await self._daily_pnl(),
            account_currency=self.settings.account_currency,
        ))

    async def all_pair_risks(self) -> list[PairRisk]:
        risks: list[PairRisk] = []
        for mapping in self.registry.mappings(enabled_only=True):
            try:
                risks.append(await self.pair_risk(mapping.name))
            except VenueError as exc:
                log.error(
                    "cannot evaluate pair risk: venue unreachable",
                    extra={"pair": mapping.name, "error": str(exc)},
                )
        return risks

    async def portfolio_risk(self) -> PortfolioRisk:
        risks = await self.all_pair_risks()
        accounts = await self.accounts()
        underlying_by_pair = {
            m.name: self.registry.get(m.source_key).effective_underlying_key
            for m in self.registry.mappings(enabled_only=True)
        }
        currency_exposure = await self._currency_exposure()
        return self.portfolio_engine.evaluate(
            risks, accounts,
            underlying_by_pair=underlying_by_pair,
            currency_exposure=currency_exposure,
            currency=self.settings.account_currency,
        )

    async def persist_risk(self) -> dict[str, int]:
        """Snapshot risk, margin and P&L into the audit tables."""
        pair_risks = await self.all_pair_risks()
        portfolio = await self.portfolio_risk()
        accounts = await self.accounts()
        written = {"risk_events": 0, "margin_snapshots": 0, "pnl_records": 0}

        async with self.database.session() as session:
            obs = ObservabilityRepository(session)
            for risk in pair_risks:
                for breach in risk.breaches:
                    await obs.record_risk_event(
                        level=breach.level.value, scope="PAIR", metric=breach.metric,
                        value=breach.value, threshold=breach.threshold,
                        message=breach.message, pair_name=risk.pair_name,
                    )
                    written["risk_events"] += 1
            for breach in portfolio.breaches:
                await obs.record_risk_event(
                    level=breach.level.value, scope="PORTFOLIO", metric=breach.metric,
                    value=breach.value, threshold=breach.threshold, message=breach.message,
                )
                written["risk_events"] += 1
            for account in accounts:
                await obs.record_margin_snapshot(account)
                written["margin_snapshots"] += 1
            for mapping in self.registry.mappings(enabled_only=True):
                breakdown = await self.pair_pnl(mapping.name)
                await obs.record_pnl(mapping.name, breakdown.to_dict(), pair_name=mapping.name)
                written["pnl_records"] += 1
        return written

    # ==================================================================
    # emergency handling
    # ==================================================================
    async def apply_emergency_actions(
        self, level: RiskLevel, actions: tuple[EmergencyAction, ...], trigger: str,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute the risk engine's prescribed actions and record each one."""
        applied: list[dict[str, Any]] = []
        for action in actions:
            executed, result = await self._apply_action(action, scope)
            applied.append({"action": action.value, "executed": executed, "result": result})
            async with self.database.session() as session:
                await ObservabilityRepository(session).record_emergency_action(
                    trigger=trigger, level=level.value, action=action.value,
                    scope=scope, executed=executed, result=result,
                )
        return applied

    async def _apply_action(
        self, action: EmergencyAction, scope: str | None
    ) -> tuple[bool, str]:
        if action is EmergencyAction.STOP_NEW_TRADES:
            self.state.trading_paused = True
            return True, "new hedge cycles are blocked"
        if action is EmergencyAction.PAUSE_PAIR:
            if scope:
                self.state.paused_pairs.add(scope)
                return True, f"pair {scope} paused"
            return False, "no pair scope supplied"
        if action is EmergencyAction.ENTER_EMERGENCY_MODE:
            self.state.emergency_mode = True
            return True, "emergency mode engaged"
        if action is EmergencyAction.CANCEL_OPEN_ORDERS:
            cancelled = await self.cancel_all_orders()
            return True, f"cancelled {cancelled} working orders"
        if action is EmergencyAction.REBALANCE:
            if scope:
                outcome = await self.rebalancer.assess(
                    mapping=self.registry.mapping_by_name(scope)
                )
                if outcome.needs_rebalance:
                    orders = await self.rebalancer.execute(outcome)
                    return True, f"rebalanced with {len(orders)} order(s)"
                return False, outcome.reason
            return False, "no pair scope supplied"
        if action is EmergencyAction.REDUCE_EXPOSURE:
            return await self._reduce_exposure(scope)
        if action is EmergencyAction.FLATTEN_POSITIONS:
            return await self._flatten(scope)
        return False, f"unhandled action {action.value}"

    async def _reduce_exposure(self, scope: str | None) -> tuple[bool, str]:
        """Halve the source leg and re-hedge, rather than closing outright."""
        mappings = (
            [self.registry.mapping_by_name(scope)] if scope
            else self.registry.mappings(enabled_only=True)
        )
        messages: list[str] = []
        for mapping in mappings:
            source_spec = self.registry.get(mapping.source_key)
            position = await self._position(source_spec)
            if position.is_flat:
                continue
            reduction = -position.quantity / Decimal(2)
            request = HedgeRequest(
                mapping=mapping, source_quantity=reduction,
                bypass_risk=True, reason="emergency exposure reduction",
            )
            result = await self.coordinator.execute(request)
            messages.append(f"{mapping.name}: {result.cycle.state.value}")
        await self._persist_positions()
        return bool(messages), "; ".join(messages) or "no open source positions to reduce"

    async def _flatten(self, scope: str | None) -> tuple[bool, str]:
        """Close every position, then **verify** it worked.

        Reporting success without checking is how a kill switch quietly leaves
        exposure open after a partial fill.  The remaining positions are named
        in the result so the operator sees exactly what is still on the book.
        """
        mappings = (
            [self.registry.mapping_by_name(scope)] if scope
            else self.registry.mappings()
        )
        total = 0
        for mapping in mappings:
            orders = await self.rebalancer.flatten(mapping)
            total += len(orders)
        await self._persist_positions()

        unreachable = [
            venue.name for venue in self.venues.values()
            if (engine := getattr(venue, "engine", None)) is not None
            and not engine.is_connected
        ]
        remaining = [p for p in await self.positions() if not p.is_flat]
        if unreachable:
            detail = ", ".join(f"{p.key}={p.quantity}" for p in remaining) or "none visible"
            log.error(
                "flatten could not confirm every venue",
                extra={"unreachable": unreachable, "remaining": detail},
            )
            return False, (
                f"{total} closing order(s) placed, but {', '.join(unreachable)} "
                f"could not be reached, so its exposure is unconfirmed "
                f"(visible remainder: {detail})"
            )
        if remaining:
            detail = ", ".join(f"{p.key}={p.quantity}" for p in remaining)
            log.error(
                "flatten left open positions",
                extra={"remaining": detail, "orders": total},
            )
            return False, (
                f"{total} closing order(s) placed but {len(remaining)} position(s) "
                f"remain open: {detail}"
            )
        return True, f"flattened successfully with {total} closing order(s)"

    async def engage_kill_switch(self, reason: str, actor: str = "operator") -> dict[str, Any]:
        """Stop everything and flatten every position.

        Deliberately not reversible from inside the API: clearing it is a
        separate, audited call.
        """
        with correlation_scope(new_correlation_id()):
            log.error("KILL SWITCH ENGAGED", extra={"reason": reason, "actor": actor})
            self.state.kill_switch_engaged = True
            self.state.trading_paused = True
            self.state.emergency_mode = True
            cancelled = await self.cancel_all_orders()
            flattened, message = await self._flatten(None)
            async with self.database.session() as session:
                obs = ObservabilityRepository(session)
                for action, executed, result in (
                    (EmergencyAction.STOP_NEW_TRADES, True, "trading halted"),
                    (EmergencyAction.CANCEL_OPEN_ORDERS, True, f"{cancelled} orders cancelled"),
                    (EmergencyAction.FLATTEN_POSITIONS, flattened, message),
                    (EmergencyAction.ENTER_EMERGENCY_MODE, True, "emergency mode engaged"),
                ):
                    await obs.record_emergency_action(
                        trigger=reason, level=RiskLevel.KILL_SWITCH.value,
                        action=action.value, scope=None, executed=executed,
                        result=result, actor=actor,
                    )
                await obs.record_audit(
                    actor=actor, action="KILL_SWITCH", entity_type="system",
                    entity_id=None, before={"kill_switch_engaged": False},
                    after={"kill_switch_engaged": True, "reason": reason},
                )
            return {
                "engaged": True, "reason": reason,
                "orders_cancelled": cancelled, "flatten_result": message,
            }

    async def clear_kill_switch(self, actor: str = "operator") -> dict[str, Any]:
        self.state.kill_switch_engaged = False
        self.state.trading_paused = False
        self.state.emergency_mode = False
        self.state.paused_pairs.clear()
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_audit(
                actor=actor, action="CLEAR_KILL_SWITCH", entity_type="system",
                entity_id=None, before={"kill_switch_engaged": True},
                after={"kill_switch_engaged": False},
            )
        return {"engaged": False}

    async def cancel_all_orders(self) -> int:
        cancelled = 0
        for venue in self.venues.values():
            try:
                for order in await venue.get_open_orders():
                    await venue.cancel_order(order.order_id)
                    cancelled += 1
            except VenueError as exc:
                log.error(
                    "cannot cancel orders on an unreachable venue",
                    extra={"venue": venue.name, "error": str(exc)},
                )
        return cancelled

    # ==================================================================
    # costs and P&L
    # ==================================================================
    async def funding_projection(
        self, mapping_name: str, horizon_days: Decimal = Decimal(1)
    ) -> FundingProjection:
        mapping = self.registry.mapping_by_name(mapping_name)
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_position = await self._position(source_spec)
        hedge_position = await self._position(hedge_spec)
        source_ticker = self.simulator.ticker(source_spec.key)
        hedge_ticker = self.simulator.ticker(hedge_spec.key)
        acct = self.settings.account_currency
        return project_funding(
            source_spec=source_spec, hedge_spec=hedge_spec,
            source_quantity=source_position.quantity,
            hedge_quantity=hedge_position.quantity,
            source_price=source_ticker.mid, hedge_price=hedge_ticker.mid,
            source_funding_rate=source_ticker.funding_rate,
            hedge_funding_rate=hedge_ticker.funding_rate,
            source_fx=self.fx.try_rate(source_spec.quote_asset, acct),
            hedge_fx=self.fx.try_rate(hedge_spec.quote_asset, acct),
            horizon_days=horizon_days, currency=acct,
        )

    async def pair_pnl(self, mapping_name: str) -> PnLBreakdown:
        mapping = self.registry.mapping_by_name(mapping_name)
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_leg = self._leg_accounting(source_spec)
        hedge_leg = self._leg_accounting(hedge_spec)
        return self.pnl_calculator.for_pair(
            scope=mapping.name, source=source_leg, hedge=hedge_leg,
            source_ticker=self.simulator.ticker(source_spec.key),
            hedge_ticker=self.simulator.ticker(hedge_spec.key),
        )

    def _leg_accounting(self, spec: InstrumentSpec) -> LegAccounting:
        """Assemble a leg's cost tally from the paper engine's own records."""
        engine = getattr(self.venues[spec.venue], "engine", None)
        position = Position(venue=spec.venue, symbol=spec.symbol)
        leg = LegAccounting(spec=spec, position=position)
        if engine is None:
            return leg
        leg.position = engine.positions.get(spec.key, position)
        order_ids = {
            order.order_id for order in engine.orders.values()
            if order.request.symbol == spec.symbol
        }
        leg.fills = [f for f in engine.fills if f.order_id in order_ids]
        for accrual in engine.funding_history:
            if accrual.symbol != spec.symbol:
                continue
            if accrual.kind == "SWAP":
                leg.swap_financing += accrual.amount
            elif accrual.amount >= ZERO:
                leg.funding_received += accrual.amount
            else:
                leg.funding_paid += -accrual.amount
        return leg

    def settle_funding(self, hours: Decimal | None = None) -> list[dict[str, Any]]:
        """Charge one funding interval / swap night across every venue."""
        accruals: list[dict[str, Any]] = []
        for venue in self.venues.values():
            engine = getattr(venue, "engine", None)
            if engine is None:
                continue
            accruals.extend(a.to_dict() for a in engine.apply_funding(hours))
        return accruals

    async def persist_funding(self, accruals: list[dict[str, Any]]) -> int:
        async with self.database.session() as session:
            obs = ObservabilityRepository(session)
            for accrual in accruals:
                await obs.record_funding(
                    venue=str(accrual["venue"]), symbol=str(accrual["symbol"]),
                    kind=str(accrual["kind"]), rate=dec(accrual["rate"]),
                    amount=dec(accrual["amount"]), currency=str(accrual["currency"]),
                )
        return len(accruals)

    # ==================================================================
    # reconciliation
    # ==================================================================
    async def reconcile(self) -> ReconciliationReport:
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            open_orders = [o.order_id for o in await OrderRepository(session).open_orders()]
        report = await self.reconciler.reconcile(positions, open_orders)
        self.state.last_reconciliation = report
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_system_event(
                "RECONCILIATION", "WARNING" if not report.is_clean else "INFO",
                "reconciliation",
                f"{len(report.discrepancies)} discrepancies across {len(report.checked_venues)} venues",
                report.to_dict(),
            )
        return report

    async def recover(self) -> RecoveryPlan:
        """Restart recovery: rebuild state and decide whether to resume."""
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            open_orders = [o.order_id for o in await OrderRepository(session).open_orders()]
            cycles = [
                {
                    "cycle_id": row.cycle_id, "pair_name": row.pair_name, "state": row.state,
                    "source_filled_quantity": str(row.source_filled_quantity),
                    "hedge_filled_quantity": str(row.hedge_filled_quantity),
                }
                for row in await CycleRepository(session).unfinished()
            ]
        plan = await self.reconciler.recover(
            database_positions=positions, unfinished_cycles=cycles,
            database_open_orders=open_orders,
        )
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_system_event(
                "RESTART_RECOVERY", "INFO" if plan.resumable else "WARNING", "reconciliation",
                f"recovery {'can' if plan.resumable else 'cannot'} resume automatically",
                plan.to_dict(),
            )
        return plan

    async def adopt_venue_state(self, actor: str = "operator") -> dict[str, Any]:
        """Overwrite the database with what the venues actually hold."""
        positions = await self.reconciler.adopt_venue_state()
        async with self.database.session() as session:
            repo = PositionRepository(session)
            before = [
                {"key": p.key, "quantity": str(p.quantity)} for p in await repo.to_domain()
            ]
            await repo.clear()
            for position in positions:
                await repo.upsert(position)
            await ObservabilityRepository(session).record_audit(
                actor=actor, action="ADOPT_VENUE_STATE", entity_type="positions",
                entity_id=None, before={"positions": before},
                after={"positions": [{"key": p.key, "quantity": str(p.quantity)} for p in positions]},
            )
        return {"adopted": len(positions)}

    # ==================================================================
    # fault injection
    # ==================================================================
    async def inject_fault(
        self,
        kind: FaultKind,
        *,
        venue: str | None = None,
        symbol: str | None = None,
        leg: str | None = None,
        count: int | None = 1,
        magnitude: Decimal = Decimal("0.5"),
        probability: Decimal = Decimal(1),
        reason: str = "operator-injected fault",
    ) -> dict[str, Any]:
        """Arm a fault, or apply one that acts immediately.

        Disconnects, stale data and price gaps are *states*, not events, so they
        are applied directly to the simulator instead of being armed.
        """
        immediate = await self._apply_immediate_fault(kind, venue, symbol, magnitude)
        if immediate is not None:
            await self._audit(
                action="INJECT_FAULT", entity_type="fault", entity_id=kind.value,
                after={"kind": kind.value, "venue": venue, "immediate": True, **immediate},
            )
            return {"kind": kind.value, "armed": False, "applied": True, **immediate}

        fault: ArmedFault = self.faults.arm(
            kind, venue=venue, symbol=symbol, leg=leg, count=count,
            probability=probability, magnitude=magnitude, reason=reason,
        )
        await self._audit(
            action="INJECT_FAULT", entity_type="fault", entity_id=kind.value,
            after=fault.to_dict(),
        )
        return {"kind": kind.value, "armed": True, "applied": False, "fault": fault.to_dict()}

    async def _apply_immediate_fault(
        self, kind: FaultKind, venue: str | None, symbol: str | None, magnitude: Decimal
    ) -> dict[str, Any] | None:
        if kind is FaultKind.DELTA_DISCONNECT:
            return self._disconnect("PAPER_DELTA")
        if kind is FaultKind.MT5_DISCONNECT:
            return self._disconnect("PAPER_MT5")
        if kind is FaultKind.STALE_MARKET_DATA:
            self.simulator.set_scenario(MarketScenario.STALE_DATA, venue)
            return {"scenario": MarketScenario.STALE_DATA.value, "venue": venue or "ALL"}
        if kind is FaultKind.WIDE_SPREAD:
            self.simulator.set_scenario(MarketScenario.SPREAD_WIDENING, venue)
            return {"scenario": MarketScenario.SPREAD_WIDENING.value, "venue": venue or "ALL"}
        if kind is FaultKind.PRICE_GAP:
            underlying = symbol or "BTC"
            price = self.simulator.apply_shock(underlying, -abs(magnitude))
            return {"underlying": underlying, "new_price": str(price),
                    "pct_move": str(-abs(magnitude))}
        if kind is FaultKind.UNEXPECTED_POSITION:
            target_venue = venue or "PAPER_MT5"
            engine = getattr(self.venues[target_venue], "engine", None)
            if engine is None:
                return {"error": f"{target_venue} has no paper engine"}
            target_symbol = symbol or next(iter(engine.instruments.values())).symbol
            spec = engine.spec(target_symbol)
            price = self.simulator.ticker(spec.key).mid
            quantity = spec.min_quantity * Decimal(10)
            engine.inject_unexpected_position(target_symbol, quantity, price)
            return {"venue": target_venue, "symbol": target_symbol,
                    "quantity": str(quantity),
                    "note": "position created on the venue only; the database does not know it"}
        return None

    def _disconnect(self, venue_name: str) -> dict[str, Any]:
        engine = getattr(self.venues[venue_name], "engine", None)
        if engine is None:
            return {"error": f"{venue_name} has no paper engine"}
        engine.force_disconnect(True)
        return {"venue": venue_name, "connected": False}

    def reconnect(self, venue_name: str) -> dict[str, Any]:
        engine = getattr(self.venues[venue_name], "engine", None)
        if engine is None:
            return {"error": f"{venue_name} has no paper engine"}
        engine.force_disconnect(False)
        self.simulator.clear_scenario(venue_name)
        return {"venue": venue_name, "connected": True}

    def clear_faults(self, kind: FaultKind | None = None) -> dict[str, Any]:
        removed = self.faults.disarm(kind)
        for name in self.venues:
            self.reconnect(name)
        self.simulator.clear_scenario()
        return {"disarmed": removed, "scenario": "NORMAL", "venues_reconnected": list(self.venues)}

    # ==================================================================
    # accounts and status
    # ==================================================================
    async def accounts(self) -> list[AccountSnapshot]:
        snapshots: list[AccountSnapshot] = []
        for venue in self.venues.values():
            snapshots.append(await venue.get_balance())
        return snapshots

    async def positions(self) -> list[Position]:
        positions: list[Position] = []
        for venue in self.venues.values():
            try:
                positions.extend(await venue.get_positions())
            except VenueError:
                continue
        return positions

    async def status(self) -> dict[str, Any]:
        venue_health: list[dict[str, Any]] = []
        for venue in self.venues.values():
            venue_health.append(await venue.health())
        return {
            "app": self.settings.app_name,
            "version": self.settings.version,
            "environment": self.settings.environment,
            "trading_mode": self.settings.trading_mode.value,
            "paper_only": True,
            "live_adapters_registered": 0,
            "database_connected": await self.database.ping(),
            "venues": venue_health,
            "instruments": len(self.specs),
            "mappings": len(self.registry.mappings()),
            "market_scenarios": self.simulator.active_scenarios(),
            "armed_faults": [f.to_dict() for f in self.faults.armed()],
            "emergency_mode": self.state.emergency_mode,
            "trading_paused": self.state.trading_paused,
            "kill_switch_engaged": self.state.kill_switch_engaged,
            "paused_pairs": sorted(self.state.paused_pairs),
            "simulator_clock": self.simulator.clock.isoformat(),
        }

    # ==================================================================
    # internals
    # ==================================================================
    def _guard_trading(self, mapping: HedgeMapping) -> None:
        if self.state.kill_switch_engaged:
            raise PermissionError("kill switch is engaged; trading is disabled")
        if self.state.trading_paused:
            raise PermissionError("trading is paused by a risk action")
        if mapping.name in self.state.paused_pairs:
            raise PermissionError(f"hedge pair {mapping.name!r} is paused")

    async def _position(self, spec: InstrumentSpec) -> Position:
        venue = self.venues[spec.venue]
        for position in await venue.get_positions():
            if position.symbol == spec.symbol:
                return position
        return Position(venue=spec.venue, symbol=spec.symbol)

    async def _persist_positions(self) -> None:
        async with self.database.session() as session:
            repo = PositionRepository(session)
            for venue in self.venues.values():
                engine = getattr(venue, "engine", None)
                if engine is None:
                    continue
                for position in engine.positions.values():
                    await repo.upsert(position)

    async def _currency_exposure(self) -> dict[str, Decimal]:
        exposure: dict[str, Decimal] = {}
        for position in await self.positions():
            spec = self.registry.find(position.key)
            if spec is None or position.is_flat:
                continue
            from .domain.quantity import QuantityConverter

            notional = QuantityConverter(spec).notional_quote(
                position.quantity, self.simulator.ticker(spec.key).mid
            )
            exposure[spec.settlement_asset] = exposure.get(spec.settlement_asset, ZERO) + notional
        return exposure

    async def _daily_pnl(self) -> Decimal:
        async with self.database.session() as session:
            return await ObservabilityRepository(session).daily_pnl()

    def _risk_thresholds(self) -> RiskThresholds:
        s = self.settings
        return RiskThresholds(
            warning_margin_level=s.warning_margin_level,
            danger_margin_level=s.danger_margin_level,
            emergency_margin_level=s.emergency_margin_level,
            kill_switch_margin_level=s.kill_switch_margin_level,
            max_daily_loss=s.max_daily_loss,
            warning_residual_bps=s.max_residual_exposure_bps,
            max_spread_bps=s.max_spread_bps_for_execution,
        )

    def _portfolio_limits(self) -> PortfolioLimits:
        s = self.settings
        return PortfolioLimits(
            max_total_notional=s.max_portfolio_notional,
            max_daily_loss=s.max_daily_loss,
            max_concentration_pct=s.max_concentration_pct,
        )

    async def _audit(
        self, *, action: str, entity_type: str, entity_id: str | None,
        before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> None:
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_audit(
                actor=actor, action=action, entity_type=entity_type,
                entity_id=entity_id, before=before, after=after,
            )
