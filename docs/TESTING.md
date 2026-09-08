# Testing

```bash
cd backend
.venv/bin/python -m pytest tests/ -q          # 402 tests, ~57s
.venv/bin/python -m pytest --cov=src/hedgelab --cov-report=term-missing
.venv/bin/hedgelab demo                       # the 25-step acceptance run
.venv/bin/hedgelab scenario run-all           # the ten demo scenarios
```

The whole suite runs on SQLite with no Docker and no PostgreSQL. That is a
consequence of the layering: `domain/`, `hedge/`, `risk/` and `costs/` are pure
functions over their arguments, so there is no I/O to mock.

## What is covered

| Area | Tests | Approach |
|---|---|---|
| Quantity conversion | 37 | Table-driven over many size × step × unit combinations, including inverse and non-decimal steps |
| Instruments & registry | 26 | Validation rules, trading sessions, the generic-instrument guarantee |
| Hedge calculator | 35 | All nine objectives, the inverse divergence, cost decomposition, auditability |
| Optimiser | 20 | Every priority, weight sensitivity, brute-force cross-check |
| Risk & margin | 30 | Closed-form liquidation, each threshold boundary, portfolio aggregation |
| State machine | 28 | Legal/illegal matrix, reachability, failure routing |
| Paper venues | 47 | Order lifecycle, TIF semantics, margin, funding, faults, stop-out, live-mode refusal |
| Market data & costs | 49 | Determinism, scenarios, FX triangulation, P&L attribution |
| Statistics | 30 | Volatility recovery, feed-rate invariance, correlation pairing, beta uncertainty |
| API | 45 | Real app over an in-process ASGI transport |
| WebSocket & scenarios | 24 | Streaming, subscription, backpressure; all ten scenarios |
| Workflow & recovery | 25 | Full pipeline, every failure mode, restart reconstruction, estimated statistics |
| Acceptance run | 3 | The 25-step demo, twice, and from a deliberately dirty state |

## Tests that assert something specific

Rather than listing all 402, these are the ones that encode the hard parts:

**`test_quantity_matching_is_not_the_same_as_exposure_matching`**
1000 contracts of 0.001 BTC hedged as "1000 lots" of 1 BTC is a 1000× over-hedge.
The test asserts the correct answer is 1 lot and that the naive one is 1000×
larger.

**`test_inverse_base_neutral_and_delta_neutral_diverge_when_in_profit`**
A $500k inverse position entered at 100,000 and marked at 125,000 needs 4 lots
to be base-neutral and 5 to be delta-neutral. Both numbers are asserted.

**`test_adaptive_objectives_ignore_the_source_leg_funding`**
Two source instruments with funding rates 500× apart must produce the *same*
optimal hedge ratio, because source funding is sunk.

**`test_rounding_never_promotes_up_to_the_minimum`**
A required 0.4 lots against a 1-lot minimum rounds to zero and is flagged —
never to 1, which would execute more than was asked for.

**`test_closing_orders_are_never_blocked_by_margin`**
An account with almost no free margin can still close. If de-risking could be
blocked by margin, a breached account could never be recovered.

**`test_leg_one_partial_fill_resizes_the_hedge`**
Leg 1 fills 40% → the hedge is 2 lots, not the 5 originally calculated.

**`test_timeout_does_not_double_hedge`**
After a timeout the final position is exactly one hedge of the right size.

**`test_duplicate_execution_reports_do_not_double_count`** and
**`test_duplicate_fills_are_rejected_by_the_database`**
Both layers of protection, in memory and at the unique constraint.

**`test_restart_with_an_unfinished_cycle_requires_attention`**
A cycle frozen in `LEG_1_FILLED` blocks automatic resumption after a restart.

**`test_step_search_matches_brute_force_on_a_small_grid`**
The optimiser's search is cross-checked against exhaustive evaluation.

**`test_estimate_is_independent_of_the_feed_rate`**
The estimate must recover the same volatility whether the feed ticks every 300s
or every 7200s. Scaling by the configured interval instead of the observed one
was a silent 3.4× error.

**`test_carry_penalty_scales_with_hedge_variance_not_residual_variance`**
With correlation near 1 the residual vol is a hundredth of the hedge vol — the
case that exposed the wrong denominator. A realistic swap must trim the ratio
slightly, not annihilate it.

**`test_literal_risk_routes_are_not_shadowed`**
FastAPI matches routes in registration order, so a parameterised path declared
before its literal siblings swallows them. `/api/risk/statistics` resolved to
`/api/risk/{mapping_name}` and 404'd.

**`test_beta_is_not_called_significant_on_thin_data`**
A 1% deviation from parity on 200 samples is noise. Reporting a point estimate
without its uncertainty invites reading that as signal — which for a hedge
ratio means trading it.

**`test_graph_has_no_structural_problems`**
Every non-terminal state reaches a terminal one; every state is reachable.

**`test_components_sum_to_the_net`**
P&L attribution adds up, to within 1e-9.

**`test_registering_a_brand_new_instrument_needs_no_code_change`** and
**`test_a_new_instrument_can_be_added_through_the_api`**
A wheat future in EUR and a silver CFD the code has never seen are registered
and priced with no source change.

**`test_only_paper_adapters_are_registered`**, **`test_factory_refuses_live_mode`**,
**`test_factory_refuses_live_even_with_allow_live_set`**,
**`test_live_adapters_cannot_be_constructed`**
The paper-only guarantee, asserted four ways.

## Bugs these tests actually caught

Not hypothetical — each of these was a real defect found during development:

1. **`Subscriber` was an unhashable dataclass**, so the WebSocket hub raised on
   every connection. Found by the first WS test.
2. **A log call with `extra={"name": ...}`** collided with a reserved
   `LogRecord` attribute and raised `KeyError` — but only once the level was low
   enough for the call to be evaluated, so it hid in development. Found when
   another test happened to configure logging at INFO. Fixed at the call site
   *and* by making the logger adapter rename colliding keys, because a log
   statement must never be able to take down a trading engine.
3. **The risk gate compared a stale source position** against the projected
   hedge, reporting a 40% residual that did not exist and blocking the trade.
   Found by the acceptance run.
4. **The kill switch could be defeated by a partial fill** — it reported
   success while leaving −2.5 lots open. Found by the acceptance run.
5. **IOC orders were left `PARTIALLY_FILLED` forever**, stranding terminal
   orders in the open set and producing a permanent `ORDER_MISMATCH` after
   every restart. Found by the acceptance run's reconciliation step.
6. **FOK was checked against the fault-capped quantity** rather than the
   requested one, letting a partial fill satisfy an FOK order.
7. **Concentration warned at 45%** when a two-asset portfolio has a 50% floor,
   so every balanced two-asset book was permanently in breach.
8. **Two mappings shared a hedge instrument**, double-counting the same broker
   position in portfolio exposure (reported net exposure of −615,208 on a flat
   book). Fixed by adding a mini contract and a registry invariant.
9. **The kill switch raised on an unreachable venue** — the one situation where
   it most needs to do whatever it can. It now flattens every reachable venue
   and reports the rest as unconfirmed.
10. **The acceptance run was not repeatable**: after a scenario sweep it
    dropped from 25/25 to 18/25 because it inherited positions, armed faults
    and an engaged kill switch. It now resets to a known state first, and a
    test deliberately dirties the platform before running it.

## The acceptance run

`hedgelab demo` runs the 25-step end-to-end demonstration and exits non-zero if
any step fails. Each step asserts an observable outcome; nothing is printed
that the system did not produce. Currently **25/25**.

It is the highest-value test in the repository — four of the eight bugs above
were found by it and by nothing else, because it is the only thing that runs
the whole system in sequence with state carried between steps.

## What is not tested

Stated plainly rather than implied:

* **No live adapter tests**, because there is no live adapter.
* **No load or latency testing.** The paper engine's timings are simulated
  sleeps; they say nothing about throughput under real load.
* **No property-based testing.** Hypothesis over instrument specifications and
  price paths would likely find rounding edge cases the table-driven tests
  miss.
* **No browser tests.** The frontend is typechecked (`tsc --noEmit`) and builds,
  and its endpoints are covered server-side, but no Playwright suite drives the
  UI.
* **No multi-process concurrency tests**, because the system is single-process
  by design.
* **PostgreSQL is verified by migration and by running the demo against it**,
  but the test suite itself runs on SQLite.
