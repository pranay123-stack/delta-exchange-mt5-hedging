"""The 25-step acceptance run, exercised as a test.

It is the highest-value check in the repository: several bugs were found by it
and by nothing else, because it is the only thing that runs the whole system in
sequence with state carried between steps.
"""

from __future__ import annotations

from decimal import Decimal

from hedgelab.domain.enums import FaultKind
from hedgelab.scenarios.acceptance import run_acceptance
from hedgelab.scenarios.runner import ScenarioRunner
from hedgelab.service import HedgeLabService

D = Decimal


async def test_acceptance_run_passes_every_step(service: HedgeLabService) -> None:
    assert await run_acceptance(service) is True


async def test_acceptance_run_is_repeatable(service: HedgeLabService) -> None:
    """Running it twice in a row must give the same answer.

    The script asserts absolute outcomes, so it has to reset rather than
    inherit whatever the previous run left on the book.
    """
    assert await run_acceptance(service) is True
    assert await run_acceptance(service) is True


async def test_acceptance_run_survives_a_dirty_starting_state(
    service: HedgeLabService,
) -> None:
    """A demo whose result depends on what ran before it is not a check.

    This was a real failure: running the scenario sweep first left positions,
    armed faults and an engaged kill switch behind, and the acceptance run
    dropped to 18/25.
    """
    # Leave the platform in the worst state we can arrange.
    await ScenarioRunner(service).run("D")
    await service.open_source_position("PAPER_DELTA:SOLUSDT-PERP", D("700"))
    await service.inject_fault(FaultKind.LEG2_PARTIAL_FILL, leg="HEDGE", magnitude=D("0.5"))
    await service.inject_fault(FaultKind.MT5_DISCONNECT)
    await service.engage_kill_switch("deliberately dirty state")

    assert service.state.kill_switch_engaged is True

    assert await run_acceptance(service) is True
