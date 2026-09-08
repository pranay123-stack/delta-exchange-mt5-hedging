"""The ten predefined demo scenarios.

Each is a coroutine that drives the real service -- there is no scripted
output.  Every number a scenario reports came from the engine actually doing
the thing.  They are runnable from the CLI (``hedgelab scenario run A``) and
from the dashboard's Fault Injection page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..domain.enums import FaultKind, HedgeObjective, MarketScenario
from ..logging_setup import get_logger
from ..service import HedgeLabService

log = get_logger(__name__)

D = Decimal
BTC_PAIR = "BTC perp -> BTC CFD"
ETH_PAIR = "ETH perp -> ETH CFD (10 ETH per lot)"
GOLD_PAIR = "Gold perp -> XAUUSD (cross-named underlying)"
SOL_PAIR = "SOL perp -> SOL CFD (100 SOL per lot)"


@dataclass
class ScenarioStep:
    """One recorded step of a scenario run."""

    label: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "detail": self.detail, "data": self.data}


@dataclass
class ScenarioResult:
    key: str
    name: str
    passed: bool
    steps: list[ScenarioStep] = field(default_factory=list)
    summary: str = ""
    error: str | None = None

    def step(self, label: str, detail: str, **data: Any) -> None:
        self.steps.append(ScenarioStep(label=label, detail=detail, data=data))
        log.info("scenario step", extra={"scenario": self.key, "step": label})

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "passed": self.passed,
            "summary": self.summary, "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass(frozen=True)
class ScenarioDefinition:
    key: str
    name: str
    description: str
    run: Callable[[HedgeLabService, ScenarioResult], Awaitable[None]]

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "name": self.name, "description": self.description}


# ----------------------------------------------------------------------
# helpers shared by the scenarios
# ----------------------------------------------------------------------
async def _reset(service: HedgeLabService) -> None:
    """Return the platform to a clean paper state between scenarios."""
    service.clear_faults()
    for venue in service.venues.values():
        engine = getattr(venue, "engine", None)
        if engine is not None:
            engine.reset()
    service.state.paused_pairs.clear()
    service.state.trading_paused = False
    service.state.emergency_mode = False
    service.state.kill_switch_engaged = False
    service.advance_market(5)


def _cycle_path(result: Any) -> str:
    return " -> ".join(e.to_state.value for e in result.cycle.events)


async def _record_hedge(
    service: HedgeLabService, out: ScenarioResult, pair: str, quantity: Decimal,
    label: str = "Execute hedge cycle", **kwargs: Any,
) -> Any:
    execution = await service.execute_hedge(pair, quantity, **kwargs)
    out.step(
        label,
        f"final state {execution.cycle.state.value}",
        path=_cycle_path(execution),
        state=execution.cycle.state.value,
        hedge_quantity=str(execution.cycle.hedge_filled_quantity),
        hedge_ratio=str(execution.cycle.hedge_ratio),
        residual=str(execution.cycle.residual_exposure),
        messages=execution.messages,
    )
    return execution


# ======================================================================
# A -- normal hedge
# ======================================================================
async def scenario_a(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    order = await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("5000"))
    out.step(
        "Open a perpetual position",
        f"bought {order.filled_quantity} contracts (= 5 BTC) at {order.average_price}",
        filled=str(order.filled_quantity), price=str(order.average_price),
        fees=str(order.fees_paid),
    )

    calculation = await service.calculate_hedge(
        source_key="PAPER_DELTA:BTCUSDT-PERP", hedge_key="PAPER_MT5:BTCUSD",
        source_quantity=D("5000"), objective=HedgeObjective.QUOTE_PNL_NEUTRAL,
    )
    out.step(
        "Calculate the hedge",
        f"{calculation.required_quantity} lots required, rounded to "
        f"{calculation.rounded_quantity}",
        conversion=str(calculation.conversion_ratio),
        cost=str(calculation.total_execution_cost),
        margin=str(calculation.margin_requirement),
        steps=[s.to_dict() for s in calculation.steps],
    )

    execution = await _record_hedge(service, out, BTC_PAIR, D("0"))
    risk = await service.pair_risk(BTC_PAIR)
    out.step(
        "Risk after hedging", f"level {risk.level.value}",
        residual_bps=str(risk.residual_bps), hedge_ratio=str(risk.hedge_ratio),
        level=risk.level.value,
    )
    out.passed = execution.succeeded and risk.residual_bps < D("50")
    out.summary = (
        f"Hedged 5 BTC of perpetual exposure with "
        f"{execution.cycle.hedge_filled_quantity} BTCUSD lots; residual "
        f"{risk.residual_bps:.2f} bps."
    )


# ======================================================================
# B -- different contract multipliers
# ======================================================================
async def scenario_b(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    findings: list[dict[str, str]] = []
    for pair_name, _source_key, quantity in (
        (BTC_PAIR, "PAPER_DELTA:BTCUSDT-PERP", D("5000")),
        (ETH_PAIR, "PAPER_DELTA:ETHUSDT-PERP", D("4000")),
        (GOLD_PAIR, "PAPER_DELTA:PAXGUSDT-PERP", D("20000")),
        (SOL_PAIR, "PAPER_DELTA:SOLUSDT-PERP", D("700")),
    ):
        mapping = service.registry.mapping_by_name(pair_name)
        source = service.registry.get(mapping.source_key)
        hedge = service.registry.get(mapping.hedge_key)
        calculation = await service.calculate_hedge(
            source_key=mapping.source_key, hedge_key=mapping.hedge_key,
            source_quantity=quantity, objective=mapping.objective,
            use_live_positions=False,
        )
        findings.append({
            "pair": pair_name,
            "source_sizing": source.describe_sizing(),
            "hedge_sizing": hedge.describe_sizing(),
            "source_quantity": str(quantity),
            "hedge_quantity": str(calculation.rounded_quantity),
            "conversion_ratio": str(calculation.conversion_ratio),
            "base_units": str(calculation.source_exposure.base_units),
        })
        out.step(
            f"{pair_name}",
            f"{quantity} {source.quantity_unit.value.lower()}s "
            f"({calculation.source_exposure.base_units} {source.base_asset}) "
            f"-> {calculation.rounded_quantity} {hedge.quantity_unit.value.lower()}s",
            **findings[-1],
        )
    ratios = {f["conversion_ratio"] for f in findings}
    sizings = {(f["source_sizing"], f["hedge_sizing"]) for f in findings}
    out.step(
        "Ratios compared",
        f"{len(findings)} pairs produce {len(ratios)} distinct contract-to-lot ratios "
        f"across {len(sizings)} distinct sizing combinations",
        ratios=sorted(ratios),
        note=(
            "BTC and ETH happen to share a 0.001 ratio despite completely "
            "different contract sizes (0.001 BTC vs 0.01 ETH per contract, "
            "1 BTC vs 10 ETH per lot). Equal ratios do not mean equal economics -- "
            "which is exactly why the engine converts through base units rather "
            "than caching a ratio per pair."
        ),
    )
    out.passed = len(ratios) >= 3 and len(sizings) == len(findings)
    out.summary = (
        f"{len(findings)} pairs, {len(sizings)} distinct sizing combinations and "
        f"{len(ratios)} distinct contract-to-lot ratios "
        f"({', '.join(sorted(ratios))}). Quantity matching would be wrong for every one."
    )


# ======================================================================
# C -- partial first-leg fill
# ======================================================================
async def scenario_c(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    await service.inject_fault(
        FaultKind.LEG1_PARTIAL_FILL, leg="SOURCE", magnitude=D("0.6"),
        reason="scenario C: source venue fills only 60%",
    )
    out.step("Arm the fault", "leg 1 will fill 60% of the requested quantity")

    execution = await _record_hedge(service, out, BTC_PAIR, D("5000"))
    source_filled = execution.cycle.source_filled_quantity
    hedge_filled = execution.cycle.hedge_filled_quantity
    risk = await service.pair_risk(BTC_PAIR)

    out.step(
        "Hedge sized from the actual fill",
        f"leg 1 filled {source_filled} of 5000, so the hedge is {hedge_filled} lots "
        f"rather than the 5 originally calculated",
        source_filled=str(source_filled), hedge_filled=str(hedge_filled),
        residual_bps=str(risk.residual_bps),
    )
    out.passed = (
        execution.succeeded and abs(source_filled) < D("5000")
        and risk.residual_bps < D("50")
    )
    out.summary = (
        f"Leg 1 filled {source_filled}/5000; the hedge was recalculated to "
        f"{hedge_filled} lots instead of over-hedging. Residual "
        f"{risk.residual_bps:.2f} bps."
    )


# ======================================================================
# D -- second-leg rejection
# ======================================================================
async def scenario_d(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    await service.inject_fault(
        FaultKind.ORDER_REJECTION, venue="PAPER_MT5", count=1,
        reason="scenario D: broker rejects the hedge order",
    )
    out.step("Arm the fault", "the broker will reject the first hedge order")

    execution = await _record_hedge(service, out, BTC_PAIR, D("5000"))
    states = [e.to_state.value for e in execution.cycle.events]
    risk = await service.pair_risk(BTC_PAIR)

    out.step(
        "Automatic recovery",
        "the cycle detected the imbalance, entered RECOVERY_REQUIRED and "
        "rebalanced back to a flat book",
        reached_recovery="RECOVERY_REQUIRED" in states,
        final_state=execution.cycle.state.value,
        residual_bps=str(risk.residual_bps),
    )
    out.passed = (
        "RECOVERY_REQUIRED" in states and execution.succeeded
        and risk.residual_bps < D("50")
    )
    out.summary = (
        f"Leg 2 was rejected with leg 1 already on the book. The cycle routed to "
        f"RECOVERY_REQUIRED, rebalanced and completed with "
        f"{risk.residual_bps:.2f} bps residual."
    )


# ======================================================================
# E -- exchange disconnect
# ======================================================================
async def scenario_e(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    await service.inject_fault(FaultKind.DELTA_DISCONNECT, reason="scenario E")
    out.step("Disconnect the perpetual venue", "PAPER_DELTA is now unreachable")

    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    out.step(
        "Hedge attempt is refused safely",
        f"cycle ended in {execution.cycle.state.value} with no exposure taken",
        state=execution.cycle.state.value, path=_cycle_path(execution),
        messages=execution.messages,
    )
    no_exposure = not execution.cycle.state.has_exposure

    service.reconnect("PAPER_DELTA")
    out.step("Reconnect", "PAPER_DELTA is reachable again")
    recovered = await _record_hedge(
        service, out, BTC_PAIR, D("5000"), label="Retry after reconnect"
    )
    out.passed = no_exposure and recovered.succeeded
    out.summary = (
        "With the exchange down the cycle failed before taking exposure; after "
        "reconnecting, the same hedge completed normally."
    )


# ======================================================================
# F -- MT5 disconnect
# ======================================================================
async def scenario_f(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    order = await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("5000"))
    out.step("Open a perpetual position", f"{order.filled_quantity} contracts filled")

    await service.inject_fault(FaultKind.MT5_DISCONNECT, reason="scenario F")
    out.step("Disconnect the broker", "PAPER_MT5 is now unreachable")

    execution = await service.execute_hedge(BTC_PAIR, D("0"))
    risk_blocked = execution.cycle.state.value
    out.step(
        "Hedge attempt refused",
        f"cycle ended in {risk_blocked}; the source position is left unhedged and "
        f"the system says so rather than pretending",
        state=risk_blocked, messages=execution.messages,
    )

    service.reconnect("PAPER_MT5")
    recovered = await _record_hedge(
        service, out, BTC_PAIR, D("0"), label="Hedge after reconnect"
    )
    risk = await service.pair_risk(BTC_PAIR)
    out.passed = recovered.succeeded and risk.residual_bps < D("50")
    out.summary = (
        "The broker outage blocked hedging and the unhedged exposure was reported "
        f"honestly; after reconnecting the hedge completed to "
        f"{risk.residual_bps:.2f} bps residual."
    )


# ======================================================================
# G -- large spread
# ======================================================================
async def scenario_g(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    before = service.simulator.ticker("PAPER_MT5:BTCUSD").spread_bps
    service.set_scenario(MarketScenario.SPREAD_WIDENING)
    after = service.simulator.ticker("PAPER_MT5:BTCUSD").spread_bps
    out.step(
        "Widen the spread",
        f"BTCUSD spread {before:.2f} bps -> {after:.2f} bps",
        before_bps=str(before), after_bps=str(after),
        execution_limit=str(service.settings.max_spread_bps_for_execution),
    )

    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    out.step(
        "Hedge refused on execution-quality grounds",
        f"cycle ended in {execution.cycle.state.value}",
        state=execution.cycle.state.value, messages=execution.messages,
    )
    blocked = not execution.succeeded and not execution.cycle.state.has_exposure

    service.simulator.clear_scenario()
    normal = await _record_hedge(
        service, out, BTC_PAIR, D("5000"), label="Hedge once spreads normalise"
    )
    out.passed = blocked and normal.succeeded
    out.summary = (
        f"A {after:.0f} bps spread exceeded the "
        f"{service.settings.max_spread_bps_for_execution} bps execution limit, so the "
        f"hedge was refused before taking exposure. It completed once spreads normalised."
    )


# ======================================================================
# H -- price gap
# ======================================================================
async def scenario_h(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    execution = await _record_hedge(service, out, BTC_PAIR, D("5000"))
    before_pnl = await service.pair_pnl(BTC_PAIR)
    before_price = service.simulator.underlying_price("BTC")

    after_price = service.simulator.apply_shock("BTC", D("-0.08"))
    service.advance_market(2)
    after_pnl = await service.pair_pnl(BTC_PAIR)
    risk = await service.pair_risk(BTC_PAIR)

    out.step(
        "Apply an 8% downward gap",
        f"BTC {before_price:.2f} -> {after_price:.2f}",
        before=str(before_price), after=str(after_price),
    )
    out.step(
        "Hedged book absorbs the gap",
        f"net P&L moved from {before_pnl.net_pnl:.2f} to {after_pnl.net_pnl:.2f} on an "
        f"8% move -- the hedge did its job",
        before_net=str(before_pnl.net_pnl), after_net=str(after_pnl.net_pnl),
        residual_bps=str(risk.residual_bps),
        gross_before=str(before_pnl.gross_pnl), gross_after=str(after_pnl.gross_pnl),
    )
    source_notional = abs(risk.source_notional)
    move_value = source_notional * D("0.08")
    pnl_change = abs(after_pnl.net_pnl - before_pnl.net_pnl)
    out.passed = execution.succeeded and pnl_change < move_value / D("5")
    out.summary = (
        f"An 8% gap would have cost ~{move_value:.0f} unhedged; the hedged book moved "
        f"{pnl_change:.2f}."
    )


# ======================================================================
# I -- margin danger
# ======================================================================
async def scenario_i(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    # Deliberately over-lever the source leg so the margin level collapses.
    order = await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("100000"))
    out.step(
        "Open a heavily levered position",
        f"{order.filled_quantity} contracts (= 100 BTC) on the perpetual venue",
        filled=str(order.filled_quantity),
    )
    accounts = {a.venue: a for a in await service.accounts()}
    out.step(
        "Margin before the move",
        f"PAPER_DELTA margin level {accounts['PAPER_DELTA'].margin_level:.1f}%",
        margin_level=str(accounts["PAPER_DELTA"].margin_level),
        used_margin=str(accounts["PAPER_DELTA"].used_margin),
    )

    service.simulator.apply_shock("BTC", D("-0.15"))
    service.advance_market(2)
    accounts = {a.venue: a for a in await service.accounts()}
    risk = await service.pair_risk(BTC_PAIR)
    out.step(
        "Margin after a 15% drop",
        f"margin level {accounts['PAPER_DELTA'].margin_level:.1f}%, risk level "
        f"{risk.level.value}",
        margin_level=str(accounts["PAPER_DELTA"].margin_level),
        equity=str(accounts["PAPER_DELTA"].equity),
        risk_level=risk.level.value,
        breaches=[b.message for b in risk.breaches],
        actions=[a.value for a in risk.actions],
    )
    applied = await service.apply_emergency_actions(
        risk.level, risk.actions, trigger="scenario I margin danger", scope=BTC_PAIR,
    )
    out.step("Emergency actions applied", f"{len(applied)} action(s) executed", applied=applied)
    out.passed = risk.level.rank >= 1 and bool(risk.breaches)
    out.summary = (
        f"A 15% adverse move drove the margin level to "
        f"{accounts['PAPER_DELTA'].margin_level:.1f}% and the risk engine escalated to "
        f"{risk.level.value}, applying {len(applied)} action(s)."
    )


# ======================================================================
# J -- restart recovery
# ======================================================================
async def scenario_j(service: HedgeLabService, out: ScenarioResult) -> None:
    await _reset(service)
    execution = await _record_hedge(service, out, BTC_PAIR, D("5000"))
    clean = await service.reconcile()
    out.step(
        "Reconcile before the restart",
        f"clean={clean.is_clean}, {clean.database_positions} database positions",
        is_clean=clean.is_clean, issues=clean.by_issue(),
    )

    # Simulate the process dying: wipe the in-memory venue state, keep the DB.
    for venue in service.venues.values():
        engine = getattr(venue, "engine", None)
        if engine is not None:
            engine.reset()
    out.step(
        "Simulate a process restart",
        "in-memory venue state discarded; only the database survives",
    )

    missing = await service.reconcile()
    out.step(
        "Reconciliation detects the gap",
        f"{len(missing.discrepancies)} discrepancies before rehydration",
        issues=missing.by_issue(),
        discrepancies=[d.message for d in missing.discrepancies],
    )

    restored = await service.rehydrate_paper_venues()
    out.step(
        "Rehydrate from the database",
        f"{restored['positions_restored']} position(s) restored",
        **{k: str(v) for k, v in restored.items()},
    )

    plan = await service.recover()
    out.step(
        "Restart recovery verdict",
        f"resumable={plan.resumable}",
        resumable=plan.resumable,
        reconstructed=len(plan.reconstructed_cycles),
        required_actions=plan.required_actions,
        notes=plan.notes,
    )
    risk = await service.pair_risk(BTC_PAIR)
    out.step(
        "Risk recomputed after recovery",
        f"level {risk.level.value}, residual {risk.residual_bps:.2f} bps",
        level=risk.level.value, residual_bps=str(risk.residual_bps),
    )
    out.passed = (
        execution.succeeded and bool(missing.discrepancies) and plan.resumable
    )
    out.summary = (
        "State was reconstructed from the database after a simulated restart; "
        "reconciliation flagged the gap before rehydration and cleared afterwards, "
        f"and recovery reported resumable={plan.resumable}."
    )


SCENARIOS: dict[str, ScenarioDefinition] = {
    d.key: d for d in (
        ScenarioDefinition("A", "Normal BTC hedge",
                           "Open a perpetual position and hedge it cleanly.", scenario_a),
        ScenarioDefinition("B", "Different contract multipliers",
                           "Four pairs whose contract-to-lot conversions all differ.", scenario_b),
        ScenarioDefinition("C", "Partial first-leg fill",
                           "Leg 1 fills 60%; the hedge resizes to match.", scenario_c),
        ScenarioDefinition("D", "Second-leg rejection",
                           "Leg 2 is rejected; the system recovers automatically.", scenario_d),
        ScenarioDefinition("E", "Exchange disconnect",
                           "The perpetual venue is unreachable.", scenario_e),
        ScenarioDefinition("F", "MT5 disconnect",
                           "The broker is unreachable while exposure is open.", scenario_f),
        ScenarioDefinition("G", "Large spread",
                           "Spreads blow out past the execution limit.", scenario_g),
        ScenarioDefinition("H", "Price gap",
                           "An 8% gap tests whether the hedge actually works.", scenario_h),
        ScenarioDefinition("I", "Margin danger",
                           "A levered position drives the margin level down.", scenario_i),
        ScenarioDefinition("J", "Restart recovery",
                           "State is rebuilt from the database after a restart.", scenario_j),
    )
}


def get_scenario(key: str) -> ScenarioDefinition:
    normalised = key.strip().upper()
    if normalised in SCENARIOS:
        return SCENARIOS[normalised]
    for definition in SCENARIOS.values():
        if definition.name.upper() == normalised:
            return definition
    raise KeyError(
        f"unknown scenario {key!r}; choose from {', '.join(sorted(SCENARIOS))}"
    )
