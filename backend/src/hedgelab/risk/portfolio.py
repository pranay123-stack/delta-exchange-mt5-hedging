"""Portfolio-level risk.

Per-pair risk is necessary but not sufficient.  Five pairs each sitting at 90%
of their individual limit is a portfolio at 450% of what any one of them was
allowed, and five "hedged" pairs can still be one big directional bet if they
all hold the same underlying on the same side.

This module aggregates across pairs and enforces limits that only exist at the
portfolio level: total notional, concentration, aggregate margin, aggregate
loss, and net currency exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.account import AccountSnapshot
from ..domain.enums import EmergencyAction, RiskLevel
from ..domain.market import utcnow
from ..domain.numeric import ZERO, quantize, safe_div
from ..logging_setup import get_logger
from .engine import PairRisk, RiskBreach

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PortfolioLimits:
    max_total_notional: Decimal = Decimal(5_000_000)
    max_aggregate_margin_utilization: Decimal = Decimal(70)   # percent of equity
    max_concentration_pct: Decimal = Decimal(60)              # one underlying
    max_daily_loss: Decimal = Decimal(25_000)
    max_net_exposure_pct: Decimal = Decimal(15)               # of total notional
    max_currency_exposure: Decimal = Decimal(1_000_000)
    warning_fraction: Decimal = Decimal("0.75")
    danger_fraction: Decimal = Decimal("0.90")


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    """Aggregate view across every hedge pair and both venues."""

    level: RiskLevel
    breaches: tuple[RiskBreach, ...]
    actions: tuple[EmergencyAction, ...]

    # exposure
    total_source_exposure: Decimal
    total_hedge_exposure: Decimal
    net_exposure: Decimal
    gross_notional: Decimal
    net_exposure_pct: Decimal

    # by dimension
    exposure_by_venue: dict[str, Decimal]
    exposure_by_underlying: dict[str, Decimal]
    currency_exposure: dict[str, Decimal]

    # capital
    aggregate_equity: Decimal
    aggregate_margin: Decimal
    aggregate_free_margin: Decimal
    margin_utilization_pct: Decimal
    worst_margin_level: Decimal
    aggregate_liquidation_risk: Decimal | None

    # P&L
    portfolio_pnl: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    aggregate_funding_per_day: Decimal
    aggregate_fees: Decimal
    portfolio_max_loss: Decimal

    # structure
    concentration_pct: Decimal
    concentration_underlying: str
    pair_count: int
    breaching_pairs: tuple[str, ...]

    currency: str = "USD"
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def allows_new_trades(self) -> bool:
        return self.level.rank < RiskLevel.DANGER.rank

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "allows_new_trades": self.allows_new_trades,
            "breaches": [b.to_dict() for b in self.breaches],
            "actions": [a.value for a in self.actions],
            "total_source_exposure": str(quantize(self.total_source_exposure, 2)),
            "total_hedge_exposure": str(quantize(self.total_hedge_exposure, 2)),
            "net_exposure": str(quantize(self.net_exposure, 2)),
            "gross_notional": str(quantize(self.gross_notional, 2)),
            "net_exposure_pct": str(quantize(self.net_exposure_pct, 4)),
            "exposure_by_venue": {k: str(quantize(v, 2)) for k, v in self.exposure_by_venue.items()},
            "exposure_by_underlying": {
                k: str(quantize(v, 2)) for k, v in self.exposure_by_underlying.items()
            },
            "currency_exposure": {k: str(quantize(v, 2)) for k, v in self.currency_exposure.items()},
            "aggregate_equity": str(quantize(self.aggregate_equity, 2)),
            "aggregate_margin": str(quantize(self.aggregate_margin, 2)),
            "aggregate_free_margin": str(quantize(self.aggregate_free_margin, 2)),
            "margin_utilization_pct": str(quantize(self.margin_utilization_pct, 2)),
            "worst_margin_level": str(quantize(self.worst_margin_level, 2)),
            "aggregate_liquidation_risk": (
                str(quantize(self.aggregate_liquidation_risk, 6))
                if self.aggregate_liquidation_risk is not None else None
            ),
            "portfolio_pnl": str(quantize(self.portfolio_pnl, 2)),
            "unrealized_pnl": str(quantize(self.unrealized_pnl, 2)),
            "realized_pnl": str(quantize(self.realized_pnl, 2)),
            "aggregate_funding_per_day": str(quantize(self.aggregate_funding_per_day, 2)),
            "aggregate_fees": str(quantize(self.aggregate_fees, 2)),
            "portfolio_max_loss": str(quantize(self.portfolio_max_loss, 2)),
            "concentration_pct": str(quantize(self.concentration_pct, 2)),
            "concentration_underlying": self.concentration_underlying,
            "pair_count": self.pair_count,
            "breaching_pairs": list(self.breaching_pairs),
            "currency": self.currency,
            "timestamp": self.timestamp.isoformat(),
        }


class PortfolioRiskEngine:
    """Aggregates :class:`PairRisk` records into a portfolio verdict."""

    def __init__(self, limits: PortfolioLimits | None = None) -> None:
        self.limits = limits or PortfolioLimits()

    def evaluate(
        self,
        pair_risks: list[PairRisk],
        accounts: list[AccountSnapshot],
        *,
        underlying_by_pair: dict[str, str] | None = None,
        currency_exposure: dict[str, Decimal] | None = None,
        currency: str = "USD",
    ) -> PortfolioRisk:
        underlying_by_pair = underlying_by_pair or {}
        breaches: list[RiskBreach] = []

        total_source = sum((abs(r.source_notional) for r in pair_risks), ZERO)
        total_hedge = sum((abs(r.hedge_notional) for r in pair_risks), ZERO)
        gross = total_source + total_hedge
        # Net exposure is the sum of *signed* residuals, not of magnitudes:
        # two pairs residual-long and residual-short genuinely offset.
        net = sum((r.residual_delta * (r.hedge_liquidation.mark_price
                                       if r.hedge_liquidation else Decimal(1))
                   for r in pair_risks), ZERO)
        net_pct = safe_div(abs(net), gross) * Decimal(100)

        exposure_by_venue: dict[str, Decimal] = {}
        exposure_by_underlying: dict[str, Decimal] = {}
        for risk in pair_risks:
            src_venue = risk.source_key.split(":")[0]
            hdg_venue = risk.hedge_key.split(":")[0]
            exposure_by_venue[src_venue] = exposure_by_venue.get(src_venue, ZERO) + abs(risk.source_notional)
            exposure_by_venue[hdg_venue] = exposure_by_venue.get(hdg_venue, ZERO) + abs(risk.hedge_notional)
            key = underlying_by_pair.get(risk.pair_name, risk.pair_name)
            exposure_by_underlying[key] = exposure_by_underlying.get(key, ZERO) + abs(risk.source_notional)

        equity = sum((a.equity for a in accounts), ZERO)
        used = sum((a.used_margin for a in accounts), ZERO)
        free = sum((a.free_margin for a in accounts), ZERO)
        utilization = safe_div(used, equity) * Decimal(100)
        worst_margin = min((a.margin_level for a in accounts), default=Decimal("999999"))

        unrealized = sum((r.unrealized_pnl for r in pair_risks), ZERO)
        realized = sum((r.realized_pnl for r in pair_risks), ZERO)
        daily = sum((r.daily_pnl for r in pair_risks), ZERO)
        funding = sum((r.funding_risk_per_day for r in pair_risks), ZERO)
        fees = sum((a.fees_paid for a in accounts), ZERO)

        tolerable = [
            r.max_tolerable_move for r in pair_risks if r.max_tolerable_move is not None
        ]
        aggregate_liq = min(tolerable) if tolerable else None

        concentration_pct, concentration_key = self._concentration(exposure_by_underlying, total_source)

        # Worst plausible loss: everything at its liquidation distance at once.
        max_loss = self._portfolio_max_loss(pair_risks, aggregate_liq)

        self._check_limit(breaches, "gross_notional", gross, self.limits.max_total_notional,
                          "gross notional across all pairs (both legs counted, so a "
                          "fully hedged pair contributes twice its position size)")
        self._check_limit(breaches, "margin_utilization", utilization,
                          self.limits.max_aggregate_margin_utilization,
                          "aggregate margin as a percentage of equity")
        self._check_concentration(
            breaches, concentration_pct, concentration_key, exposure_by_underlying
        )
        self._check_limit(breaches, "net_exposure_pct", net_pct, self.limits.max_net_exposure_pct,
                          "net (unhedged) exposure as a percentage of gross notional")
        if daily < ZERO:
            self._check_limit(breaches, "portfolio_daily_loss", -daily, self.limits.max_daily_loss,
                              "aggregate daily loss")
        for ccy, amount in (currency_exposure or {}).items():
            self._check_limit(breaches, f"currency_exposure:{ccy}", abs(amount),
                              self.limits.max_currency_exposure,
                              f"net settlement exposure in {ccy}")

        # A pair in EMERGENCY drags the portfolio with it.
        worst_pair_level = max((r.level for r in pair_risks), key=lambda x: x.rank,
                               default=RiskLevel.NORMAL)
        if worst_pair_level.rank >= RiskLevel.EMERGENCY.rank:
            breaches.append(RiskBreach(
                metric="pair_escalation",
                level=worst_pair_level,
                value=Decimal(worst_pair_level.rank),
                threshold=Decimal(RiskLevel.EMERGENCY.rank),
                message=f"at least one pair is at {worst_pair_level.value}",
            ))

        level = max((b.level for b in breaches), key=lambda x: x.rank, default=RiskLevel.NORMAL)
        actions = self._actions_for(level)

        result = PortfolioRisk(
            level=level,
            breaches=tuple(breaches),
            actions=actions,
            total_source_exposure=total_source,
            total_hedge_exposure=total_hedge,
            net_exposure=net,
            gross_notional=gross,
            net_exposure_pct=net_pct,
            exposure_by_venue=exposure_by_venue,
            exposure_by_underlying=exposure_by_underlying,
            currency_exposure=dict(currency_exposure or {}),
            aggregate_equity=equity,
            aggregate_margin=used,
            aggregate_free_margin=free,
            margin_utilization_pct=utilization,
            worst_margin_level=worst_margin,
            aggregate_liquidation_risk=aggregate_liq,
            portfolio_pnl=unrealized + realized,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            aggregate_funding_per_day=funding,
            aggregate_fees=fees,
            portfolio_max_loss=max_loss,
            concentration_pct=concentration_pct,
            concentration_underlying=concentration_key,
            pair_count=len(pair_risks),
            breaching_pairs=tuple(r.pair_name for r in pair_risks if r.level is not RiskLevel.NORMAL),
            currency=currency,
        )
        if level is not RiskLevel.NORMAL:
            log.warning(
                "portfolio risk breach",
                extra={"level": level.value, "breaches": [b.metric for b in breaches],
                       "gross_notional": str(quantize(gross, 2))},
            )
        return result

    # ------------------------------------------------------------------
    def _check_limit(
        self,
        breaches: list[RiskBreach],
        metric: str,
        value: Decimal,
        limit: Decimal,
        description: str,
        max_level: RiskLevel = RiskLevel.EMERGENCY,
    ) -> None:
        """Graded check: warn at 75% of the limit, danger at 90%, breach at 100%.

        ``max_level`` caps how far a metric can escalate.  Not every limit
        deserves an emergency response -- some describe portfolio shape rather
        than solvency.
        """
        if limit <= ZERO:
            return
        for fraction, level in (
            (Decimal(1), RiskLevel.EMERGENCY),
            (self.limits.danger_fraction, RiskLevel.DANGER),
            (self.limits.warning_fraction, RiskLevel.WARNING),
        ):
            threshold = limit * fraction
            if value >= threshold:
                if level.rank > max_level.rank:
                    level = max_level
                breaches.append(RiskBreach(
                    metric=metric,
                    level=level,
                    value=value,
                    threshold=threshold,
                    message=(
                        f"{description} is {quantize(value, 2)}, at or above "
                        f"{quantize(fraction * 100, 0)}% of the {quantize(limit, 2)} limit"
                    ),
                ))
                return

    def _check_concentration(
        self,
        breaches: list[RiskBreach],
        concentration_pct: Decimal,
        concentration_key: str,
        exposure_by_underlying: dict[str, Decimal],
    ) -> None:
        """Concentration needs its own grading, not the generic 75/90/100 ramp.

        Two facts make the generic check useless here.  First, a portfolio with
        ``n`` underlyings has a *floor* of ``100/n`` percent concentration, so
        warning at 75% of a 60% limit (45%) would fire on every two-asset book
        no matter how evenly balanced.  Second, a single open pair is trivially
        100% concentrated, which says nothing at all.

        So: skip the check below two underlyings, breach only at or above the
        limit, and escalate to DANGER only past the midpoint between the limit
        and total concentration.  Concentration never reaches EMERGENCY -- it
        describes portfolio shape, not solvency.
        """
        active = [v for v in exposure_by_underlying.values() if v > ZERO]
        if len(active) < 2:
            return
        limit = self.limits.max_concentration_pct
        if limit <= ZERO or concentration_pct < limit:
            return
        danger_at = limit + (Decimal(100) - limit) / Decimal(2)
        level = RiskLevel.DANGER if concentration_pct >= danger_at else RiskLevel.WARNING
        breaches.append(RiskBreach(
            metric="concentration",
            level=level,
            value=concentration_pct,
            threshold=limit,
            message=(
                f"{quantize(concentration_pct, 2)}% of source exposure is in "
                f"{concentration_key}, above the {limit}% limit across "
                f"{len(active)} underlyings"
            ),
        ))

    @staticmethod
    def _concentration(
        exposure_by_underlying: dict[str, Decimal], total: Decimal
    ) -> tuple[Decimal, str]:
        if not exposure_by_underlying or total <= ZERO:
            return ZERO, ""
        key, amount = max(exposure_by_underlying.items(), key=lambda kv: kv[1])
        return safe_div(amount, total) * Decimal(100), key

    @staticmethod
    def _portfolio_max_loss(
        pair_risks: list[PairRisk], aggregate_liq: Decimal | None
    ) -> Decimal:
        """Loss if every residual moved adversely by the tightest liquidation distance.

        A deliberately blunt stress: it assumes perfect adverse correlation
        across pairs, which is the right assumption for a limit that exists to
        stop a correlated blow-up.
        """
        if aggregate_liq is None:
            return ZERO
        total = ZERO
        for risk in pair_risks:
            mark = risk.hedge_liquidation.mark_price if risk.hedge_liquidation else Decimal(1)
            total += abs(risk.residual_delta) * mark * aggregate_liq
        return total

    @staticmethod
    def _actions_for(level: RiskLevel) -> tuple[EmergencyAction, ...]:
        if level is RiskLevel.NORMAL:
            return ()
        actions: list[EmergencyAction] = []
        if level.rank >= RiskLevel.WARNING.rank:
            actions.append(EmergencyAction.REBALANCE)
        if level.rank >= RiskLevel.DANGER.rank:
            actions.append(EmergencyAction.STOP_NEW_TRADES)
        if level.rank >= RiskLevel.EMERGENCY.rank:
            actions.extend([
                EmergencyAction.CANCEL_OPEN_ORDERS,
                EmergencyAction.REDUCE_EXPOSURE,
                EmergencyAction.ENTER_EMERGENCY_MODE,
            ])
        if level is RiskLevel.KILL_SWITCH:
            actions.append(EmergencyAction.FLATTEN_POSITIONS)
        return tuple(dict.fromkeys(actions))
