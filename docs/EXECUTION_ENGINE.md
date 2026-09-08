# The execution engine

## The state machine

Sixteen states. Transitions are declared as data in
`execution/state_machine.py` and validated on every move — an illegal
transition raises `IllegalTransition` and is logged; it is never silently
applied.

```
CREATED
  └─> VALIDATED
        └─> AWAITING_MARKET_DATA
              └─> CALCULATED
                    └─> RISK_APPROVED
                          └─> LEG_1_SUBMITTED
                                ├─> LEG_1_PARTIAL ──┐
                                └─> LEG_1_FILLED <──┘
                                      └─> LEG_2_SUBMITTED
                                            ├─> LEG_2_PARTIAL ──┐
                                            └─> BOTH_FILLED <───┘
                                                  ├─> COMPLETED
                                                  ├─> REBALANCING ──> COMPLETED
                                                  └─> RISK_REDUCTION ──> COMPLETED

  any non-terminal state ──> FAILED             (no exposure was taken)
                        ──> RECOVERY_REQUIRED   (leg 1 is on the book)
                        ──> EMERGENCY           (threshold breach / kill switch)
```

Two structural properties are asserted by `validate_graph()`, which the API
exposes at `/api/hedge/state-machine` and a test runs on every build:

1. Every non-terminal state can reach a terminal one — no cycle can get stuck
   forever holding exposure.
2. Every state is reachable from `CREATED` — no dead code in the graph.

## Failure routing

`CycleState.has_exposure` decides where a failure goes:

```python
target = CycleState.RECOVERY_REQUIRED if self.state.has_exposure else CycleState.FAILED
```

The *same* rejection is a non-event before leg 1 and an incident after it. A
cycle that traded and then failed must never be marked `FAILED` — that would
report an unhedged position as finished.

| Failure point | Outcome |
|---|---|
| Venue disconnected before leg 1 | `FAILED` — nothing traded |
| Stale data or a spread past the limit | `FAILED` — refused before trading |
| Risk check rejects | `FAILED` — refused before trading |
| Leg 2 rejected | `RECOVERY_REQUIRED` → rebalance → `COMPLETED` |
| Leg 2 times out | `RECOVERY_REQUIRED` → resolve → rebalance → `COMPLETED` |
| Recovery itself fails | `EMERGENCY` |

## Write-ahead transitions

Every transition is persisted **before** the side effect it describes is
attempted:

```python
await self._transition(cycle, CycleState.LEG_2_SUBMITTED, "leg_2_submitted", {...})
order = await self._place(hedge_venue, order_request, cycle, Leg.HEDGE)
```

So a process that dies between those two lines leaves a durable record saying
"leg 2 was submitted, outcome unknown". Restart recovery reads that and refuses
to resume automatically. Recording the transition *after* the call would leave
no trace at all, and the platform would come back believing it had never
traded.

## Leg 1 is never skipped

When the source position already exists, `source_quantity` is zero and no trade
is needed. The cycle still records `LEG_1_SUBMITTED` and `LEG_1_FILLED`, with
the payload `{"note": "source position already exists; leg 1 is a confirmation
step"}`. A gap in the audit trail where a leg should be is worse than an
explicit no-op, because it is indistinguishable from a bug.

## The hedge is recalculated from what actually filled

This is the property that makes partial fills safe:

```python
actual_source = await self._position(source_venue, source_spec)
if actual_source.quantity != projected_source_qty:
    # Re-price and re-solve from the real fill, not the pre-trade estimate.
    calculation = await self._calculate(..., actual_source.quantity, ...)
```

Leg 1 asks for 5000 contracts and fills 3000 → the hedge becomes 3 lots, not
the 5 originally calculated. Using the pre-trade number would over-hedge by
67%, turning a partial fill into a directional position in the opposite
direction. Tested in `test_leg_one_partial_fill_resizes_the_hedge`.

## Timeouts are resolved, not retried

```python
except VenueTimeout as exc:
    resolved = await self._resolve_timeout(venue, request)   # match client_order_id
    if resolved is not None:
        return resolved
    raise ExecutionError(
        "... timed out and no matching order was found on the venue; "
        "not retrying to avoid a duplicate position"
    )
```

The client order id is the idempotency key, and it carries a `UNIQUE`
constraint in the `orders` table so a retry that did slip through cannot create
a second row. `test_timeout_does_not_double_hedge` asserts the final position
is exactly one hedge, never two.

## Time in force is honoured

The default is IOC. An IOC order that fills partially is **terminal**: the
remainder is cancelled and the status becomes `CANCELLED` with
`filled_quantity > 0` — which is what exchanges report.

This is not cosmetic. Leaving such an order as `PARTIALLY_FILLED` strands a
finished order in the open-order set forever, and reconciliation then reports a
permanent `ORDER_MISMATCH` after every restart. That bug was found by the
acceptance run's reconciliation step, not by a unit test.

FOK is measured against what the client asked for, not against what the venue
was able to offer — comparing against the reduced amount would let a partial
fill satisfy an FOK order, which is the one thing FOK exists to prevent.

## Rebalancing: assess, then act

`Rebalancer.assess()` is read-only and safe to call on a timer.
`Rebalancer.execute()` trades. They are separate methods because the monitoring
loop calls `assess` constantly and must never trade as a side effect of
looking.

A rebalance is only recommended when *all* of the following hold:

* the residual exceeds the pair's tolerance in bps of source notional,
* the adjustment rounds to a non-zero quantity on the venue's lattice,
* at least one leg is not flat.

When the residual is out of tolerance but the adjustment is below the venue
minimum, the assessment says so explicitly rather than silently recommending
nothing — the residual is real, it simply cannot be traded away.

## Flattening actually reaches flat

The kill switch closes the **hedge leg first**: closing the source first would
leave the hedge naked and directional for as long as the second order takes.

Each leg is retried up to five times, because a partial fill does not end the
attempt — a kill switch that leaves half a position open because one order
filled 50% has not killed anything. If the budget is exhausted the remaining
positions are named in the result and logged as an error; the operation reports
failure rather than success.

Found by the acceptance run: an armed partial-fill fault from an earlier step
fired during the flatten and left −2.5 lots open, while the kill switch
reported success.

An unreachable venue does **not** abort the operation. It is exactly when the
kill switch matters most, so every reachable venue is flattened and the
unreachable ones are named in the result with their exposure marked
unconfirmed. The operation reports failure — it did not achieve a flat book —
but it did as much as it could. Found by
`test_acceptance_run_survives_a_dirty_starting_state`, where the kill switch
previously raised on a disconnected broker.

## Every cycle is reconstructible

`GET /api/hedge/cycles/{id}` returns every transition with its payload,
including the full calculation derivation stored in the `CALCULATED`
transition. The dashboard's Hedge Cycles page renders it as a timeline. A
cycle whose decisions cannot be reconstructed after the fact is not auditable,
and an unauditable execution engine is not usable in production.
