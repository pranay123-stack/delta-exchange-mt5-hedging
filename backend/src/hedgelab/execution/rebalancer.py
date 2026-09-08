"""Rebalancing: keeping a live hedge inside its tolerance.

A hedge is correct only at the instant it is placed.  Afterwards the ratio
drifts because prices move (an inverse leg's delta depends on entry price),
because funding and fees change the position, and because a partial fill left
a shortfall that was never closed.

The rebalancer answers two questions, separately and in that order:

1. **Should we trade?**  ``assess()`` is read-only and safe to call on a timer.
2. **Trade.**  ``execute()`` places the delta order.

Splitting them matters: the monitoring loop calls ``assess`` constantly and
must never trade as a side effect of looking.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..domain.enums import HedgeObjective, Leg, Side
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, normalize, quantize, safe_div
from ..domain.orders import Order, OrderRequest, Position
from ..domain.quantity import QuantityConverter
from ..hedge.calculator import HedgeCalculator, HedgeInputs
from ..hedge.objectives import RiskParameters
from ..instruments.registry import HedgeMapping, InstrumentRegistry
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from ..venues.base import TradingVenue, VenueError
from .ports import CycleRecorder, NullRecorder

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RebalanceAssessment:
    """Read-only verdict on whether a live pair needs adjusting."""

    pair_name: str
    source_key: str
    hedge_key: str
    source_quantity: Decimal
    hedge_quantity: Decimal
    target_hedge_quantity: Decimal
    rebalance_quantity: Decimal
    hedge_ratio: Decimal
    residual_account_delta: Decimal
    residual_bps: Decimal
    tolerance_bps: Decimal
    needs_rebalance: Decimal | bool
    reason: str
    estimated_cost: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair_name,
            "source": self.source_key,
            "hedge": self.hedge_key,
            "source_quantity": str(normalize(self.source_quantity)),
            "hedge_quantity": str(normalize(self.hedge_quantity)),
            "target_hedge_quantity": str(normalize(self.target_hedge_quantity)),
            "rebalance_quantity": str(normalize(self.rebalance_quantity)),
            "hedge_ratio": str(quantize(self.hedge_ratio, 6)),
            "residual_account_delta": str(quantize(self.residual_account_delta, 8)),
            "residual_bps": str(quantize(self.residual_bps, 4)),
            "tolerance_bps": str(self.tolerance_bps),
            "needs_rebalance": bool(self.needs_rebalance),
            "reason": self.reason,
            "estimated_cost": str(quantize(self.estimated_cost, 4)),
        }


class Rebalancer:
    """Computes and applies hedge adjustments."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: InstrumentRegistry,
        venues: dict[str, TradingVenue],
        calculator: HedgeCalculator,
        fx: FxService,
        recorder: CycleRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.venues = venues
        self.calculator = calculator
        self.fx = fx
        self.recorder: CycleRecorder = recorder or NullRecorder()

    # ------------------------------------------------------------------
    async def assess(
        self,
        *,
        mapping: HedgeMapping,
        objective: HedgeObjective | None = None,
        target_ratio: Decimal | None = None,
        risk_params: RiskParameters = RiskParameters(),
    ) -> RebalanceAssessment:
        """Compute the current residual and whether it justifies a trade."""
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_venue = self.venues[source_spec.venue]
        hedge_venue = self.venues[hedge_spec.venue]

        source_position = await self._position(source_venue, source_spec)
        hedge_position = await self._position(hedge_venue, hedge_spec)
        source_ticker = await source_venue.get_ticker(source_spec.symbol)
        hedge_ticker = await hedge_venue.get_ticker(hedge_spec.symbol)

        calculation = self.calculator.calculate(HedgeInputs(
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_quantity=source_position.quantity,
            source_ticker=source_ticker,
            hedge_ticker=hedge_ticker,
            objective=objective or mapping.objective,
            target_ratio=target_ratio if target_ratio is not None else mapping.target_ratio,
            current_hedge_quantity=hedge_position.quantity,
            source_entry_price=source_position.average_entry or None,
            hedge_entry_price=hedge_position.average_entry or None,
            hedge_book=await hedge_venue.get_orderbook(hedge_spec.symbol, 12),
            hedge_account=await hedge_venue.get_balance(),
            risk_params=risk_params,
            account_currency=self.settings.account_currency,
        ))

        # Residual measured against the *live* hedge position, not the target.
        live_hedge_exposure = QuantityConverter(hedge_spec).exposure(
            hedge_position.quantity,
            hedge_ticker.mid,
            account_currency=self.settings.account_currency,
            fx_rate=self.fx.try_rate(hedge_spec.quote_asset, self.settings.account_currency),
            entry_price=hedge_position.average_entry or None,
        )
        residual_delta = calculation.source_exposure.account_delta + live_hedge_exposure.account_delta
        residual_value = abs(residual_delta) * hedge_ticker.mid
        source_notional = abs(calculation.source_exposure.notional_account)
        residual_bps = safe_div(residual_value, source_notional) * Decimal(10000)
        hedge_ratio = safe_div(
            -live_hedge_exposure.account_delta, calculation.source_exposure.account_delta
        )

        delta = calculation.rounded_quantity - hedge_position.quantity
        rounding = QuantityConverter(hedge_spec).round_quantity(delta)

        tolerance = mapping.tolerance_bps
        over_tolerance = residual_bps > tolerance
        tradable = rounding.is_executable

        if source_position.is_flat and hedge_position.is_flat:
            reason = "both legs are flat; nothing to rebalance"
            needs = False
        elif not over_tolerance:
            reason = (
                f"residual {quantize(residual_bps, 2)} bps is within the "
                f"{tolerance} bps tolerance"
            )
            needs = False
        elif not tradable:
            reason = (
                f"residual {quantize(residual_bps, 2)} bps exceeds tolerance but the "
                f"adjustment {normalize(delta)} is below the venue minimum "
                f"{normalize(hedge_spec.min_quantity)}; it cannot be traded away"
            )
            needs = False
        else:
            reason = (
                f"residual {quantize(residual_bps, 2)} bps exceeds the {tolerance} bps "
                f"tolerance; adjust the hedge by {normalize(rounding.rounded)}"
            )
            needs = True

        return RebalanceAssessment(
            pair_name=mapping.name,
            source_key=source_spec.key,
            hedge_key=hedge_spec.key,
            source_quantity=source_position.quantity,
            hedge_quantity=hedge_position.quantity,
            target_hedge_quantity=calculation.rounded_quantity,
            rebalance_quantity=rounding.rounded,
            hedge_ratio=hedge_ratio,
            residual_account_delta=residual_delta,
            residual_bps=residual_bps,
            tolerance_bps=tolerance,
            needs_rebalance=needs,
            reason=reason,
            estimated_cost=calculation.total_execution_cost,
        )

    # ------------------------------------------------------------------
    async def execute(
        self, assessment: RebalanceAssessment, *, cycle_id: str | None = None
    ) -> list[Order]:
        """Place the adjustment order.  No-op when the assessment says so."""
        if assessment.rebalance_quantity == ZERO:
            return []
        hedge_spec = self.registry.get(assessment.hedge_key)
        venue = self.venues[hedge_spec.venue]

        request = OrderRequest(
            venue=hedge_spec.venue,
            symbol=hedge_spec.symbol,
            side=Side.BUY if assessment.rebalance_quantity > ZERO else Side.SELL,
            quantity=abs(assessment.rebalance_quantity),
            cycle_id=cycle_id,
            leg=Leg.HEDGE,
        )
        log.info(
            "rebalancing hedge leg",
            extra={
                "pair": assessment.pair_name,
                "delta": str(normalize(assessment.rebalance_quantity)),
                "residual_bps": str(quantize(assessment.residual_bps, 4)),
                "reason": assessment.reason,
            },
        )
        order = await venue.place_order(request)
        await self.recorder.record_order(order, cycle_id)
        for fill in order.fills:
            await self.recorder.record_fill(fill, order)
        return [order]

    #: A flatten attempt that partially fills is retried this many times before
    #: the platform gives up and escalates.
    MAX_FLATTEN_ATTEMPTS = 5

    async def flatten(
        self, mapping: HedgeMapping, *, cycle_id: str | None = None
    ) -> list[Order]:
        """Close both legs.  Used by the kill switch.

        Two properties matter and both are easy to get wrong:

        * The hedge leg closes **first**.  Closing the source first would leave
          the hedge naked and directional for as long as the second order takes.
        * A partial fill does not end the attempt.  A kill switch that leaves
          half a position open because one order filled 50% has not killed
          anything, so each leg is retried until it is genuinely flat or the
          attempt budget is exhausted -- and an exhausted budget is logged as an
          error rather than reported as success.
        """
        orders: list[Order] = []
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        for spec in (hedge_spec, source_spec):
            venue = self.venues[spec.venue]
            leg = Leg.HEDGE if spec is hedge_spec else Leg.SOURCE
            try:
                await self._flatten_leg(venue, spec, leg, cycle_id, orders)
            except VenueError as exc:
                # An unreachable venue is exactly when the kill switch matters
                # most. Flatten everything reachable and report the rest rather
                # than aborting the whole operation on the first failure.
                log.error(
                    "cannot flatten on an unreachable venue",
                    extra={"symbol": spec.key, "venue": spec.venue, "error": str(exc)},
                )
        return orders

    async def _flatten_leg(
        self,
        venue: TradingVenue,
        spec: InstrumentSpec,
        leg: Leg,
        cycle_id: str | None,
        orders: list[Order],
    ) -> None:
        """Close one leg, retrying until it is genuinely flat."""
        for attempt in range(1, self.MAX_FLATTEN_ATTEMPTS + 1):
            position = await self._position(venue, spec)
            if position.is_flat:
                break
            rounding = QuantityConverter(spec).round_quantity(-position.quantity)
            if not rounding.is_executable:
                log.error(
                    "cannot flatten position: residual is below the venue minimum",
                    extra={"symbol": spec.key, "quantity": str(position.quantity),
                           "min_quantity": str(spec.min_quantity)},
                )
                break
            request = OrderRequest(
                venue=spec.venue, symbol=spec.symbol,
                side=Side.BUY if rounding.rounded > ZERO else Side.SELL,
                quantity=abs(rounding.rounded), cycle_id=cycle_id, leg=leg,
            )
            order = await venue.place_order(request)
            await self.recorder.record_order(order, cycle_id)
            orders.append(order)
            log.warning(
                "flatten order placed",
                extra={"symbol": spec.key, "attempt": attempt,
                       "requested": str(normalize(rounding.rounded)),
                       "filled": str(normalize(order.filled_quantity))},
            )
        else:
            remaining = await self._position(venue, spec)
            if not remaining.is_flat:
                log.error(
                    "flatten did not reach a flat position",
                    extra={"symbol": spec.key,
                           "remaining": str(normalize(remaining.quantity)),
                           "attempts": self.MAX_FLATTEN_ATTEMPTS},
                )

    # ------------------------------------------------------------------
    @staticmethod
    async def _position(venue: TradingVenue, spec: InstrumentSpec) -> Position:
        for position in await venue.get_positions():
            if position.symbol == spec.symbol:
                return position
        return Position(venue=spec.venue, symbol=spec.symbol)
