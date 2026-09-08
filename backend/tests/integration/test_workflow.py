"""End-to-end workflow, failure recovery and restart reconstruction.

These run against the real service with a real database -- no mocks on the
path from market data through to persisted state.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedgelab.config import Settings
from hedgelab.db.repositories import CycleRepository, ObservabilityRepository, OrderRepository
from hedgelab.db.session import Database
from hedgelab.domain.enums import CycleState, FaultKind, HedgeObjective, MarketScenario
from hedgelab.domain.numeric import ZERO
from hedgelab.service import HedgeLabService

D = Decimal
BTC_PAIR = "BTC perp -> BTC CFD"


# ======================================================================
# the full pipeline
# ======================================================================
async def test_market_data_to_reconciliation_pipeline(service: HedgeLabService) -> None:
    """Market data -> calculate -> risk -> execute -> fill -> position ->
    reconcile -> monitor -> rebalance."""
    # 1. market data
    ticker = service.ticker("PAPER_DELTA:BTCUSDT-PERP")
    assert ticker.ask > ticker.bid > 0

    # 2. source position
    order = await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("5000"))
    assert order.filled_quantity == D("5000")

    # 3. calculation
    calculation = await service.calculate_hedge(
        source_key="PAPER_DELTA:BTCUSDT-PERP", hedge_key="PAPER_MT5:BTCUSD",
        source_quantity=D("5000"), objective=HedgeObjective.QUOTE_PNL_NEUTRAL,
    )
    assert calculation.rounded_quantity == D("-5")
    assert calculation.is_executable

    # 4-6. risk, execution, fills
    execution = await service.execute_hedge(BTC_PAIR, D("0"))
    assert execution.cycle.state is CycleState.COMPLETED
    states = [e.to_state for e in execution.cycle.events]
    assert CycleState.RISK_APPROVED in states
    assert CycleState.BOTH_FILLED in states

    # 7. positions
    positions = {p.key: p for p in await service.positions()}
    assert positions["PAPER_DELTA:BTCUSDT-PERP"].quantity == D("5000")
    assert positions["PAPER_MT5:BTCUSD"].quantity == D("-5")

    # 8. reconciliation
    report = await service.reconcile()
    assert report.is_clean

    # 9. monitoring
    risk = await service.pair_risk(BTC_PAIR)
    assert risk.residual_bps < D("50")

    # 10. rebalancing is not needed yet
    assessment = await service.assess_rebalance(BTC_PAIR)
    assert assessment.needs_rebalance is False

    # 11. move the market, then confirm the hedge held
    before = await service.pair_pnl(BTC_PAIR)
    service.apply_shock("BTC", D("0.05"))
    service.advance_market(3)
    after = await service.pair_pnl(BTC_PAIR)
    source_notional = abs(risk.source_notional)
    assert abs(after.net_pnl - before.net_pnl) < source_notional * D("0.01")


async def test_every_transition_is_persisted(service: HedgeLabService) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("3000"))
    execution = await service.execute_hedge(BTC_PAIR, D("0"))

    async with service.database.session() as session:
        events = await CycleRepository(session).events(execution.cycle.cycle_id)
    assert len(events) == len(execution.cycle.events)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert events[-1].to_state == "COMPLETED"
    # The calculation's working is stored with the transition that produced it.
    calculated = next(e for e in events if e.to_state == "CALCULATED")
    assert calculated.payload["steps"]


async def test_orders_and_fills_are_persisted(service: HedgeLabService) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("3000"))
    execution = await service.execute_hedge(BTC_PAIR, D("0"))

    async with service.database.session() as session:
        repo = OrderRepository(session)
        orders = await repo.list(cycle_id=execution.cycle.cycle_id)
        assert orders
        assert all(o.is_paper for o in orders)
        fills = await repo.fills()
        assert fills


async def test_duplicate_fills_are_rejected_by_the_database(
    service: HedgeLabService,
) -> None:
    """The unique exec_id constraint is the last line of defence.

    Two things are checked: the execution path already de-duplicates (so a
    fill it recorded cannot be written twice), and a *fresh* execution report
    is stored exactly once no matter how often it is replayed.
    """
    from hedgelab.domain.orders import Fill

    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("1000"))
    engine = service.venues["PAPER_DELTA"].engine
    order = next(iter(engine.orders.values()))

    async with service.database.session() as session:
        repo = OrderRepository(session)
        # The order path already persisted this one.
        assert await repo.save_fill(order.fills[0], order) is None

        replayed = Fill(order_id=order.order_id, quantity=D("1"), price=D("100"),
                        fee=D("0"), is_maker=False)
        assert await repo.save_fill(replayed, order) is not None
        assert await repo.save_fill(replayed, order) is None

        stored = await repo.fills(order_id=order.order_id)
        exec_ids = [f.exec_id for f in stored]
    assert len(exec_ids) == len(set(exec_ids))


# ======================================================================
# failure and recovery
# ======================================================================
async def test_leg_two_rejection_recovers_automatically(service: HedgeLabService) -> None:
    await service.inject_fault(FaultKind.ORDER_REJECTION, venue="PAPER_MT5", count=1)
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    states = [e.to_state for e in execution.cycle.events]

    assert CycleState.RECOVERY_REQUIRED in states
    assert execution.cycle.state is CycleState.COMPLETED
    risk = await service.pair_risk(BTC_PAIR)
    assert risk.residual_bps < D("50")


async def test_leg_one_partial_fill_resizes_the_hedge(service: HedgeLabService) -> None:
    await service.inject_fault(
        FaultKind.LEG1_PARTIAL_FILL, leg="SOURCE", magnitude=D("0.4")
    )
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    assert execution.cycle.source_filled_quantity == D("2000")
    # 2 BTC of exposure hedged with 2 lots, not the 5 originally calculated.
    assert execution.cycle.hedge_filled_quantity == D("-2")
    assert execution.cycle.state is CycleState.COMPLETED


async def test_leg_two_partial_fill_triggers_a_rebalance(service: HedgeLabService) -> None:
    await service.inject_fault(
        FaultKind.LEG2_PARTIAL_FILL, leg="HEDGE", magnitude=D("0.5")
    )
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    states = [e.to_state for e in execution.cycle.events]
    assert CycleState.LEG_2_PARTIAL in states
    assert CycleState.REBALANCING in states
    assert execution.cycle.hedge_filled_quantity == D("-5")


async def test_timeout_does_not_double_hedge(service: HedgeLabService) -> None:
    """An unknown outcome must never be blindly retried."""
    await service.inject_fault(FaultKind.API_TIMEOUT, venue="PAPER_MT5", leg="HEDGE")
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    positions = {p.key: p for p in await service.positions()}
    # Exactly one hedge position of the right size, never two.
    assert positions["PAPER_MT5:BTCUSD"].quantity == D("-5")
    assert any("not retrying" in m for m in execution.messages)


async def test_disconnect_before_leg_one_takes_no_exposure(
    service: HedgeLabService,
) -> None:
    await service.inject_fault(FaultKind.MT5_DISCONNECT)
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    assert execution.cycle.state is CycleState.FAILED
    assert not execution.cycle.state.has_exposure
    assert await service.positions() == []


async def test_stale_data_blocks_execution(service: HedgeLabService) -> None:
    service.set_scenario(MarketScenario.STALE_DATA)
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    assert execution.cycle.state is CycleState.FAILED
    assert any("stale" in m for m in execution.messages)


async def test_wide_spread_blocks_execution(service: HedgeLabService) -> None:
    service.set_scenario(MarketScenario.SPREAD_WIDENING)
    execution = await service.execute_hedge(BTC_PAIR, D("5000"))
    assert execution.cycle.state is CycleState.FAILED
    assert any("spread" in m for m in execution.messages)


async def test_recovery_is_recorded_as_a_system_event(service: HedgeLabService) -> None:
    await service.inject_fault(FaultKind.ORDER_REJECTION, venue="PAPER_MT5", count=1)
    await service.execute_hedge(BTC_PAIR, D("5000"))
    async with service.database.session() as session:
        events = await ObservabilityRepository(session).system_events()
    assert any(e.kind == "RECOVERY_REQUIRED" for e in events)


# ======================================================================
# reconciliation and restart
# ======================================================================
async def test_unknown_position_is_detected(service: HedgeLabService) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("1000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    assert (await service.reconcile()).is_clean

    service.venues["PAPER_MT5"].engine.inject_unexpected_position(
        "XAUUSD", D("0.5"), D("2400")
    )
    report = await service.reconcile()
    assert report.by_issue()["UNKNOWN_POSITION"] == 1
    assert report.has_critical


async def test_missing_position_is_detected(service: HedgeLabService) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("1000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    # The venue loses its state; the database still has it.
    service.venues["PAPER_MT5"].engine.positions.clear()
    report = await service.reconcile()
    assert "MISSING_POSITION" in report.by_issue()


async def test_quantity_mismatch_is_detected(service: HedgeLabService) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("1000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    engine = service.venues["PAPER_MT5"].engine
    position = engine.positions["PAPER_MT5:BTCUSD"]
    position.quantity += D("0.5")            # venue drifted
    report = await service.reconcile()
    assert "QUANTITY_MISMATCH" in report.by_issue()


async def test_unreachable_venue_blocks_resumption(service: HedgeLabService) -> None:
    await service.inject_fault(FaultKind.MT5_DISCONNECT)
    plan = await service.recover()
    assert plan.resumable is False
    assert any("unreachable" in a for a in plan.required_actions)


async def test_restart_reconstructs_state(settings: Settings, database: Database) -> None:
    """Kill the process mid-life and rebuild from the database alone."""
    first = HedgeLabService(settings, database=database)
    await first.startup(create_schema=False, rehydrate=False)
    first.advance_market(20)
    await first.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("4000"))
    execution = await first.execute_hedge(BTC_PAIR, D("0"))
    assert execution.cycle.state is CycleState.COMPLETED
    await first.persist_risk()
    before = {p.key: p.quantity for p in await first.positions()}

    # A brand new service object: fresh in-memory venues, same database.
    second = HedgeLabService(settings, database=database)
    await second.startup(create_schema=False, seed_reference_data=False, rehydrate=True)

    after = {p.key: p.quantity for p in await second.positions()}
    assert after == before

    plan = await second.recover()
    assert plan.resumable is True
    assert plan.reconciliation.is_clean
    assert not plan.cycles_needing_attention

    risk = await second.pair_risk(BTC_PAIR)
    assert risk.residual_bps < D("50")


async def test_restart_with_an_unfinished_cycle_requires_attention(
    settings: Settings, database: Database
) -> None:
    """A cycle that died holding exposure must block automatic resumption."""
    first = HedgeLabService(settings, database=database)
    await first.startup(create_schema=False, rehydrate=False)
    first.advance_market(20)
    await first.inject_fault(FaultKind.API_TIMEOUT, venue="PAPER_MT5", leg="HEDGE")

    # Freeze the cycle in LEG_1_FILLED by recording it directly: this is exactly
    # the row a process that died between the legs would leave behind.
    from hedgelab.execution.state_machine import CycleState as CS
    from hedgelab.execution.state_machine import HedgeCycle

    cycle = HedgeCycle(pair_name=BTC_PAIR, source_key="PAPER_DELTA:BTCUSDT-PERP",
                       hedge_key="PAPER_MT5:BTCUSD")
    for state in (CS.VALIDATED, CS.CALCULATED, CS.RISK_APPROVED,
                  CS.LEG_1_SUBMITTED, CS.LEG_1_FILLED):
        event = cycle.transition(state, "step")
        await first.recorder.save_cycle(cycle)
        await first.recorder.record_event(event)

    second = HedgeLabService(settings, database=database)
    await second.startup(create_schema=False, seed_reference_data=False, rehydrate=True)
    plan = await second.recover()

    assert plan.resumable is False
    assert any(c["state"] == "LEG_1_FILLED" for c in plan.cycles_needing_attention)
    assert any("exposure on the book" in a for a in plan.required_actions)


async def test_adopting_venue_state_clears_discrepancies(
    service: HedgeLabService,
) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("1000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    service.venues["PAPER_MT5"].engine.inject_unexpected_position(
        "XAUUSD", D("0.5"), D("2400")
    )
    assert not (await service.reconcile()).is_clean

    await service.adopt_venue_state()
    assert (await service.reconcile()).is_clean

    async with service.database.session() as session:
        audit = await ObservabilityRepository(session).audit_trail()
    assert any(e.action == "ADOPT_VENUE_STATE" for e in audit)


# ======================================================================
# emergency handling
# ======================================================================
async def test_kill_switch_flattens_everything(service: HedgeLabService) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSDT-PERP", D("2000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    assert await service.positions()

    result = await service.engage_kill_switch("integration test")
    assert result["engaged"] is True
    assert await service.positions() == []

    with pytest.raises(PermissionError, match="kill switch"):
        await service.execute_hedge(BTC_PAIR, D("1000"))

    await service.clear_kill_switch()
    execution = await service.execute_hedge(BTC_PAIR, D("1000"))
    assert execution.cycle.state is CycleState.COMPLETED


async def test_paused_pair_is_blocked(service: HedgeLabService) -> None:
    service.state.paused_pairs.add(BTC_PAIR)
    with pytest.raises(PermissionError, match="paused"):
        await service.execute_hedge(BTC_PAIR, D("1000"))


async def test_emergency_actions_are_audited(service: HedgeLabService) -> None:
    from hedgelab.domain.enums import EmergencyAction, RiskLevel

    applied = await service.apply_emergency_actions(
        RiskLevel.DANGER,
        (EmergencyAction.STOP_NEW_TRADES, EmergencyAction.CANCEL_OPEN_ORDERS),
        trigger="test", scope=BTC_PAIR,
    )
    assert len(applied) == 2
    async with service.database.session() as session:
        rows = await ObservabilityRepository(session).emergency_actions()
    assert {r.action for r in rows} >= {"STOP_NEW_TRADES", "CANCEL_OPEN_ORDERS"}


# ======================================================================
# estimated statistics
# ======================================================================
async def test_objectives_fall_back_to_assumptions_before_there_is_data(
    service: HedgeLabService,
) -> None:
    params, estimate = service.risk_parameters_for(
        "PAPER_DELTA:BTCUSDT-PERP", "PAPER_MT5:BTCUSD"
    )
    assert params.estimated is False
    assert estimate.is_reliable is False

    calculation = await service.calculate_hedge(
        source_key="PAPER_DELTA:BTCUSDT-PERP", hedge_key="PAPER_MT5:BTCUSD",
        source_quantity=D("5000"), objective=HedgeObjective.RISK_WEIGHTED,
        use_live_positions=False,
    )
    # The warning must say so rather than presenting the default as measured.
    assert any("assumptions, not measurements" in w for w in calculation.warnings)


async def test_objectives_use_measured_statistics_once_available(
    service: HedgeLabService,
) -> None:
    service.advance_market(400)
    params, estimate = service.risk_parameters_for(
        "PAPER_DELTA:BTCUSDT-PERP", "PAPER_MT5:BTCUSD"
    )
    assert params.estimated is True
    assert estimate.is_reliable is True
    assert params.source_daily_vol > ZERO
    assert params.correlation > D("0.9")

    calculation = await service.calculate_hedge(
        source_key="PAPER_DELTA:BTCUSDT-PERP", hedge_key="PAPER_MT5:BTCUSD",
        source_quantity=D("5000"), objective=HedgeObjective.RISK_WEIGHTED,
        use_live_positions=False,
    )
    assert not any("assumptions, not measurements" in w for w in calculation.warnings)
    step = next(s for s in calculation.steps if s.label == "Statistical inputs")
    assert "measured" in step.formula
    assert "beta=" in step.value


async def test_cost_aware_objectives_stay_near_beta_with_real_estimates(
    service: HedgeLabService,
) -> None:
    """Regression for the mean-variance denominator.

    Dividing the carry penalty by the *residual* variance instead of the hedge
    leg's drove the ratio to zero once statistics were measured rather than
    assumed -- on a pair whose hedge demonstrably removes almost all of the
    drawdown.
    """
    service.advance_market(400)
    params, _ = service.risk_parameters_for(
        "PAPER_DELTA:BTCUSDT-PERP", "PAPER_MT5:BTCUSD"
    )
    assert params.estimated is True
    # Correlation near 1 makes residual vol tiny -- the case that exposed it.
    assert params.residual_daily_vol < params.hedge_daily_vol / D("10")

    for objective in (HedgeObjective.FUNDING_ADJUSTED, HedgeObjective.COST_ADJUSTED):
        calculation = await service.calculate_hedge(
            source_key="PAPER_DELTA:BTCUSDT-PERP", hedge_key="PAPER_MT5:BTCUSD",
            source_quantity=D("5000"), objective=objective, use_live_positions=False,
        )
        # A realistic swap trims the hedge slightly; it must not annihilate it.
        assert abs(calculation.required_quantity) > D("4.9"), objective.value
