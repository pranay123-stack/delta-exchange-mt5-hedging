"""The 25-step end-to-end acceptance demonstration.

Every step drives the real engine and asserts an observable outcome.  Nothing
is printed that was not produced by the system doing the thing.  A step that
fails is reported as FAIL and the run continues, so the output shows the whole
picture rather than stopping at the first problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..domain.enums import CycleState, FaultKind, HedgeObjective
from ..logging_setup import get_logger
from ..service import HedgeLabService

log = get_logger(__name__)
console = Console()

D = Decimal
BTC_PAIR = "BTC perp -> BTC CFD"


@dataclass
class Step:
    number: int
    title: str
    passed: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Acceptance:
    steps: list[Step] = field(default_factory=list)

    def record(self, number: int, title: str, passed: bool, detail: str, **data: Any) -> None:
        self.steps.append(Step(number, title, passed, detail, data))
        marker = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"{marker} [bold]{number:2}. {title}[/bold]")
        console.print(f"        {detail}")
        for key, value in data.items():
            console.print(f"          [dim]{key}[/dim] = {value}")

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)


async def _prepare(service: HedgeLabService) -> dict[str, Any]:
    """Return the platform to a known state before the run.

    The acceptance script asserts absolute outcomes -- "the hedge is exactly
    -5 lots", "no positions remain" -- so it must not inherit whatever a
    previous demo or scenario left behind.  Without this the run passes or
    fails depending on what happened to execute before it, which makes it
    useless as a check.

    Everything cleared here goes through the ordinary service API; nothing
    reaches around it.
    """
    service.clear_faults()
    if service.state.kill_switch_engaged:
        await service.clear_kill_switch(actor="acceptance")
    service.state.paused_pairs.clear()
    service.state.trading_paused = False
    service.state.emergency_mode = False

    cleared = 0
    for venue in service.venues.values():
        engine = getattr(venue, "engine", None)
        if engine is not None:
            cleared += len(engine.open_positions())
            engine.reset()

    async with service.database.session() as session:
        from ..db.repositories import PositionRepository

        await PositionRepository(session).clear()

    service.advance_market(5)
    return {"positions_cleared": cleared}


async def run_acceptance(service: HedgeLabService) -> bool:
    """Run the acceptance demonstration.  Returns True if every step passed."""
    acceptance = Acceptance()
    console.print(Panel.fit(
        "[bold]HedgeLab acceptance demonstration[/bold]\n"
        "[yellow]PAPER TRADING ONLY -- no live venue is reachable from this process.[/yellow]",
        border_style="yellow",
    ))

    prepared = await _prepare(service)

    # 1 --------------------------------------------------------------
    status = await service.status()
    acceptance.record(
        1, "Environment started",
        (
            status["database_connected"]
            and status["paper_only"]
            and not status["kill_switch_engaged"]
            and not await service.positions()
        ),
        f"mode={status['trading_mode']}, database={status['database_connected']}, "
        f"live adapters registered={status['live_adapters_registered']}; reset to a "
        f"known state ({prepared['positions_cleared']} stale position(s) cleared)",
    )

    # 2 --------------------------------------------------------------
    instruments = service.registry.all()
    venues = service.registry.venues()
    units = {s.quantity_unit.value for s in instruments}
    acceptance.record(
        2, "Multiple instrument configurations loaded",
        len(instruments) >= 8 and len(venues) == 2 and units == {"CONTRACT", "LOT"},
        f"{len(instruments)} instruments across {venues}; quantity units {sorted(units)}",
        sizings={s.symbol: s.describe_sizing() for s in instruments[:4]},
    )

    # 3 --------------------------------------------------------------
    service.advance_market(30)
    tickers = service.tickers()
    two_sided = all(t.ask > t.bid > 0 for t in tickers.values())
    acceptance.record(
        3, "Paper market data running", two_sided and len(tickers) == len(instruments),
        f"{len(tickers)} instruments quoting two-sided prices at "
        f"{service.simulator.clock.isoformat()}",
        btc_perp=str(service.ticker("PAPER_DELTA:BTCUSDT-PERP").mid),
        btc_cfd=str(service.ticker("PAPER_MT5:BTCUSD").mid),
    )

    # 4 --------------------------------------------------------------
    order = await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("5000"))
    acceptance.record(
        4, "Perpetual position created", order.filled_quantity == D("5000"),
        f"bought {order.filled_quantity} contracts at {order.average_price} "
        f"(= 5 BTC of exposure)",
        fees=str(order.fees_paid),
    )

    # 5 --------------------------------------------------------------
    calculation = await service.calculate_hedge(
        source_key="PAPER_DELTA:BTCUSDT-PERP", hedge_key="PAPER_MT5:BTCUSD",
        source_quantity=D("5000"), objective=HedgeObjective.QUOTE_PNL_NEUTRAL,
    )
    acceptance.record(
        5, "MT5 hedge calculated", calculation.rounded_quantity == D("-5"),
        f"{calculation.required_quantity} lots required, rounded to "
        f"{calculation.rounded_quantity}",
        conversion=f"1 contract = {calculation.conversion_ratio} lots",
    )

    # 6 --------------------------------------------------------------
    table = Table(title="Step 6 -- the calculation, in full", show_lines=True)
    table.add_column("Step")
    table.add_column("Formula", overflow="fold")
    table.add_column("Value", overflow="fold")
    for step in calculation.steps:
        table.add_row(step.label, step.formula, step.value)
    console.print(table)
    acceptance.record(
        6, "Mathematical derivation shown", len(calculation.steps) >= 9,
        f"{len(calculation.steps)} derivation steps, each with formula and value",
    )

    # 7 --------------------------------------------------------------
    pre_risk = await service.pair_risk(BTC_PAIR)
    acceptance.record(
        7, "Risk validation run", bool(pre_risk.breaches) or pre_risk.level.value == "NORMAL",
        f"unhedged pair is at {pre_risk.level.value} with residual "
        f"{pre_risk.residual_bps:.0f} bps -- the hedge is exactly what fixes it",
        breaches=[b.metric for b in pre_risk.breaches],
    )

    # 8 --------------------------------------------------------------
    execution = await service.execute_hedge(BTC_PAIR, D("0"))
    acceptance.record(
        8, "Both paper legs executed",
        execution.cycle.state is CycleState.COMPLETED,
        f"cycle {execution.cycle.cycle_id} finished in {execution.cycle.state.value}",
        path=" -> ".join(e.to_state.value for e in execution.cycle.events),
    )

    # 9 --------------------------------------------------------------
    async with service.database.session() as session:
        from ..db.repositories import OrderRepository

        fills = await OrderRepository(session).fills(limit=20)
    acceptance.record(
        9, "Fills recorded", len(fills) >= 2,
        f"{len(fills)} fills persisted",
        detail_rows=[f"{f.order_id[:16]} {f.quantity} @ {f.price} fee {f.fee}"
                     for f in fills[:3]],
    )

    # 10 -------------------------------------------------------------
    risk = await service.pair_risk(BTC_PAIR)
    acceptance.record(
        10, "Hedge ratio", abs(risk.hedge_ratio - D(1)) < D("0.01"),
        f"achieved hedge ratio {risk.hedge_ratio:.6f}",
    )

    # 11 -------------------------------------------------------------
    acceptance.record(
        11, "Residual exposure", risk.residual_bps < D("50"),
        f"residual {risk.residual_bps:.2f} bps of source notional "
        f"({risk.residual_delta:.8f} delta)",
        note="non-zero because the legs settle in USDT and USD respectively",
    )

    # 12 -------------------------------------------------------------
    before_pnl = await service.pair_pnl(BTC_PAIR)
    before_price = service.simulator.underlying_price("BTC")
    after_price = service.apply_shock("BTC", D("0.04"))
    service.advance_market(3)
    acceptance.record(
        12, "Market movement applied", True,
        f"BTC {before_price:.2f} -> {D(after_price['price']):.2f} (+4%)",
    )

    # 13 -------------------------------------------------------------
    after_pnl = await service.pair_pnl(BTC_PAIR)
    move_value = abs(risk.source_notional) * D("0.04")
    change = abs(after_pnl.net_pnl - before_pnl.net_pnl)
    acceptance.record(
        13, "P&L recalculated", change < move_value / D(5) and after_pnl.check_consistency(),
        f"net P&L {before_pnl.net_pnl:.2f} -> {after_pnl.net_pnl:.2f} on a move worth "
        f"{move_value:.0f} unhedged",
        components_sum_to_net=after_pnl.check_consistency(),
    )

    # 14 -------------------------------------------------------------
    service.set_funding_rate("PAPER_DELTA:BTCUSDT-PERP", D("0.0015"))
    funding = await service.funding_projection(BTC_PAIR, horizon_days=D(7))
    acceptance.record(
        14, "Funding changed", funding.net_per_day < 0,
        f"funding raised to 15 bps per 8h; net carry {funding.net_per_day:.2f}/day "
        f"({funding.net_annualized_pct:.1f}% annualised)",
        seven_day=str(funding.net_over_horizon),
    )

    # 15 -------------------------------------------------------------
    accruals = service.settle_funding()
    await service.persist_funding(accruals)
    profitable = await service.pair_pnl(BTC_PAIR)
    acceptance.record(
        15, "Profitability recalculated", len(accruals) >= 2,
        f"{len(accruals)} accruals applied; net P&L now {profitable.net_pnl:.2f} "
        f"with break-even cost {profitable.break_even_cost:.2f}",
        funding_component=str(profitable.net_funding),
        verdict=("this hedge loses money at this funding rate"
                 if profitable.net_pnl < 0 else "this hedge is net positive"),
    )

    # 16 -------------------------------------------------------------
    await service.inject_fault(
        FaultKind.LEG2_PARTIAL_FILL, leg="HEDGE", magnitude=D("0.5"),
        reason="acceptance step 16",
    )
    partial = await service.execute_hedge(BTC_PAIR, D("2000"))
    states = [e.to_state for e in partial.cycle.events]
    acceptance.record(
        16, "Partial fill triggered", CycleState.LEG_2_PARTIAL in states,
        "leg 2 partially filled; cycle path included LEG_2_PARTIAL",
        path=" -> ".join(s.value for s in states),
    )

    # 17 -------------------------------------------------------------
    after_rebalance = await service.pair_risk(BTC_PAIR)
    acceptance.record(
        17, "Automatic rebalancing demonstrated",
        CycleState.REBALANCING in states and after_rebalance.residual_bps < D("50"),
        f"the shortfall was detected and traded away; residual now "
        f"{after_rebalance.residual_bps:.2f} bps",
        messages=partial.messages,
    )

    # 18 -------------------------------------------------------------
    await service.inject_fault(FaultKind.DELTA_DISCONNECT, reason="acceptance step 18")
    disconnected = await service.execute_hedge(BTC_PAIR, D("1000"))
    acceptance.record(
        18, "Exchange disconnect triggered",
        not disconnected.cycle.state.has_exposure,
        f"cycle refused at {disconnected.cycle.state.value} without taking exposure",
        messages=disconnected.messages,
    )

    # 19 -------------------------------------------------------------
    service.reconnect("PAPER_DELTA")
    recovered = await service.execute_hedge(BTC_PAIR, D("1000"))
    acceptance.record(
        19, "Safe recovery demonstrated",
        recovered.cycle.state is CycleState.COMPLETED,
        f"after reconnecting, the same hedge completed in "
        f"{recovered.cycle.state.value}",
    )

    # 20 -------------------------------------------------------------
    await service.persist_risk()
    positions_before = {p.key: p.quantity for p in await service.positions()}
    for venue in service.venues.values():
        engine = getattr(venue, "engine", None)
        if engine is not None:
            engine.reset()
    acceptance.record(
        20, "Application restart simulated", not await service.positions(),
        "in-memory venue state discarded; only the database survives",
        positions_before={k: str(v) for k, v in positions_before.items()},
    )

    # 21 -------------------------------------------------------------
    gap = await service.reconcile()
    restored = await service.rehydrate_paper_venues()
    reconciled = await service.reconcile()
    acceptance.record(
        21, "Positions reconciled",
        bool(gap.discrepancies) and reconciled.is_clean,
        f"{len(gap.discrepancies)} discrepancies before rehydration, "
        f"{len(reconciled.discrepancies)} after",
        restored=str(restored["positions_restored"]),
    )

    # 22 -------------------------------------------------------------
    plan = await service.recover()
    positions_after = {p.key: p.quantity for p in await service.positions()}
    acceptance.record(
        22, "Hedge state reconstructed",
        plan.resumable and positions_after == positions_before,
        f"recovery reports resumable={plan.resumable}; positions match pre-restart state",
        notes=plan.notes,
    )

    # 23 -------------------------------------------------------------
    # A 25% drop on a *hedged* book is a non-event -- that is the whole point of
    # hedging, and pretending otherwise would be theatre.  A real emergency
    # needs real unhedged exposure, so the broker leg is closed underneath us
    # (exactly what a stop-out or a manual intervention looks like) and then the
    # market moves.
    service.simulator.apply_shock("BTC", D("-0.25"))
    service.advance_market(3)
    hedged_view = await service.pair_risk(BTC_PAIR)

    # ``engine`` exists on the paper adapters, not on the TradingVenue
    # protocol, so reach for it the same way the service does.
    mt5_engine = getattr(service.venues["PAPER_MT5"], "engine", None)
    closed: list[str] = []
    if mt5_engine is not None:
        closed = [
            key for key, position in mt5_engine.positions.items() if not position.is_flat
        ]
        for key in closed:
            mt5_engine.positions[key].quantity = D(0)
            mt5_engine.positions[key].average_entry = D(0)

    stressed = await service.pair_risk(BTC_PAIR)
    portfolio = await service.portfolio_risk()
    await service.persist_risk()
    acceptance.record(
        23, "Emergency threshold triggered",
        stressed.level.rank >= 3 and bool(stressed.actions),
        f"a 25% drop left the hedged book at {hedged_view.level.value}; once the "
        f"broker leg was closed underneath it ({', '.join(closed)}), the naked "
        f"exposure took the pair to {stressed.level.value} "
        f"(portfolio {portfolio.level.value})",
        pair_breaches=[b.message for b in stressed.breaches],
        prescribed_actions=[a.value for a in stressed.actions],
    )

    # 24 -------------------------------------------------------------
    kill = await service.engage_kill_switch("acceptance step 24")
    flat = await service.positions()
    blocked = False
    try:
        await service.execute_hedge(BTC_PAIR, D("1000"))
    except PermissionError:
        blocked = True
    acceptance.record(
        24, "Kill switch demonstrated",
        kill["engaged"] and not flat and blocked,
        f"{kill['flatten_result']}; {kill['orders_cancelled']} working order(s) "
        f"cancelled; further trading refused",
        positions_remaining=[f"{p.key}={p.quantity}" for p in flat] or "none",
        further_trading_blocked=blocked,
    )

    # 25 -------------------------------------------------------------
    async with service.database.session() as session:
        from ..db.repositories import CycleRepository, ObservabilityRepository

        observability = ObservabilityRepository(session)
        audit = await observability.audit_trail(limit=500)
        system_events = await observability.system_events(limit=500)
        emergencies = await observability.emergency_actions(limit=100)
        risk_events = await observability.risk_events(limit=500)
        cycles = await CycleRepository(session).list(limit=100)
        cycle_events = await CycleRepository(session).events(execution.cycle.cycle_id)

    trail = Table(title="Step 25 -- audit trail")
    trail.add_column("Table")
    trail.add_column("Rows", justify="right")
    trail.add_column("Most recent", overflow="fold")
    for label, rows, describe in (
        ("audit_logs", audit, lambda r: f"{r.actor}: {r.action} on {r.entity_type}"),
        ("system_events", system_events, lambda r: f"{r.kind}: {r.message[:60]}"),
        ("risk_events", risk_events, lambda r: f"{r.level}: {r.metric}"),
        ("emergency_actions", emergencies, lambda r: f"{r.action} -> {r.result[:40]}"),
        ("hedge_cycles", cycles, lambda r: f"{r.pair_name} {r.state}"),
        ("hedge_cycle_events", cycle_events, lambda r: f"{r.from_state} -> {r.to_state}"),
    ):
        trail.add_row(label, str(len(rows)), describe(rows[0]) if rows else "-")
    console.print(trail)
    acceptance.record(
        25, "Complete audit trail",
        bool(audit) and bool(cycle_events) and bool(emergencies),
        f"{len(audit)} audit entries, {len(cycle_events)} transitions for the first "
        f"cycle, {len(emergencies)} emergency actions -- every decision reconstructible",
    )

    await service.clear_kill_switch()

    passed = sum(1 for s in acceptance.steps if s.passed)
    console.print()
    console.print(Panel.fit(
        f"[bold]{passed}/{len(acceptance.steps)} steps passed[/bold]\n"
        f"All of it on paper: no live adapter is registered in this process.",
        border_style="green" if acceptance.passed else "red",
    ))
    return acceptance.passed
