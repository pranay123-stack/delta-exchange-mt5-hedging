"""Hedge-cycle state machine: legality, reachability and failure routing."""

from __future__ import annotations

import pytest

from hedgelab.domain.enums import CycleState, Leg
from hedgelab.execution.state_machine import (
    HedgeCycle,
    IllegalTransition,
    allowed_targets,
    can_transition,
    describe_graph,
    reachable_states,
    validate_graph,
)

HAPPY_PATH = [
    CycleState.VALIDATED, CycleState.AWAITING_MARKET_DATA, CycleState.CALCULATED,
    CycleState.RISK_APPROVED, CycleState.LEG_1_SUBMITTED, CycleState.LEG_1_FILLED,
    CycleState.LEG_2_SUBMITTED, CycleState.BOTH_FILLED, CycleState.COMPLETED,
]


def test_graph_has_no_structural_problems() -> None:
    """Every non-terminal state can reach a terminal one, and all are reachable."""
    assert validate_graph() == []


def test_every_state_is_reachable_from_created() -> None:
    assert reachable_states(CycleState.CREATED) == set(CycleState)


def test_terminal_states_have_no_exits() -> None:
    assert allowed_targets(CycleState.COMPLETED) == frozenset()
    assert allowed_targets(CycleState.FAILED) == frozenset()


def test_failure_is_reachable_from_every_non_terminal_state() -> None:
    for state in CycleState:
        if state.is_terminal:
            continue
        assert can_transition(state, CycleState.FAILED)
        assert can_transition(state, CycleState.EMERGENCY)


def test_happy_path_walks_cleanly() -> None:
    cycle = HedgeCycle(pair_name="p")
    for state in HAPPY_PATH:
        cycle.transition(state, "step")
    assert cycle.state is CycleState.COMPLETED
    assert cycle.is_complete
    assert len(cycle.events) == len(HAPPY_PATH)
    assert [e.sequence for e in cycle.events] == list(range(1, len(HAPPY_PATH) + 1))


def test_states_cannot_be_skipped() -> None:
    cycle = HedgeCycle()
    with pytest.raises(IllegalTransition):
        cycle.transition(CycleState.BOTH_FILLED, "skip")
    assert cycle.state is CycleState.CREATED   # unchanged


def test_illegal_transition_leaves_no_event() -> None:
    cycle = HedgeCycle()
    with pytest.raises(IllegalTransition):
        cycle.transition(CycleState.LEG_2_SUBMITTED, "skip")
    assert cycle.events == []


def test_partial_fills_can_repeat() -> None:
    cycle = HedgeCycle()
    for state in HAPPY_PATH[:5]:
        cycle.transition(state, "step")
    cycle.transition(CycleState.LEG_1_PARTIAL, "partial")
    cycle.transition(CycleState.LEG_1_PARTIAL, "another partial")
    cycle.transition(CycleState.LEG_1_FILLED, "done")
    assert cycle.state is CycleState.LEG_1_FILLED


def test_failure_before_exposure_ends_as_failed() -> None:
    cycle = HedgeCycle()
    cycle.transition(CycleState.VALIDATED, "v")
    cycle.fail("bad configuration")
    assert cycle.state is CycleState.FAILED
    assert cycle.error == "bad configuration"


def test_failure_after_leg_one_requires_recovery() -> None:
    """The critical routing rule: exposure on the book is never just 'failed'."""
    cycle = HedgeCycle()
    for state in HAPPY_PATH[:6]:
        cycle.transition(state, "step")
    assert cycle.state is CycleState.LEG_1_FILLED
    cycle.fail("leg 2 rejected")
    assert cycle.state is CycleState.RECOVERY_REQUIRED
    assert cycle.needs_attention


@pytest.mark.parametrize("state", [s for s in CycleState if s.has_exposure])
def test_exposed_states_route_failure_to_recovery(state: CycleState) -> None:
    assert state.has_exposure
    assert CycleState.RECOVERY_REQUIRED in allowed_targets(state) or state is CycleState.RECOVERY_REQUIRED


def test_recovery_can_complete() -> None:
    cycle = HedgeCycle()
    for state in HAPPY_PATH[:6]:
        cycle.transition(state, "step")
    cycle.fail("leg 2 rejected")
    cycle.transition(CycleState.REBALANCING, "recovery")
    cycle.transition(CycleState.COMPLETED, "recovered")
    assert cycle.state is CycleState.COMPLETED


def test_rebalancing_loop_is_allowed() -> None:
    cycle = HedgeCycle()
    for state in HAPPY_PATH[:8]:
        cycle.transition(state, "step")
    cycle.transition(CycleState.REBALANCING, "rebalance")
    cycle.transition(CycleState.BOTH_FILLED, "back")
    cycle.transition(CycleState.REBALANCING, "again")
    cycle.transition(CycleState.COMPLETED, "done")
    assert cycle.state is CycleState.COMPLETED


def test_escalation_records_the_reason() -> None:
    cycle = HedgeCycle()
    cycle.transition(CycleState.VALIDATED, "v")
    event = cycle.escalate("margin breach")
    assert cycle.state is CycleState.EMERGENCY
    assert event.payload["reason"] == "margin breach"


def test_leg_states_are_derived_from_fills() -> None:
    from decimal import Decimal

    cycle = HedgeCycle(
        source_target_quantity=Decimal("100"), hedge_target_quantity=Decimal("-5"),
    )
    assert cycle.leg_state(Leg.SOURCE) == "PENDING"
    cycle.source_filled_quantity = Decimal("40")
    assert cycle.leg_state(Leg.SOURCE) == "PARTIAL"
    cycle.source_filled_quantity = Decimal("100")
    assert cycle.leg_state(Leg.SOURCE) == "FILLED"
    cycle.hedge_filled_quantity = Decimal("-5")
    assert cycle.leg_state(Leg.HEDGE) == "FILLED"


def test_leg_imbalance_reports_the_shortfall() -> None:
    from decimal import Decimal

    cycle = HedgeCycle(hedge_target_quantity=Decimal("-5"),
                       hedge_filled_quantity=Decimal("-3"))
    assert cycle.leg_imbalance == Decimal("-2")


def test_events_carry_their_payload() -> None:
    cycle = HedgeCycle()
    event = cycle.transition(CycleState.VALIDATED, "validated", {"source": "X", "hedge": "Y"})
    assert event.payload == {"source": "X", "hedge": "Y"}
    assert event.from_state is CycleState.CREATED
    assert event.to_state is CycleState.VALIDATED


def test_cycle_serialises_with_leg_detail() -> None:
    cycle = HedgeCycle(pair_name="p")
    cycle.transition(CycleState.VALIDATED, "v")
    payload = cycle.to_dict()
    for key in ("cycle_id", "state", "source_leg_state", "hedge_leg_state",
                "leg_imbalance", "event_count"):
        assert key in payload


def test_graph_description_covers_every_state() -> None:
    described = {name for name, _ in describe_graph()}
    assert described == {s.value for s in CycleState}
