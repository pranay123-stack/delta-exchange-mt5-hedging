"""Per-pair risk engine.

Turns market data plus account state into a graded risk assessment and a set of
actions.  Thresholds are configuration, and every breach produces a
:class:`RiskEvent` that is persisted -- a risk system whose decisions cannot be
reconstructed after the fact is not auditable.

Levels escalate NORMAL -> WARNING -> DANGER -> EMERGENCY -> KILL_SWITCH, and
the *worst* breaching metric sets the level for the whole assessment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.account import AccountSnapshot
from ..domain.enums import EmergencyAction, RiskLevel
from ..domain.instrument import InstrumentSpec
from ..domain.market import Ticker, utcnow
from ..domain.numeric import ZERO, quantize, safe_div
from ..domain.orders import Position
from ..domain.quantity import QuantityConverter
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from .margin import LiquidationEstimate, liquidation_price

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskThresholds:
    """Configurable limits.  All are inclusive bounds that trigger on breach."""

    warning_margin_level: Decimal = Decimal(200)
    danger_margin_level: Decimal = Decimal(150)
    emergency_margin_level: Decimal = Decimal(120)
    kill_switch_margin_level: Decimal = Decimal(100)

    #: Residual exposure as bps of source notional.
    warning_residual_bps: Decimal = Decimal(50)
    danger_residual_bps: Decimal = Decimal(150)
    emergency_residual_bps: Decimal = Decimal(400)

    #: Distance to liquidation, as a fraction of the mark.
    warning_liquidation_distance: Decimal = Decimal("0.10")
    danger_liquidation_distance: Decimal = Decimal("0.05")
    emergency_liquidation_distance: Decimal = Decimal("0.02")

    max_daily_loss: Decimal = Decimal(25000)
    max_strategy_loss: Decimal = Decimal(10000)
    #: Fraction of ``max_daily_loss`` at which to warn.
    loss_warning_fraction: Decimal = Decimal("0.6")
    loss_danger_fraction: Decimal = Decimal("0.85")

    max_spread_bps: Decimal = Decimal(50)
    stale_data_seconds: Decimal = Decimal(5)
    #: Basis (source vs hedge price) beyond which the hedge is unreliable.
    warning_basis_bps: Decimal = Decimal(30)
    danger_basis_bps: Decimal = Decimal(80)


@dataclass(frozen=True, slots=True)
class RiskBreach:
    """One metric outside its limit."""

    metric: str
    level: RiskLevel
    value: Decimal
    threshold: Decimal
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "level": self.level.value,
            "value": str(quantize(self.value, 8)),
            "threshold": str(quantize(self.threshold, 8)),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class PairRisk:
    """Complete risk picture for one hedge pair."""

    pair_name: str
    source_key: str
    hedge_key: str
    level: RiskLevel
    breaches: tuple[RiskBreach, ...]
    actions: tuple[EmergencyAction, ...]

    # exposures
    source_notional: Decimal
    hedge_notional: Decimal
    net_notional: Decimal
    residual_delta: Decimal
    residual_bps: Decimal
    hedge_ratio: Decimal

    # margin
    source_margin: Decimal
    hedge_margin: Decimal
    total_margin: Decimal
    source_margin_level: Decimal
    hedge_margin_level: Decimal

    # liquidation
    source_liquidation: LiquidationEstimate | None
    hedge_liquidation: LiquidationEstimate | None
    max_tolerable_move: Decimal | None

    # carry and basis
    funding_risk_per_day: Decimal
    basis_bps: Decimal
    liquidity_risk_bps: Decimal

    # P&L
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    daily_pnl: Decimal

    currency: str = "USD"
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def is_tradable(self) -> bool:
        """New hedges are blocked from DANGER upward."""
        return self.level.rank < RiskLevel.DANGER.rank

    def to_dict(self) -> dict[str, object]:
        return {
            "pair": self.pair_name,
            "source": self.source_key,
            "hedge": self.hedge_key,
            "level": self.level.value,
            "breaches": [b.to_dict() for b in self.breaches],
            "actions": [a.value for a in self.actions],
            "source_notional": str(quantize(self.source_notional, 2)),
            "hedge_notional": str(quantize(self.hedge_notional, 2)),
            "net_notional": str(quantize(self.net_notional, 2)),
            "residual_delta": str(quantize(self.residual_delta, 8)),
            "residual_bps": str(quantize(self.residual_bps, 4)),
            "hedge_ratio": str(quantize(self.hedge_ratio, 6)),
            "total_margin": str(quantize(self.total_margin, 2)),
            "source_margin_level": str(quantize(self.source_margin_level, 2)),
            "hedge_margin_level": str(quantize(self.hedge_margin_level, 2)),
            "source_liquidation_price": (
                str(self.source_liquidation.liquidation_price)
                if self.source_liquidation and self.source_liquidation.liquidation_price else None
            ),
            "hedge_liquidation_price": (
                str(self.hedge_liquidation.liquidation_price)
                if self.hedge_liquidation and self.hedge_liquidation.liquidation_price else None
            ),
            "max_tolerable_move": str(self.max_tolerable_move) if self.max_tolerable_move else None,
            "funding_risk_per_day": str(quantize(self.funding_risk_per_day, 4)),
            "basis_bps": str(quantize(self.basis_bps, 4)),
            "liquidity_risk_bps": str(quantize(self.liquidity_risk_bps, 4)),
            "unrealized_pnl": str(quantize(self.unrealized_pnl, 2)),
            "realized_pnl": str(quantize(self.realized_pnl, 2)),
            "daily_pnl": str(quantize(self.daily_pnl, 2)),
            "currency": self.currency,
            "is_tradable": self.is_tradable,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PairRiskInputs:
    pair_name: str
    source_spec: InstrumentSpec
    hedge_spec: InstrumentSpec
    source_position: Position
    hedge_position: Position
    source_ticker: Ticker
    hedge_ticker: Ticker
    source_account: AccountSnapshot
    hedge_account: AccountSnapshot
    funding_per_day: Decimal = ZERO
    daily_pnl: Decimal = ZERO
    account_currency: str = "USD"


class RiskEngine:
    """Evaluates pair risk and decides what to do about it."""

    def __init__(self, fx: FxService, thresholds: RiskThresholds | None = None) -> None:
        self.fx = fx
        self.thresholds = thresholds or RiskThresholds()

    def evaluate(self, inputs: PairRiskInputs) -> PairRisk:
        src, hdg = inputs.source_spec, inputs.hedge_spec
        src_conv, hdg_conv = QuantityConverter(src), QuantityConverter(hdg)
        acct = inputs.account_currency
        src_fx = self.fx.try_rate(src.quote_asset, acct)
        hdg_fx = self.fx.try_rate(hdg.quote_asset, acct)
        src_mid, hdg_mid = inputs.source_ticker.mid, inputs.hedge_ticker.mid

        src_exp = src_conv.exposure(
            inputs.source_position.quantity, src_mid, account_currency=acct,
            fx_rate=src_fx, entry_price=inputs.source_position.average_entry or None,
        )
        hdg_exp = hdg_conv.exposure(
            inputs.hedge_position.quantity, hdg_mid, account_currency=acct,
            fx_rate=hdg_fx, entry_price=inputs.hedge_position.average_entry or None,
        )
        residual_delta = src_exp.account_delta + hdg_exp.account_delta
        residual_value = abs(residual_delta) * hdg_mid
        source_notional = abs(src_exp.notional_account)
        residual_bps = safe_div(residual_value, source_notional) * Decimal(10000)
        hedge_ratio = safe_div(-hdg_exp.account_delta, src_exp.account_delta)

        src_margin = self._margin(src, inputs.source_position, src_mid, src_fx)
        hdg_margin = self._margin(hdg, inputs.hedge_position, hdg_mid, hdg_fx)

        src_liq = self._liquidation(src, inputs.source_position, inputs.source_account, src_mid, src_fx)
        hdg_liq = self._liquidation(hdg, inputs.hedge_position, inputs.hedge_account, hdg_mid, hdg_fx)
        tolerable = self._min_tolerable(src_liq, hdg_liq)

        basis_bps = self._basis_bps(src_mid, hdg_mid, src_fx, hdg_fx)
        liquidity_bps = max(inputs.source_ticker.spread_bps, inputs.hedge_ticker.spread_bps)

        unrealized = (
            self._unrealized(src, inputs.source_position, src_mid) * src_fx
            + self._unrealized(hdg, inputs.hedge_position, hdg_mid) * hdg_fx
        )
        realized = (
            inputs.source_position.realized_pnl * src_fx
            + inputs.hedge_position.realized_pnl * hdg_fx
        )

        breaches: list[RiskBreach] = []
        self._check_margin_levels(breaches, inputs.source_account, inputs.hedge_account)
        self._check_residual(breaches, residual_bps, inputs.source_position)
        self._check_liquidation(breaches, src_liq, hdg_liq)
        self._check_losses(breaches, inputs.daily_pnl, unrealized + realized)
        self._check_market_quality(breaches, inputs.source_ticker, inputs.hedge_ticker, basis_bps)

        level = max((b.level for b in breaches), key=lambda x: x.rank, default=RiskLevel.NORMAL)
        actions = self._actions_for(level, breaches)

        risk = PairRisk(
            pair_name=inputs.pair_name,
            source_key=src.key,
            hedge_key=hdg.key,
            level=level,
            breaches=tuple(breaches),
            actions=actions,
            source_notional=source_notional,
            hedge_notional=abs(hdg_exp.notional_account),
            net_notional=src_exp.notional_account + hdg_exp.notional_account,
            residual_delta=residual_delta,
            residual_bps=residual_bps,
            hedge_ratio=hedge_ratio,
            source_margin=src_margin,
            hedge_margin=hdg_margin,
            total_margin=src_margin + hdg_margin,
            source_margin_level=inputs.source_account.margin_level,
            hedge_margin_level=inputs.hedge_account.margin_level,
            source_liquidation=src_liq,
            hedge_liquidation=hdg_liq,
            max_tolerable_move=tolerable,
            funding_risk_per_day=inputs.funding_per_day,
            basis_bps=basis_bps,
            liquidity_risk_bps=liquidity_bps,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            daily_pnl=inputs.daily_pnl,
            currency=acct,
        )
        if level is not RiskLevel.NORMAL:
            log.warning(
                "pair risk breach",
                extra={"pair": inputs.pair_name, "level": level.value,
                       "breaches": [b.metric for b in breaches],
                       "actions": [a.value for a in actions]},
            )
        return risk

    # ------------------------------------------------------------------
    # components
    # ------------------------------------------------------------------
    def _margin(
        self, spec: InstrumentSpec, position: Position, mark: Decimal, fx_rate: Decimal
    ) -> Decimal:
        if position.is_flat:
            return ZERO
        notional = abs(QuantityConverter(spec).notional_quote(position.quantity, mark))
        return notional * fx_rate * spec.effective_initial_margin_rate

    def _unrealized(self, spec: InstrumentSpec, position: Position, mark: Decimal) -> Decimal:
        if position.is_flat:
            return ZERO
        if spec.is_inverse:
            return position.unrealized_pnl_inverse(mark, spec.units_per_quantity)
        return position.unrealized_pnl_linear(mark, spec.units_per_quantity)

    def _liquidation(
        self,
        spec: InstrumentSpec,
        position: Position,
        account: AccountSnapshot,
        mark: Decimal,
        fx_rate: Decimal,
    ) -> LiquidationEstimate | None:
        if position.is_flat:
            return None
        # Equity backing the position, restated in the instrument's own
        # settlement currency -- inverse contracts post margin in base units.
        equity_quote = safe_div(account.equity, fx_rate, account.equity)
        if spec.is_inverse:
            equity_backing = safe_div(equity_quote, mark)
        else:
            equity_backing = safe_div(equity_quote, spec.units_per_quantity) * spec.units_per_quantity
        return liquidation_price(
            spec, position.quantity, position.average_entry, equity_backing, mark
        )

    @staticmethod
    def _min_tolerable(
        a: LiquidationEstimate | None, b: LiquidationEstimate | None
    ) -> Decimal | None:
        values = [
            est.max_tolerable_move
            for est in (a, b)
            if est is not None and est.max_tolerable_move is not None
        ]
        return min(values) if values else None

    def _basis_bps(
        self, src_mid: Decimal, hdg_mid: Decimal, src_fx: Decimal, hdg_fx: Decimal
    ) -> Decimal:
        """Price difference between the legs, in a common currency.

        Basis is the irreducible risk of a cross-venue hedge: the two legs can
        move apart even when the underlying does not move at all.
        """
        src_common = src_mid * src_fx
        hdg_common = hdg_mid * hdg_fx
        if hdg_common == ZERO:
            return ZERO
        return safe_div(src_common - hdg_common, hdg_common) * Decimal(10000)

    # ------------------------------------------------------------------
    # threshold checks
    # ------------------------------------------------------------------
    def _check_margin_levels(
        self, breaches: list[RiskBreach], source: AccountSnapshot, hedge: AccountSnapshot
    ) -> None:
        t = self.thresholds
        for account in (source, hedge):
            if account.is_flat:
                continue
            level_value = account.margin_level
            for threshold, risk_level in (
                (t.kill_switch_margin_level, RiskLevel.KILL_SWITCH),
                (t.emergency_margin_level, RiskLevel.EMERGENCY),
                (t.danger_margin_level, RiskLevel.DANGER),
                (t.warning_margin_level, RiskLevel.WARNING),
            ):
                if level_value <= threshold:
                    breaches.append(RiskBreach(
                        metric=f"margin_level:{account.venue}",
                        level=risk_level,
                        value=level_value,
                        threshold=threshold,
                        message=(
                            f"{account.venue} margin level {quantize(level_value, 2)}% is at or "
                            f"below the {risk_level.value} threshold of {threshold}%"
                        ),
                    ))
                    break

    def _check_residual(
        self, breaches: list[RiskBreach], residual_bps: Decimal, source_position: Position
    ) -> None:
        if source_position.is_flat:
            return
        t = self.thresholds
        magnitude = abs(residual_bps)
        for threshold, level in (
            (t.emergency_residual_bps, RiskLevel.EMERGENCY),
            (t.danger_residual_bps, RiskLevel.DANGER),
            (t.warning_residual_bps, RiskLevel.WARNING),
        ):
            if magnitude >= threshold:
                breaches.append(RiskBreach(
                    metric="residual_exposure_bps",
                    level=level,
                    value=magnitude,
                    threshold=threshold,
                    message=(
                        f"residual exposure {quantize(magnitude, 2)} bps of source notional "
                        f"exceeds the {level.value} threshold of {threshold} bps"
                    ),
                ))
                break

    def _check_liquidation(
        self,
        breaches: list[RiskBreach],
        source: LiquidationEstimate | None,
        hedge: LiquidationEstimate | None,
    ) -> None:
        t = self.thresholds
        for label, est in (("source", source), ("hedge", hedge)):
            if est is None or est.max_tolerable_move is None:
                continue
            if est.is_liquidatable:
                breaches.append(RiskBreach(
                    metric=f"liquidation:{label}",
                    level=RiskLevel.KILL_SWITCH,
                    value=est.mark_price,
                    threshold=est.liquidation_price or ZERO,
                    message=f"{label} leg has breached its liquidation price",
                ))
                continue
            for threshold, level in (
                (t.emergency_liquidation_distance, RiskLevel.EMERGENCY),
                (t.danger_liquidation_distance, RiskLevel.DANGER),
                (t.warning_liquidation_distance, RiskLevel.WARNING),
            ):
                if est.max_tolerable_move <= threshold:
                    breaches.append(RiskBreach(
                        metric=f"liquidation_distance:{label}",
                        level=level,
                        value=est.max_tolerable_move,
                        threshold=threshold,
                        message=(
                            f"{label} leg is {quantize(est.max_tolerable_move * 100, 3)}% from "
                            f"liquidation, inside the {level.value} threshold of "
                            f"{quantize(threshold * 100, 2)}%"
                        ),
                    ))
                    break

    def _check_losses(
        self, breaches: list[RiskBreach], daily_pnl: Decimal, total_pnl: Decimal
    ) -> None:
        t = self.thresholds
        loss = -daily_pnl
        if loss <= ZERO:
            return
        for fraction, level in (
            (Decimal(1), RiskLevel.KILL_SWITCH),
            (t.loss_danger_fraction, RiskLevel.DANGER),
            (t.loss_warning_fraction, RiskLevel.WARNING),
        ):
            limit = t.max_daily_loss * fraction
            if loss >= limit:
                breaches.append(RiskBreach(
                    metric="daily_loss",
                    level=level,
                    value=loss,
                    threshold=limit,
                    message=(
                        f"daily loss {quantize(loss, 2)} has reached {quantize(fraction * 100, 0)}% "
                        f"of the {quantize(t.max_daily_loss, 2)} limit"
                    ),
                ))
                break

    def _check_market_quality(
        self,
        breaches: list[RiskBreach],
        source_ticker: Ticker,
        hedge_ticker: Ticker,
        basis_bps: Decimal,
    ) -> None:
        t = self.thresholds
        for ticker in (source_ticker, hedge_ticker):
            if ticker.is_stale:
                breaches.append(RiskBreach(
                    metric=f"stale_data:{ticker.venue}",
                    level=RiskLevel.DANGER,
                    value=Decimal(1),
                    threshold=ZERO,
                    message=f"{ticker.key} market data is stale; hedging on a frozen feed is unsafe",
                ))
            if ticker.spread_bps > t.max_spread_bps:
                breaches.append(RiskBreach(
                    metric=f"spread:{ticker.venue}",
                    level=RiskLevel.WARNING,
                    value=ticker.spread_bps,
                    threshold=t.max_spread_bps,
                    message=(
                        f"{ticker.key} spread {quantize(ticker.spread_bps, 2)} bps exceeds the "
                        f"{t.max_spread_bps} bps execution limit"
                    ),
                ))
        magnitude = abs(basis_bps)
        for threshold, level in (
            (t.danger_basis_bps, RiskLevel.DANGER),
            (t.warning_basis_bps, RiskLevel.WARNING),
        ):
            if magnitude >= threshold:
                breaches.append(RiskBreach(
                    metric="basis_bps",
                    level=level,
                    value=magnitude,
                    threshold=threshold,
                    message=(
                        f"cross-venue basis {quantize(magnitude, 2)} bps exceeds the "
                        f"{level.value} threshold of {threshold} bps"
                    ),
                ))
                break

    # ------------------------------------------------------------------
    @staticmethod
    def _actions_for(
        level: RiskLevel, breaches: tuple[RiskBreach, ...] | list[RiskBreach]
    ) -> tuple[EmergencyAction, ...]:
        """Escalating response.  Each level includes the ones below it."""
        if level is RiskLevel.NORMAL:
            return ()
        actions: list[EmergencyAction] = []
        if level.rank >= RiskLevel.WARNING.rank:
            metrics = {b.metric.split(":")[0] for b in breaches}
            if "residual_exposure_bps" in metrics:
                actions.append(EmergencyAction.REBALANCE)
        if level.rank >= RiskLevel.DANGER.rank:
            actions.append(EmergencyAction.STOP_NEW_TRADES)
            actions.append(EmergencyAction.PAUSE_PAIR)
        if level.rank >= RiskLevel.EMERGENCY.rank:
            actions.append(EmergencyAction.CANCEL_OPEN_ORDERS)
            actions.append(EmergencyAction.REDUCE_EXPOSURE)
            actions.append(EmergencyAction.ENTER_EMERGENCY_MODE)
        if level is RiskLevel.KILL_SWITCH:
            actions.append(EmergencyAction.FLATTEN_POSITIONS)
        # Preserve order, drop duplicates.
        return tuple(dict.fromkeys(actions))
