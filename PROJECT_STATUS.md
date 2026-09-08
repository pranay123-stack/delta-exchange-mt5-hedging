# PROJECT STATUS

**Complete.** The end-to-end paper workflow works, and everything below was
produced by running the system rather than asserted.

```
 402 tests passing                    (unit + integration, SQLite, no Docker needed)
  25/25 acceptance steps passing      hedgelab demo
  10/10 demo scenarios passing        hedgelab scenario run-all
  83% line coverage overall           93–100% on the calculation core
 ruff: all checks passed
 mypy: strict, 73 source files, no issues
 tsc:  no issues; frontend builds (268 kB / 82 kB gzipped)
 21 tables migrated on PostgreSQL 16, clean downgrade/upgrade round-trip
 4/4 Docker services healthy
```

---

## 1. Implemented

### The generic instrument model
Forty-field `InstrumentSpec` covering venue, assets, quantity unit, contract
size and multiplier, linear/inverse settlement, tick and quantity lattice,
precision, leverage, margin model and rates, maker/taker fees, spread, funding
model and interval, swap points and triple day, trading sessions, direction
permissions, price source and FX path. Loaded from YAML **and** editable at
runtime through the API.

The engine contains no hardcoded symbol. Two tests prove it by registering
instruments the code has never seen — a wheat future quoted in EUR at 5000
units per lot, and a silver CFD added over HTTP — and pricing and hedging them
with no source change.

Ten instruments ship across two venues, deliberately covering: linear perps
(0.001/0.01/1 base units per contract), an **inverse** coin-margined perp, CFDs
at 1/10/100/0.1 base units per lot, a cross-named underlying (PAXG perp hedged
with XAUUSD), and gold's weekend session.

### Quantity conversion
Every conversion routes through base units. Five configured pairs produce four
distinct contract-to-lot relationships. Inverse contracts are handled properly:
notional is price-independent, base units move with the mark, and quote delta
depends on the **entry** price — a distinction worth 25% of hedge size on a
position in profit.

### Nine hedge objectives
Base-asset-, notional-, quote-P&L- and account-currency-P&L-neutral; custom
ratio; partial; funding-adjusted and cost-adjusted (mean-variance,
`h* = β − k/(λσh²)`, using the *marginal* cost of hedging); risk-weighted
(beta). Every result carries its derivation steps with formula and value.

### Estimated statistics
Volatility, correlation and beta are measured from observed prices rather than
assumed — recovering the simulator's configured volatilities within 4% across a
6.6× range, and invariant to the feed rate across a 24× range of tick
intervals. β is reported with its standard error and flagged when its distance
from 1.0 is within sampling noise, so nobody trades on a difference that is not
there. The objectives fall back to configured assumptions, with an explicit
warning, until there is enough data.

### Optimiser
Exact, step-search over seven named priorities, and weighted scoring with
range-normalised metrics. Cross-checked against brute force. On a real example
it finds a lattice point with 40% less residual than naive rounding.

### Paper execution
Two paper adapters over a shared matching engine that walks synthetic depth, so
slippage emerges from size versus liquidity. Models commission versus
spread-paid pricing, IOC/FOK/GTC semantics, trading sessions, margin checks
that never block de-risking, funding and swap accrual, and venue stop-out.

### Two-leg execution
Sixteen states, declared transitions, structural validation, write-ahead
persistence of every transition with its payload. The hedge is recalculated
from what leg 1 **actually** filled. Timeouts are resolved against venue state,
never blindly retried.

### Risk
Per-pair and portfolio, four-level ladder with prescribed actions. Closed-form
margin and liquidation for linear and inverse instruments. Portfolio
aggregation across venue, underlying and settlement currency. Kill switch with
confirmation, verification and full audit.

### Costs
Funding, swap, fees, spread, slippage and FX attributed separately, asserted to
sum to the net. Forward projection with annualised carry.

### Reconciliation and recovery
Three-way comparison with six issue classes and a suggested action for each.
Restart recovery that reconstructs state and refuses to resume when anything is
ambiguous. Paper venues rehydrate from the database, so restart recovery tests
something real.

### Fault injection
Fourteen faults, armed with scope and count for reproducibility. Ten scenarios
that drive the real engine.

### Infrastructure
FastAPI with 56 routes and OpenAPI; WebSocket with twelve topics and bounded
per-client queues; PostgreSQL with 21 tables and Alembic; React + TypeScript
dashboard with eight pages; Docker Compose with four healthchecked services;
structured JSON logging with correlation IDs; role-based access with
confirmation on irreversible actions.

---

## 2. Test results

```
tests/unit/test_quantity_conversion.py      37 passed
tests/unit/test_instruments.py              26 passed
tests/unit/test_hedge_calculator.py         35 passed
tests/unit/test_optimizer.py                20 passed
tests/unit/test_risk.py                     30 passed
tests/unit/test_state_machine.py            28 passed
tests/unit/test_paper_venues.py             47 passed
tests/unit/test_marketdata_and_costs.py     49 passed
tests/unit/test_statistics.py               30 passed
tests/integration/test_api.py               45 passed
tests/integration/test_workflow.py          25 passed
tests/integration/test_websocket_and_scenarios.py  24 passed
tests/integration/test_acceptance.py         3 passed
                                           ---
                                           402 passed in ~57s
```

Coverage on the modules that matter:

| Module | Coverage |
|---|---|
| `costs/funding.py`, `marketdata/stats.py` | 100% |
| `venues/factory.py` | 100% |
| `costs/pnl.py` | 99% |
| `hedge/objectives.py` | 98% |
| `risk/engine.py`, `risk/portfolio.py`, `execution/state_machine.py` | 97% |
| `hedge/calculator.py` | 96% |
| `risk/margin.py` | 95% |
| `hedge/optimizer.py`, `marketdata/simulator.py`, `venues/paper_engine.py` | 93–94% |
| **Overall** | **83%** |

### Ten bugs the tests and the acceptance run actually found

Listed because they are the reason to trust the rest:

1. `Subscriber` was an unhashable dataclass — the WebSocket hub raised on every
   connection.
2. A log call with `extra={"name": ...}` collided with a reserved `LogRecord`
   field and raised at INFO level only, so it hid in development. Fixed at the
   call site *and* by making the logger adapter rename colliding keys.
3. The risk gate compared a **stale source position** against the projected
   hedge, inventing a 40% residual and blocking the trade.
4. The kill switch could be **defeated by a partial fill** while reporting
   success.
5. The kill switch **raised** on an unreachable venue — the case where it
   matters most.
6. IOC orders were left `PARTIALLY_FILLED` forever, producing a permanent
   `ORDER_MISMATCH` after every restart.
7. FOK was checked against the fault-capped quantity, letting a partial fill
   satisfy a fill-or-kill order.
8. Concentration warned at 45% when a two-asset portfolio has a 50% floor.
9. Two mappings shared a hedge instrument, double-counting the same broker
   position (−615,208 of "net exposure" on a flat book).
10. The acceptance run was not repeatable — 25/25 on a clean database, 18/25
    after a scenario sweep.
11. **The mean-variance objectives divided by the wrong variance.** The carry
    penalty was scaled by the *residual* variance instead of the hedge leg's,
    making it ~300× too large and returning a hedge ratio of **zero** for a pair
    whose hedge removes 98.8% of drawdown. Invisible against the shipped
    assumptions (it produced a plausible-looking 0.92); unmissable the moment
    correlation was measured at 0.9998 rather than assumed at 0.995.
12. **The volatility estimator scaled by the configured sampling interval**
    rather than the observed one, overstating volatility by
    `sqrt(actual/configured)` — a silent 3.4× error whenever the feed ticked
    more slowly than configured.
13. **Simulated time was welded to real time**, so an 8-hour funding interval
    took 8 real hours and the estimator needed 2.5 real hours of data before it
    could say anything. Decoupled: the loop cadence is now a presentation
    setting and the market runs 120× real time by default.
14. **A parameterised route shadowed its literal siblings.** FastAPI matches in
    registration order, and `/risk/{mapping_name}` was declared before
    `/risk/statistics`, so the new endpoint returned
    `404 unknown hedge mapping named 'statistics'`. Found by calling it.

Four of these were found only by the 25-step acceptance run, and three more
only by feeding the objectives real estimates instead of assumptions.

### Backtest

500 steps at one hour per step (≈21 simulated days) on the BTC pair:

| Hedge ratio | Price drawdown removed | Net P&L | of which carry |
|---|---|---|---|
| 1.00 | 98.8% | −5,413 | −4,814 |
| 0.50 | 49.7% | −12,746 | −3,884 |
| ~0.00 | 1.0% | −19,933 | −2,973 |

Risk reduction tracks the hedge ratio linearly — the correctness check. The
hedge **costs money**; that is what a hedge does, and the platform says so.

---

## 3. Known limitations

Stated plainly. None of these are hidden behind a passing test.

### By design

* **No live trading.** Five structural barriers, documented in
  [PAPER_TRADING.md](docs/PAPER_TRADING.md).
* **Single process.** One process owns the book. Two would need leader election
  and a per-pair lock, because two coordinators would both see the same
  residual and both trade it.
* **No automatic trading loop.** The background task advances the market and
  streams prices; it does not trade. Rebalancing is operator- or scenario-
  driven so the demo stays deterministic.
* **Redis is optional.** It is in the stack and healthy, but the event bus
  currently runs in-process; Redis would only be load-bearing with more than
  one process.

### Simplifications

* **Aggregated depth, no queue position.** Fine for a hedger crossing the
  spread; wrong for a market maker.
* **Latency is simulated `asyncio.sleep`.** It demonstrates ordering, not
  throughput.
* **Statistics are estimated, but only from simulated prices.** Volatility and
  correlation now come from a rolling estimator over observed prices
  (`marketdata/stats.py`), and the objectives fall back to configured
  assumptions — with a warning — until there is enough data. Those prices are
  the simulator's, so the estimator is validated against a known answer rather
  than against a real market. `risk_aversion` and `horizon_days` remain policy
  inputs and always will be.
* **Worst-case P&L is a normal-quantile estimate**, not a fat-tailed one. It
  will understate a real gap.
* **The FX graph triangulates one hop** through a pivot. Enough for
  USDT→USD→INR; not a general cross-currency engine.
* **Instrument specifications are illustrative**, modelled on the shape of real
  contract specs. They are configuration, and the YAML says so.

### Not built

* **No data retention or partitioning.** `market_data` grows fastest; the
  audit tables are the ones to keep and partition rather than prune.
* **No property-based testing.** Hypothesis over specs and price paths would
  likely find rounding edges the table-driven tests miss.
* **No browser tests.** The frontend typechecks and builds, and its endpoints
  are covered server-side, but nothing drives the UI.
* **No load testing.**
* **`hedge_configs` is persisted but not yet the source of truth** for
  per-pair optimiser settings; those come from the request or the mapping. The
  table and API exist; wiring them as the default is unfinished.
* **`SPOT_EXCHANGE` and `FX` instrument types are modelled but unused.**

---

## 4. Demo instructions

```bash
cp .env.example .env
docker compose up --build
```

Dashboard <http://localhost:5174> · API docs <http://localhost:8001/docs>

```bash
docker compose exec backend hedgelab demo             # 25 steps, ~10s
docker compose exec backend hedgelab scenario run-all # 10 scenarios
docker compose exec backend hedgelab instruments pairs
```

[DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) has a ten-minute guided tour. The three
things worth showing:

1. **Configuration → sizing column.** Six different definitions of "one unit".
2. **Hedge Calculator with the inverse perp.** `BASE_ASSET_NEUTRAL` says 4
   lots, `QUOTE_PNL_NEUTRAL` says 5, on the same position.
3. **Fault Injection.** Arm a leg-2 rejection, hedge, and watch the cycle go
   `RECOVERY_REQUIRED → REBALANCING → COMPLETED`.

---

## 5. Live-adapter extension points

The shape is deliberate even though live is not implemented.

**Where the code goes.** `venues/live_stubs.py` already defines `DeltaAdapter`
and `MT5Adapter` against the `TradingVenue` interface — nine methods. Replacing
the raising bodies with an HTTP/WebSocket client (Delta) or a MetaTrader5
bridge (MT5) is the whole integration; nothing above `venues/` changes, because
nothing above it knows what a venue is.

**What must change to reach live**, in order:

1. Implement the adapter methods.
2. Add credential settings and load them from the environment. There are none
   today.
3. Register the class in `LIVE_ADAPTERS` in `venues/factory.py` — currently an
   empty dict.
4. Relax the factory's mode guard behind `allow_live`.
5. Remove the entrypoint's refusal to start outside `PAPER`.

Steps 3–5 are the point of no return, and all three are one-line, obvious edits
a reviewer cannot miss. That is intentional.

**What a real deployment would need that this does not have**, and which should
not be improvised later:

* Rate limiting and backoff per venue.
* Per-order and per-day notional caps enforced *below* the strategy layer.
* A mandatory dry-run mode comparing intended against simulated fills.
* Reconciliation against the venue's own end-of-day statements, not just its
  live API.
* Clock-skew detection between venues.
* An independent kill switch that does not share a process with the strategy.
* Real credential management, and separate keys per environment.

**What carries over unchanged**: the instrument model, quantity conversion, all
nine objectives, the optimiser, the risk and cost engines, the state machine,
reconciliation and the entire audit trail. That is the point of keeping
`domain/`, `hedge/`, `risk/` and `costs/` free of I/O — they are already the
parts that would matter most with real money, and they are already the
best-tested parts of the system.
