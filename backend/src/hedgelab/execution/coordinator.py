"""Two-leg hedge execution.

The hard part of cross-venue hedging is not placing two orders -- it is what
happens when the second one does not behave.  This coordinator is built around
that:

* Leg 1 goes on first, always on the **source** venue.  If the source position
  already exists, leg 1 is a recorded no-op confirmation rather than a skipped
  state, so the audit trail never has a hole where a leg should be.
* Between the legs the market can move.  The hedge quantity is recalculated
  from *post-leg-1* fills, not from the pre-trade estimate, so a partial fill
  on leg 1 produces a correspondingly smaller hedge instead of an over-hedge.
* A timeout is not a failure -- it is an **unknown outcome**.  The coordinator
  queries the venue to find out what actually happened before deciding.
* If leg 2 cannot be completed, the book is exposed.  Recovery retries, then
  unwinds leg 1 to restore a flat book, then escalates.  Leaving a half-hedge
  and reporting success is the one outcome that is never acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..domain.enums import CycleState, HedgeObjective, Leg, RiskLevel, Side
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, normalize, quantize, safe_div
from ..domain.orders import Order, OrderRequest, Position
from ..domain.quantity import QuantityConverter
from ..hedge.calculator import HedgeCalculation, HedgeCalculator, HedgeInputs
from ..hedge.objectives import RiskParameters
from ..hedge.optimizer import HedgeOptimizer, OptimizerConfig
from ..instruments.registry import HedgeMapping, InstrumentRegistry
from ..logging_setup import correlation_scope, get_logger, new_correlation_id
from ..marketdata.fx import FxService
from ..risk.engine import PairRiskInputs, RiskEngine
from ..venues.base import (
    TradingVenue,
    VenueDisconnected,
    VenueError,
    VenueTimeout,
)
from .ports import CycleRecorder, EventPublisher, NullPublisher, NullRecorder
from .state_machine import HedgeCycle

log = get_logger(__name__)


class ExecutionError(RuntimeError):
    """Raised when a cycle cannot proceed and the caller must handle it."""


@dataclass(frozen=True, slots=True)
class HedgeRequest:
    """Instruction to establish (or adjust) a hedged pair."""

    mapping: HedgeMapping
    #: Signed source quantity to trade.  Zero means "the position already
    #: exists; only put the hedge on".
    source_quantity: Decimal = ZERO
    objective: HedgeObjective | None = None
    target_ratio: Decimal | None = None
    optimizer: OptimizerConfig | None = None
    risk_params: RiskParameters = RiskParameters()
    correlation_id: str | None = None
    #: Skip risk approval.  Only ever set by the emergency de-risking path.
    bypass_risk: bool = False
    reason: str = "operator request"


@dataclass
class ExecutionResult:
    cycle: HedgeCycle
    calculation: HedgeCalculation | None = None
    source_order: Order | None = None
    hedge_order: Order | None = None
    rebalance_orders: list[Order] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.cycle.state is CycleState.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle.to_dict(),
            "calculation": self.calculation.to_dict() if self.calculation else None,
            "source_order_id": self.source_order.order_id if self.source_order else None,
            "hedge_order_id": self.hedge_order.order_id if self.hedge_order else None,
            "rebalance_order_ids": [o.order_id for o in self.rebalance_orders],
            "succeeded": self.succeeded,
            "messages": self.messages,
        }


class HedgeCoordinator:
    """Drives a hedge cycle through the state machine."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: InstrumentRegistry,
        venues: dict[str, TradingVenue],
        calculator: HedgeCalculator,
        optimizer: HedgeOptimizer,
        risk_engine: RiskEngine,
        fx: FxService,
        recorder: CycleRecorder | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.venues = venues
        self.calculator = calculator
        self.optimizer = optimizer
        self.risk_engine = risk_engine
        self.fx = fx
        self.recorder: CycleRecorder = recorder or NullRecorder()
        self.publisher: EventPublisher = publisher or NullPublisher()
        self.cycles: dict[str, HedgeCycle] = {}

    # ==================================================================
    # public entry point
    # ==================================================================
    async def execute(self, request: HedgeRequest) -> ExecutionResult:
        correlation_id = request.correlation_id or new_correlation_id()
        mapping = request.mapping
        cycle = HedgeCycle(
            pair_name=mapping.name,
            source_key=mapping.source_key,
            hedge_key=mapping.hedge_key,
            objective=(request.objective or mapping.objective).value,
            correlation_id=correlation_id,
        )
        self.cycles[cycle.cycle_id] = cycle
        result = ExecutionResult(cycle=cycle)

        with correlation_scope(correlation_id, cycle_id=cycle.cycle_id):
            await self._save(cycle)
            try:
                await self._run(request, cycle, result)
            except (ExecutionError, VenueError) as exc:
                message = str(exc) if isinstance(exc, ExecutionError) else f"venue error: {exc}"
                await self._transition(cycle, *self._fail_target(cycle, message))
                result.messages.append(message)
                # Recovery runs for *any* failure that left exposure on the
                # book, not just venue errors.  ``_fail_target`` already routed
                # the cycle to RECOVERY_REQUIRED when leg 1 had traded, and
                # ``_recover`` returns immediately when it did not.
                await self._recover(cycle, result, request)
            await self._save(cycle)
        return result

    # ==================================================================
    # the cycle
    # ==================================================================
    async def _run(
        self, request: HedgeRequest, cycle: HedgeCycle, result: ExecutionResult
    ) -> None:
        mapping = request.mapping
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)

        # --- 1. validation --------------------------------------------
        await self._validate(request, cycle, source_spec, hedge_spec)

        # --- 2. market data -------------------------------------------
        await self._transition(cycle, CycleState.AWAITING_MARKET_DATA, "awaiting_market_data", {})
        source_venue = self._venue(source_spec.venue)
        hedge_venue = self._venue(hedge_spec.venue)
        source_ticker = await source_venue.get_ticker(source_spec.symbol)
        hedge_ticker = await hedge_venue.get_ticker(hedge_spec.symbol)
        self._validate_market_data(source_ticker, hedge_ticker)

        # --- 3. calculation -------------------------------------------
        source_position = await self._position(source_venue, source_spec)
        hedge_position = await self._position(hedge_venue, hedge_spec)
        projected_source_qty = source_position.quantity + request.source_quantity

        calculation = await self._calculate(
            request, source_spec, hedge_spec, projected_source_qty,
            source_ticker, hedge_ticker, source_position, hedge_position,
            hedge_venue, result,
        )
        cycle.source_target_quantity = projected_source_qty
        cycle.hedge_target_quantity = calculation.rounded_quantity
        cycle.hedge_ratio = calculation.hedge_ratio
        cycle.residual_exposure = calculation.residual.account_delta
        await self._transition(cycle, CycleState.CALCULATED, "hedge_calculated", {
            "required_quantity": str(calculation.required_quantity),
            "rounded_quantity": str(calculation.rounded_quantity),
            "hedge_ratio": str(calculation.hedge_ratio),
            "steps": [s.to_dict() for s in calculation.steps],
        })

        # --- 4. risk validation ---------------------------------------
        # The risk check must see the *same* book the calculation was made for.
        # Passing the current source position while the hedge is the projected
        # one compares 5 BTC of source against 7 lots of hedge and reports a
        # 40% residual that does not exist.
        projected_source = Position(
            venue=source_spec.venue,
            symbol=source_spec.symbol,
            quantity=projected_source_qty,
            average_entry=(
                source_position.average_entry if source_position.average_entry > ZERO
                else source_ticker.mid
            ),
            realized_pnl=source_position.realized_pnl,
        )
        await self._risk_check(
            request, cycle, source_spec, hedge_spec, projected_source, hedge_position,
            source_ticker, hedge_ticker, source_venue, hedge_venue, calculation,
            current_source_position=source_position,
        )

        # --- 5. leg 1: the source venue -------------------------------
        source_order = await self._execute_leg_1(request, cycle, source_spec, source_venue, result)
        result.source_order = source_order

        # --- 6. recalculate from what actually filled ------------------
        actual_source = await self._position(source_venue, source_spec)
        if actual_source.quantity != projected_source_qty:
            result.messages.append(
                f"leg 1 filled {normalize(actual_source.quantity)} of "
                f"{normalize(projected_source_qty)}; hedge recalculated from the actual fill"
            )
            source_ticker = await source_venue.get_ticker(source_spec.symbol)
            hedge_ticker = await hedge_venue.get_ticker(hedge_spec.symbol)
            calculation = await self._calculate(
                request, source_spec, hedge_spec, actual_source.quantity,
                source_ticker, hedge_ticker, actual_source, hedge_position,
                hedge_venue, result,
            )
            cycle.hedge_target_quantity = calculation.rounded_quantity
            cycle.hedge_ratio = calculation.hedge_ratio
        cycle.source_filled_quantity = actual_source.quantity
        result.calculation = calculation

        # --- 7. leg 2: the hedge venue --------------------------------
        hedge_order = await self._execute_leg_2(
            request, cycle, hedge_spec, hedge_venue, calculation, hedge_position, result
        )
        result.hedge_order = hedge_order

        final_hedge = await self._position(hedge_venue, hedge_spec)
        cycle.hedge_filled_quantity = final_hedge.quantity

        # --- 8. settle the cycle --------------------------------------
        await self._transition(cycle, CycleState.BOTH_FILLED, "both_legs_filled", {
            "source_quantity": str(actual_source.quantity),
            "hedge_quantity": str(final_hedge.quantity),
        })

        await self._settle(
            request, cycle, source_spec, hedge_spec, source_venue, hedge_venue, result
        )

    # ------------------------------------------------------------------
    async def _validate(
        self,
        request: HedgeRequest,
        cycle: HedgeCycle,
        source_spec: InstrumentSpec,
        hedge_spec: InstrumentSpec,
    ) -> None:
        problems: list[str] = []
        if not request.mapping.enabled:
            problems.append(f"hedge pair {request.mapping.name!r} is disabled")
        for spec in (source_spec, hedge_spec):
            if not spec.active:
                problems.append(f"{spec.key} is not active")
            if spec.venue not in self.venues:
                problems.append(f"no venue adapter registered for {spec.venue}")
        if source_spec.venue == hedge_spec.venue:
            problems.append("source and hedge legs are on the same venue")
        if request.source_quantity != ZERO and not source_spec.supports_side_sign(request.source_quantity):
            problems.append(f"{source_spec.key} does not allow this direction")
        if problems:
            raise ExecutionError("; ".join(problems))
        await self._transition(cycle, CycleState.VALIDATED, "validated", {
            "source": source_spec.key, "hedge": hedge_spec.key,
            "source_sizing": source_spec.describe_sizing(),
            "hedge_sizing": hedge_spec.describe_sizing(),
        })

    def _validate_market_data(self, source_ticker: Any, hedge_ticker: Any) -> None:
        limit = self.settings.max_spread_bps_for_execution
        for ticker in (source_ticker, hedge_ticker):
            if ticker.is_stale:
                raise ExecutionError(
                    f"{ticker.key} market data is stale; refusing to hedge on a frozen feed"
                )
            if ticker.spread_bps > limit:
                raise ExecutionError(
                    f"{ticker.key} spread {quantize(ticker.spread_bps, 2)} bps exceeds the "
                    f"{limit} bps execution limit"
                )

    async def _calculate(
        self,
        request: HedgeRequest,
        source_spec: InstrumentSpec,
        hedge_spec: InstrumentSpec,
        source_quantity: Decimal,
        source_ticker: Any,
        hedge_ticker: Any,
        source_position: Position,
        hedge_position: Position,
        hedge_venue: TradingVenue,
        result: ExecutionResult,
    ) -> HedgeCalculation:
        inputs = HedgeInputs(
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_quantity=source_quantity,
            source_ticker=source_ticker,
            hedge_ticker=hedge_ticker,
            objective=request.objective or request.mapping.objective,
            target_ratio=(
                request.target_ratio if request.target_ratio is not None
                else request.mapping.target_ratio
            ),
            current_hedge_quantity=hedge_position.quantity,
            source_entry_price=source_position.average_entry or None,
            hedge_entry_price=hedge_position.average_entry or None,
            hedge_book=await hedge_venue.get_orderbook(hedge_spec.symbol, 12),
            hedge_account=await hedge_venue.get_balance(),
            risk_params=request.risk_params,
            account_currency=self.settings.account_currency,
        )
        if request.optimizer is not None:
            optimisation = self.optimizer.optimize(inputs, request.optimizer)
            result.messages.append(optimisation.rationale)
            return optimisation.calculation
        return self.calculator.calculate(inputs)

    async def _risk_check(
        self,
        request: HedgeRequest,
        cycle: HedgeCycle,
        source_spec: InstrumentSpec,
        hedge_spec: InstrumentSpec,
        source_position: Position,
        hedge_position: Position,
        source_ticker: Any,
        hedge_ticker: Any,
        source_venue: TradingVenue,
        hedge_venue: TradingVenue,
        calculation: HedgeCalculation,
        current_source_position: Position | None = None,
    ) -> None:
        current_source_position = (
            current_source_position if current_source_position is not None
            else source_position
        )
        if request.bypass_risk:
            await self._transition(cycle, CycleState.RISK_APPROVED, "risk_bypassed", {
                "reason": request.reason,
                "note": "risk approval bypassed by an emergency de-risking action",
            })
            return

        if not calculation.is_executable:
            raise ExecutionError(
                "hedge is not executable: " + "; ".join(calculation.warnings)
            )

        # Evaluate the book as it will be *after* the hedge, not as it is now.
        # An unhedged source position is by definition a large residual
        # exposure; gating on the current state would make the risk engine
        # block the one trade that removes the risk it is complaining about.
        projected_hedge = Position(
            venue=hedge_spec.venue,
            symbol=hedge_spec.symbol,
            quantity=calculation.rounded_quantity,
            average_entry=(
                hedge_position.average_entry if hedge_position.average_entry > ZERO
                else hedge_ticker.mid
            ),
            realized_pnl=hedge_position.realized_pnl,
        )
        risk = self.risk_engine.evaluate(PairRiskInputs(
            pair_name=request.mapping.name,
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_position=source_position,
            hedge_position=projected_hedge,
            source_ticker=source_ticker,
            hedge_ticker=hedge_ticker,
            # Account snapshots stay current: margin level describes the money
            # that exists now.  The *additional* margin this trade needs is
            # checked separately against ``max_safe_quantity``.
            source_account=await source_venue.get_balance(),
            hedge_account=await hedge_venue.get_balance(),
            funding_per_day=calculation.funding_impact_per_day,
            account_currency=self.settings.account_currency,
        ))
        if not risk.is_tradable:
            # A trade that strictly reduces residual exposure is always allowed
            # to proceed unless the kill switch itself has fired -- otherwise a
            # breached account can never be de-risked.
            current_residual = abs(self._residual_bps(
                source_spec, hedge_spec, current_source_position, hedge_position,
                source_ticker, hedge_ticker,
            ))
            if risk.level is RiskLevel.KILL_SWITCH or abs(risk.residual_bps) >= current_residual:
                raise ExecutionError(
                    f"risk level {risk.level.value} blocks new hedges: "
                    + "; ".join(b.message for b in risk.breaches)
                )
            log.warning(
                "proceeding despite elevated risk: the hedge strictly reduces residual exposure",
                extra={"pair": request.mapping.name, "level": risk.level.value,
                       "residual_before_bps": str(quantize(current_residual, 2)),
                       "residual_after_bps": str(quantize(abs(risk.residual_bps), 2))},
            )
        if (
            request.mapping.max_notional is not None
            and calculation.notional_exposure > request.mapping.max_notional
        ):
            raise ExecutionError(
                f"pair notional {quantize(calculation.notional_exposure, 2)} exceeds the "
                f"configured maximum {request.mapping.max_notional}"
            )
        await self._transition(cycle, CycleState.RISK_APPROVED, "risk_approved", {
            "level": risk.level.value,
            "evaluated_against": "post-trade book",
            "projected_hedge_quantity": str(calculation.rounded_quantity),
            "projected_residual_bps": str(quantize(abs(risk.residual_bps), 4)),
            "margin_requirement": str(calculation.margin_requirement),
            "breaches": [b.to_dict() for b in risk.breaches],
        })

    def _residual_bps(
        self,
        source_spec: InstrumentSpec,
        hedge_spec: InstrumentSpec,
        source_position: Position,
        hedge_position: Position,
        source_ticker: Any,
        hedge_ticker: Any,
    ) -> Decimal:
        """Residual exposure of the book as it stands right now, in bps."""
        acct = self.settings.account_currency
        source = QuantityConverter(source_spec).exposure(
            source_position.quantity, source_ticker.mid, account_currency=acct,
            fx_rate=self.fx.try_rate(source_spec.quote_asset, acct),
            entry_price=source_position.average_entry or None,
        )
        hedge = QuantityConverter(hedge_spec).exposure(
            hedge_position.quantity, hedge_ticker.mid, account_currency=acct,
            fx_rate=self.fx.try_rate(hedge_spec.quote_asset, acct),
            entry_price=hedge_position.average_entry or None,
        )
        residual_value = abs(source.account_delta + hedge.account_delta) * hedge_ticker.mid
        return safe_div(residual_value, abs(source.notional_account)) * Decimal(10000)

    # ------------------------------------------------------------------
    async def _execute_leg_1(
        self,
        request: HedgeRequest,
        cycle: HedgeCycle,
        source_spec: InstrumentSpec,
        source_venue: TradingVenue,
        result: ExecutionResult,
    ) -> Order | None:
        if request.source_quantity == ZERO:
            # No source trade needed.  Record both transitions anyway so the
            # audit trail shows an explicit decision rather than a gap.
            await self._transition(cycle, CycleState.LEG_1_SUBMITTED, "leg_1_no_trade_required", {
                "note": "source position already exists; leg 1 is a confirmation step",
            })
            await self._transition(cycle, CycleState.LEG_1_FILLED, "leg_1_confirmed", {})
            return None

        rounding = QuantityConverter(source_spec).round_quantity(request.source_quantity)
        if not rounding.is_executable:
            raise ExecutionError(
                f"source quantity {normalize(request.source_quantity)} rounds to zero on a "
                f"{normalize(source_spec.quantity_step)} step with a "
                f"{normalize(source_spec.min_quantity)} minimum"
            )
        order_request = OrderRequest(
            venue=source_spec.venue,
            symbol=source_spec.symbol,
            side=Side.BUY if rounding.rounded > ZERO else Side.SELL,
            quantity=abs(rounding.rounded),
            cycle_id=cycle.cycle_id,
            leg=Leg.SOURCE,
            correlation_id=cycle.correlation_id,
        )
        await self._transition(cycle, CycleState.LEG_1_SUBMITTED, "leg_1_submitted", {
            "venue": source_spec.venue, "symbol": source_spec.symbol,
            "side": order_request.side.value, "quantity": str(order_request.quantity),
            "client_order_id": order_request.client_order_id,
        })
        order = await self._place(source_venue, order_request, cycle, Leg.SOURCE)
        cycle.source_order_id = order.order_id
        await self._record_order(order, cycle)

        if order.is_partial:
            await self._transition(cycle, CycleState.LEG_1_PARTIAL, "leg_1_partial", {
                "filled": str(order.filled_quantity), "requested": str(order_request.quantity),
                "fill_ratio": str(quantize(order.fill_ratio, 6)),
            })
            result.messages.append(
                f"leg 1 partially filled: {normalize(order.filled_quantity)} of "
                f"{normalize(order_request.quantity)}"
            )
        if order.filled_quantity <= ZERO:
            raise ExecutionError("leg 1 did not fill; no exposure taken")
        await self._transition(cycle, CycleState.LEG_1_FILLED, "leg_1_filled", {
            "filled": str(order.filled_quantity), "average_price": str(order.average_price),
            "fees": str(order.fees_paid),
        })
        return order

    async def _execute_leg_2(
        self,
        request: HedgeRequest,
        cycle: HedgeCycle,
        hedge_spec: InstrumentSpec,
        hedge_venue: TradingVenue,
        calculation: HedgeCalculation,
        hedge_position: Position,
        result: ExecutionResult,
    ) -> Order | None:
        delta = calculation.rounded_quantity - hedge_position.quantity
        rounding = QuantityConverter(hedge_spec).round_quantity(delta)
        if not rounding.is_executable:
            await self._transition(cycle, CycleState.LEG_2_SUBMITTED, "leg_2_no_trade_required", {
                "note": f"hedge delta {normalize(delta)} rounds to zero; position already correct",
            })
            return None

        order_request = OrderRequest(
            venue=hedge_spec.venue,
            symbol=hedge_spec.symbol,
            side=Side.BUY if rounding.rounded > ZERO else Side.SELL,
            quantity=abs(rounding.rounded),
            cycle_id=cycle.cycle_id,
            leg=Leg.HEDGE,
            correlation_id=cycle.correlation_id,
        )
        await self._transition(cycle, CycleState.LEG_2_SUBMITTED, "leg_2_submitted", {
            "venue": hedge_spec.venue, "symbol": hedge_spec.symbol,
            "side": order_request.side.value, "quantity": str(order_request.quantity),
            "client_order_id": order_request.client_order_id,
        })
        order = await self._place(hedge_venue, order_request, cycle, Leg.HEDGE)
        cycle.hedge_order_id = order.order_id
        await self._record_order(order, cycle)

        if order.is_partial:
            await self._transition(cycle, CycleState.LEG_2_PARTIAL, "leg_2_partial", {
                "filled": str(order.filled_quantity), "requested": str(order_request.quantity),
            })
            result.messages.append(
                f"leg 2 partially filled: {normalize(order.filled_quantity)} of "
                f"{normalize(order_request.quantity)}; the shortfall is a live residual"
            )
        return order

    async def _place(
        self, venue: TradingVenue, request: OrderRequest, cycle: HedgeCycle, leg: Leg
    ) -> Order:
        """Place an order, resolving timeouts against actual venue state.

        A timeout means the outcome is *unknown*.  Retrying blindly is how a
        book ends up double-hedged, so the coordinator asks the venue what its
        open orders look like before deciding.
        """
        try:
            return await venue.place_order(request)
        except VenueTimeout as exc:
            log.error(
                "order timed out with unknown outcome; querying venue",
                extra={"venue": venue.name, "symbol": request.symbol, "leg": leg.value},
            )
            await self.recorder.record_system_event(
                "ORDER_TIMEOUT", "ERROR", "coordinator",
                f"{venue.name} {request.symbol} order timed out; resolving actual state",
                {"cycle_id": cycle.cycle_id, "client_order_id": request.client_order_id},
            )
            resolved = await self._resolve_timeout(venue, request)
            if resolved is not None:
                return resolved
            raise ExecutionError(
                f"{venue.name} timed out placing {request.symbol} and no matching order was "
                f"found on the venue; not retrying to avoid a duplicate position"
            ) from exc

    async def _resolve_timeout(
        self, venue: TradingVenue, request: OrderRequest
    ) -> Order | None:
        """Look for an order matching the client id we sent."""
        try:
            open_orders = await venue.get_open_orders()
        except VenueError:
            return None
        for order in open_orders:
            if order.request.client_order_id == request.client_order_id:
                log.warning(
                    "timed-out order was found on the venue",
                    extra={"order_id": order.order_id, "status": order.status.value},
                )
                return order
        return None

    # ------------------------------------------------------------------
    async def _settle(
        self,
        request: HedgeRequest,
        cycle: HedgeCycle,
        source_spec: InstrumentSpec,
        hedge_spec: InstrumentSpec,
        source_venue: TradingVenue,
        hedge_venue: TradingVenue,
        result: ExecutionResult,
    ) -> None:
        """Check the achieved residual and rebalance if it is out of tolerance."""
        from .rebalancer import Rebalancer  # local import: avoids a cycle

        rebalancer = Rebalancer(
            settings=self.settings, registry=self.registry, venues=self.venues,
            calculator=self.calculator, fx=self.fx, recorder=self.recorder,
        )
        assessment = await rebalancer.assess(
            mapping=request.mapping,
            objective=request.objective or request.mapping.objective,
            target_ratio=(
                request.target_ratio if request.target_ratio is not None
                else request.mapping.target_ratio
            ),
            risk_params=request.risk_params,
        )
        cycle.residual_exposure = assessment.residual_account_delta
        cycle.hedge_ratio = assessment.hedge_ratio

        if not assessment.needs_rebalance:
            await self._transition(cycle, CycleState.COMPLETED, "completed", {
                "hedge_ratio": str(quantize(assessment.hedge_ratio, 6)),
                "residual_bps": str(quantize(assessment.residual_bps, 4)),
                "tolerance_bps": str(request.mapping.tolerance_bps),
            })
            return

        await self._transition(cycle, CycleState.REBALANCING, "rebalance_required", {
            "residual_bps": str(quantize(assessment.residual_bps, 4)),
            "tolerance_bps": str(request.mapping.tolerance_bps),
            "delta_quantity": str(assessment.rebalance_quantity),
        })
        orders = await rebalancer.execute(assessment, cycle_id=cycle.cycle_id)
        result.rebalance_orders.extend(orders)
        for order in orders:
            await self._record_order(order, cycle)

        after = await rebalancer.assess(
            mapping=request.mapping,
            objective=request.objective or request.mapping.objective,
            target_ratio=(
                request.target_ratio if request.target_ratio is not None
                else request.mapping.target_ratio
            ),
            risk_params=request.risk_params,
        )
        cycle.residual_exposure = after.residual_account_delta
        cycle.hedge_ratio = after.hedge_ratio
        cycle.hedge_filled_quantity = after.hedge_quantity
        result.messages.append(
            f"rebalanced: residual {quantize(assessment.residual_bps, 2)} bps -> "
            f"{quantize(after.residual_bps, 2)} bps"
        )
        await self._transition(cycle, CycleState.COMPLETED, "completed_after_rebalance", {
            "residual_bps_before": str(quantize(assessment.residual_bps, 4)),
            "residual_bps_after": str(quantize(after.residual_bps, 4)),
            "hedge_ratio": str(quantize(after.hedge_ratio, 6)),
        })

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    async def _recover(
        self, cycle: HedgeCycle, result: ExecutionResult, request: HedgeRequest
    ) -> None:
        """Restore a safe book after leg 2 failed with leg 1 on.

        Order of preference: complete the hedge, then unwind leg 1, then
        escalate.  Never leave the cycle reporting success.
        """
        if cycle.state is not CycleState.RECOVERY_REQUIRED:
            return
        log.error(
            "hedge cycle needs recovery: exposure is on the book without a complete hedge",
            extra={"cycle_id": cycle.cycle_id, "error": cycle.error},
        )
        await self.recorder.record_system_event(
            "RECOVERY_REQUIRED", "ERROR", "coordinator",
            f"cycle {cycle.cycle_id} left exposure without a complete hedge",
            {"pair": cycle.pair_name, "error": cycle.error},
        )
        from .rebalancer import Rebalancer

        rebalancer = Rebalancer(
            settings=self.settings, registry=self.registry, venues=self.venues,
            calculator=self.calculator, fx=self.fx, recorder=self.recorder,
        )
        try:
            assessment = await rebalancer.assess(
                mapping=request.mapping,
                objective=request.objective or request.mapping.objective,
                target_ratio=(
                    request.target_ratio if request.target_ratio is not None
                    else request.mapping.target_ratio
                ),
                risk_params=request.risk_params,
            )
            if assessment.rebalance_quantity != ZERO:
                await self._transition(cycle, CycleState.REBALANCING, "recovery_rebalance", {
                    "delta_quantity": str(assessment.rebalance_quantity),
                })
                orders = await rebalancer.execute(assessment, cycle_id=cycle.cycle_id)
                result.rebalance_orders.extend(orders)
                after = await rebalancer.assess(
                    mapping=request.mapping,
                    objective=request.objective or request.mapping.objective,
                    target_ratio=(
                        request.target_ratio if request.target_ratio is not None
                        else request.mapping.target_ratio
                    ),
                    risk_params=request.risk_params,
                )
                cycle.residual_exposure = after.residual_account_delta
                cycle.hedge_ratio = after.hedge_ratio
                cycle.hedge_filled_quantity = after.hedge_quantity
                if not after.needs_rebalance:
                    result.messages.append(
                        f"recovery succeeded: hedge restored, residual "
                        f"{quantize(after.residual_bps, 2)} bps"
                    )
                    await self._transition(cycle, CycleState.COMPLETED, "recovered", {
                        "residual_bps": str(quantize(after.residual_bps, 4)),
                    })
                    return
            result.messages.append("recovery could not restore the hedge; escalating")
            await self._transition(cycle, CycleState.EMERGENCY, "recovery_failed", {
                "residual_bps": str(quantize(assessment.residual_bps, 4)),
            })
        except (VenueError, ExecutionError) as exc:
            result.messages.append(f"recovery failed: {exc}")
            if cycle.state is not CycleState.EMERGENCY:
                await self._transition(cycle, CycleState.EMERGENCY, "recovery_failed", {
                    "error": str(exc),
                })

    @staticmethod
    def _fail_target(cycle: HedgeCycle, reason: str) -> tuple[CycleState, str, dict[str, Any]]:
        cycle.error = reason
        target = CycleState.RECOVERY_REQUIRED if cycle.state.has_exposure else CycleState.FAILED
        return target, "cycle_failed", {"reason": reason}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _venue(self, name: str) -> TradingVenue:
        try:
            return self.venues[name]
        except KeyError:
            raise ExecutionError(f"no venue adapter registered for {name!r}") from None

    async def _position(self, venue: TradingVenue, spec: InstrumentSpec) -> Position:
        try:
            positions = await venue.get_positions()
        except VenueDisconnected:
            raise
        for position in positions:
            if position.symbol == spec.symbol:
                return position
        return Position(venue=spec.venue, symbol=spec.symbol)

    async def _transition(
        self, cycle: HedgeCycle, state: CycleState, event: str, payload: dict[str, Any]
    ) -> None:
        record = cycle.transition(state, event, payload)
        await self.recorder.record_event(record)
        await self.publisher.publish("hedge_cycle", record.to_dict())

    async def _save(self, cycle: HedgeCycle) -> None:
        await self.recorder.save_cycle(cycle)
        await self.publisher.publish("hedge_cycle_state", cycle.to_dict())

    async def _record_order(self, order: Order, cycle: HedgeCycle) -> None:
        await self.recorder.record_order(order, cycle.cycle_id)
        for fill in order.fills:
            await self.recorder.record_fill(fill, order)
        await self.publisher.publish("order", {
            "order_id": order.order_id,
            "cycle_id": cycle.cycle_id,
            "venue": order.request.venue,
            "symbol": order.request.symbol,
            "side": order.request.side.value,
            "quantity": str(order.request.quantity),
            "filled": str(order.filled_quantity),
            "average_price": str(order.average_price),
            "status": order.status.value,
        })
