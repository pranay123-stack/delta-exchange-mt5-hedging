# PROJECT PLAN — Cross-Platform Perpetual ↔ MT5 Paper Hedging Platform

> **Paper trading only.** No live credentials, no live order path. See §7.

## 0. Repository analysis (pre-existing state)

The target directory was **empty** — no git repository, no source files, no build
configuration. There is no existing architecture, language, or framework to preserve,
so nothing is being rewritten. The stack below is chosen fresh against the brief.

Verified local toolchain: Python 3.12.3, Node 20.20, Docker 29.2 + Compose v5.0.2,
PostgreSQL client 16, redis-server. Ports 5468 (postgres) and 6389 (redis) are used to
avoid colliding with the user's other project containers.

---

## 1. Problem statement

Hold a position in a **perpetual future** on a crypto exchange (Delta-Exchange-style) and
hold the **economically offsetting** position at an **MT5 broker** (Exness-style CFD).
The two venues disagree on nearly everything that matters:

| | Perp venue | MT5 broker |
|---|---|---|
| Quantity unit | contracts | lots |
| Size of one unit | `contract_size × multiplier` base units | `units_per_lot` base units |
| Linear / inverse | either | linear |
| Financing | funding rate every N hours | swap points per night, triple on Wed |
| Margin | initial/maintenance rate on notional | margin level %, stop-out % |
| Quote currency | USDT | USD |
| Settlement | USDT / base (inverse) | account currency (USD/INR/EUR) |

So `1 contract ≠ 1 lot` almost always. The platform's job is to compute the **economically
correct** hedge quantity, execute both legs as a supervised two-leg cycle, and keep the
book flat under partial fills, rejections, disconnects and restarts.

---

## 2. Architecture

```
                       ┌──────────────────────────────────────────┐
   React + TS UI ──────┤ FastAPI  (REST + WebSocket)              │
   (Vite, LWCharts)    │  routers/  ws.py  deps.py                │
                       └────────────────┬─────────────────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────────┐
   │                            APPLICATION SERVICES                         │
   │  HedgeService · RiskService · ReconciliationService · ScenarioRunner     │
   └───┬─────────┬──────────┬───────────┬────────────┬───────────┬───────────┘
       │         │          │           │            │           │
  ┌────▼───┐ ┌───▼────┐ ┌───▼─────┐ ┌───▼──────┐ ┌───▼─────┐ ┌───▼────────┐
  │ hedge/ │ │ risk/  │ │ costs/  │ │execution/│ │recon-   │ │ faults/    │
  │ calc   │ │ margin │ │ funding │ │ state    │ │ ciliation│ │ injector  │
  │ optim  │ │ liq    │ │ fees    │ │ machine  │ │          │ │           │
  │ object.│ │ portf. │ │ pnl     │ │ coord.   │ │          │ │           │
  └────┬───┘ └───┬────┘ └───┬─────┘ └───┬──────┘ └───┬─────┘ └───┬────────┘
       │         │          │           │            │           │
  ┌────▼─────────▼──────────▼───────────▼────────────▼───────────▼────────┐
  │ DOMAIN:  InstrumentSpec · QuantityConverter · Exposure · Position ·    │
  │          Order · Fill · Money/Decimal  (no I/O, pure functions)        │
  └────┬──────────────────────────────┬───────────────────────────────────┘
       │                              │
  ┌────▼──────────────┐        ┌──────▼───────────────────────────────────┐
  │ venues/           │        │ marketdata/                              │
  │  TradingVenue ABC │        │  DeterministicSimulator (seeded)         │
  │  PaperDeltaAdapter│◄───────┤  ScenarioController                      │
  │  PaperMT5Adapter  │        │  MarketDataBus (in-proc + Redis fanout)  │
  │  live_stubs (NI)  │        └──────────────────────────────────────────┘
  └────┬──────────────┘
       │
  ┌────▼──────────────────────────────────────────────────────────────────┐
  │ db/  SQLAlchemy 2.0 async · PostgreSQL (asyncpg) / SQLite (tests)     │
  │      Alembic migrations · 21 tables · full audit trail                │
  └───────────────────────────────────────────────────────────────────────┘
```

**Dependency rule:** `domain/` imports nothing from the layers above it. `hedge/`, `risk/`,
`costs/` are pure calculators over domain objects — they take market data and account state
as arguments, never fetch it. Only application services touch the DB and the venues. This
is what makes the calculation core exhaustively unit-testable without a database.

### 2.1 Generic instrument model (the core principle)

Nothing in the engine knows what "BTC" or "XAUUSD" is. An instrument is a record:

```
venue, symbol, instrument_type, base_asset, quote_asset, settlement_asset,
quantity_unit {CONTRACT|LOT|BASE_UNIT}, contract_size, contract_multiplier,
units_per_contract, units_per_lot, is_inverse,
tick_size, tick_value, min_qty, max_qty, qty_step, price_precision, qty_precision,
max_leverage, margin_model, initial_margin_rate, maintenance_margin_rate,
maker_fee_bps, taker_fee_bps, fee_currency,
funding_model, funding_interval_hours, swap_long/short/triple_day,
trading_hours, allow_long, allow_short, price_source, fx_path
```

loaded from `instruments/specs/*.yaml` **and/or** the `instruments` table. Adding an
instrument = adding a YAML record or a DB row. Zero source changes. Enforced by a test that
loads a synthetic instrument the code has never seen and hedges it.

### 2.2 The exposure abstraction

Everything routes through one conversion so heterogeneous instruments become comparable:

```
position (qty in venue units)
   → base_units          = qty × units_per_quantity      (linear)
                         = qty × contract_size / price   (inverse)
   → notional_quote      = base_units × price            (linear)
                         = qty × contract_size           (inverse)
   → quote_delta (dPnL/dS, the hedgeable sensitivity)
                         = base_units                    (linear)
                         = notional_quote / entry_price  (inverse)
   → account_ccy_delta   = quote_delta × FX(quote → account_ccy)
```

The inverse case is not decoration: an inverse perp's quote-delta depends on **entry**
price, so quantity-matching it against a linear CFD leaves residual exposure. The engine
models this explicitly.

---

## 3. Modules

| Module | Responsibility |
|---|---|
| `domain/` | `InstrumentSpec`, `QuantityConverter`, `Exposure`, `Position`, `Order`, `Fill`, decimal rounding |
| `instruments/` | YAML + DB registry, instrument mappings (source ↔ hedge pairs) |
| `marketdata/` | seeded deterministic simulator, 7 scenarios, pub/sub bus |
| `venues/` | `TradingVenue` ABC, `PaperDeltaAdapter`, `PaperMT5Adapter`, shared paper matching engine, live stubs that raise |
| `hedge/` | 9 hedge objectives, `HedgeCalculator` (21-field result), `HedgeOptimizer` (exact / step-search / weighted) |
| `risk/` | margin models, liquidation price, per-pair risk, portfolio aggregation, 4 threshold levels + actions |
| `costs/` | funding accrual, swap/financing, fees, spread, slippage, FX conversion, gross/net/realized/unrealized P&L breakdown |
| `execution/` | 16-state machine, two-leg coordinator, rebalancer, recovery |
| `reconciliation/` | DB ↔ paper-Delta ↔ paper-MT5 three-way diff, restart reconstruction |
| `faults/` | 14 injectable faults with scope/count/probability |
| `db/` | async SQLAlchemy models, repositories, session management |
| `api/` | REST routers, WebSocket hub, OpenAPI |
| `scenarios/` | 10 predefined demo scenarios (A–J), CLI + API runnable |
| `backtest/` | synthetic/replay simulation harness with performance report |

---

## 4. Database schema (21 tables, PostgreSQL + Alembic)

```
users(id, username, role, api_key_hash, created_at)
instruments(id, venue, symbol, … 30 spec columns …, active)
instrument_mappings(id, source_instrument_id, hedge_instrument_id, name, enabled)
hedge_configs(id, mapping_id, objective, target_ratio, tolerance_bps, rebalance_*,
              optimizer_*, max_notional, enabled)
orders(id, cycle_id, leg, venue, instrument_id, client_order_id UNIQUE, side, qty,
       price, order_type, status, filled_qty, avg_price, fees, reject_reason, ts)
fills(id, order_id, qty, price, fee, is_maker, liquidity_flag, exec_id UNIQUE, ts)
positions(id, venue, instrument_id, side, qty, avg_entry, realized_pnl, funding_paid,
          funding_received, updated_at)   -- UNIQUE(venue, instrument_id)
market_data(id, venue, symbol, bid, ask, mid, last, spread, volume, is_stale, ts)
funding(id, venue, symbol, rate, interval_hours, applied_to_position_id, amount, ts)
fees(id, order_id, fill_id, kind, amount, currency, ts)
fx_rates(id, base_ccy, quote_ccy, rate, source, ts)
hedge_cycles(id, mapping_id, config_id, state, objective, source_qty, hedge_qty,
             hedge_ratio, residual_exposure, correlation_id, error, created_at, updated_at)
hedge_cycle_events(id, cycle_id, seq, from_state, to_state, event, payload JSONB, ts)
risk_events(id, level, scope, mapping_id, metric, value, threshold, message, ts)
margin_snapshots(id, venue, balance, equity, used_margin, free_margin, margin_level,
                 maintenance_margin, ts)
pnl_records(id, scope, mapping_id, gross, fees, funding, swap, spread, slippage,
            fx_impact, net, realized, unrealized, ts)
system_events(id, kind, severity, component, message, payload JSONB, ts)
audit_logs(id, actor, action, entity_type, entity_id, before JSONB, after JSONB,
           correlation_id, ts)
emergency_actions(id, trigger, level, action, executed, result, ts)
configuration_changes(id, actor, entity, entity_id, before JSONB, after JSONB, ts)
scenario_runs(id, scenario, status, seed, summary JSONB, started_at, finished_at)
```

Append-only tables (`hedge_cycle_events`, `audit_logs`, `system_events`, `risk_events`,
`emergency_actions`, `configuration_changes`) form the audit trail: every state transition,
every calculation input/output, every config edit is reconstructible.

---

## 5. Execution state machine

```
CREATED → VALIDATED → AWAITING_MARKET_DATA → CALCULATED → RISK_APPROVED
        → LEG_1_SUBMITTED → [LEG_1_PARTIAL] → LEG_1_FILLED
        → LEG_2_SUBMITTED → [LEG_2_PARTIAL] → BOTH_FILLED → COMPLETED

  any → FAILED            (validation/risk/market-data rejection, no exposure taken)
  any → RECOVERY_REQUIRED (leg 1 has exposure and leg 2 cannot complete)
  any → EMERGENCY         (kill switch / threshold breach)
  BOTH_FILLED → REBALANCING → COMPLETED       (residual outside tolerance)
  * → RISK_REDUCTION → COMPLETED              (portfolio limit breach)
```

Transitions are declared in a table and validated — an illegal transition raises and is
logged, never silently applied. Every transition is persisted to `hedge_cycle_events` with
its payload before the side effect is attempted (write-ahead), which is what makes restart
recovery able to tell "submitted but unknown" from "never submitted".

---

## 6. Testing strategy

| Layer | Approach |
|---|---|
| Domain / quantity conversion | table-driven unit tests over many contract-size × lot-size × tick combinations, incl. inverse and non-decimal steps |
| Hedge calculator | property tests: residual → 0 for exact objectives; ratio monotonicity; rounding never exceeds `max_safe_qty` |
| Optimizer | exhaustive small-grid comparison vs brute force |
| Risk / margin / liquidation | closed-form expected values, boundary conditions at each threshold |
| State machine | legal/illegal transition matrix, full-path walks |
| Fault handling | one test per injectable fault asserting the safe response |
| Reconciliation | seeded divergences (unknown/missing/mismatch/stale) |
| Restart recovery | kill mid-cycle, rebuild from DB, assert state + risk match |
| API | httpx ASGI transport against a SQLite-backed app |
| WebSocket | connect, drive an event, assert frame content |
| Integration | full pipeline: market data → calc → risk → execute → fill → position → reconcile → monitor → rebalance |

Target: every calculation module has direct tests; the end-to-end workflow is covered by
integration tests that run in CI without Docker (SQLite), and against PostgreSQL in Compose.

---

## 7. Paper-trading design (safety)

1. `TradingMode` enum with `PAPER` default; `LIVE` is **not implemented**.
2. `DeltaAdapter` / `MT5Adapter` live classes exist only as stubs whose every write method
   raises `LiveTradingDisabledError`. They are never registered in the venue factory.
3. The venue factory refuses to construct a non-paper adapter unless
   `HEDGELAB_ALLOW_LIVE=1` **and** a live adapter is registered — neither is possible in
   this repository. A test asserts the factory raises for `LIVE`.
4. No credential field exists in settings for a live exchange; nothing reads an API secret.
5. Optional external market data is **read-only** and off by default
   (`MARKET_DATA_SOURCE=simulator`).
6. Every order object carries `is_paper=True`, persisted, and surfaced in the UI banner.

---

## 8. Milestones — all delivered

Recorded with what was actually verified, not with what was intended.

| # | Milestone | Exit criterion | Outcome |
|---|---|---|---|
| 1 | Domain + registry + quantity conversion | conversion tests green | 37 tests; 4 distinct contract-to-lot ratios across 5 pairs |
| 2 | Market-data simulator + scenarios | deterministic replay from seed | same seed ⇒ identical 150-step path; 7 scenarios |
| 3 | Paper venue adapters | partial fills, rejects, slippage observable | 47 tests; slippage emerges from book depth |
| 4 | Hedge calculator + optimiser | 9 objectives, full result, tests green | 55 tests; 14-step derivation on every result |
| 5 | Risk + costs + portfolio | margin/liquidation/threshold tests green | 30 tests; liquidation matches closed form |
| 6 | DB + Alembic + repositories | migration applies on PostgreSQL | 21 tables, JSONB, numeric(38,18), clean round-trip |
| 7 | Execution machine + rebalancer | every transition persisted | 16 states, structurally validated on each request |
| 8 | Reconciliation + restart recovery | restart rebuilds state | 6 issue classes; `resumable=True` after a simulated restart |
| 9 | Fault injection | 14 faults with asserted responses | 14 faults; each with a test |
| 10 | REST + WebSocket API | OpenAPI complete, WS streams | 56 routes, 12 topics, 43 API tests |
| 11 | React dashboard | builds, talks to the API | 8 pages, typechecks, 268 kB bundle |
| 12 | Compose + docs + acceptance | the 25-step demo passes | **25/25**, repeatably, against PostgreSQL in Docker |

### Where the plan changed during implementation

Worth recording, because the deviations were driven by defects the plan did not
anticipate:

* **A tenth instrument was added.** Two enabled mappings originally shared
  `PAPER_MT5:BTCUSD`, which double-counted the same broker position in
  portfolio exposure. A mini BTC contract plus a registry invariant forbidding
  shared legs fixed it.
* **Risk validation moved to the post-trade book.** Evaluating the current
  state made the risk engine block the hedge that would fix the exposure it was
  reporting.
* **The acceptance run became a test.** It found four bugs nothing else did, so
  it now runs in CI and deliberately starts from a dirty state.
* **The backtest gained a configurable step interval.** At the simulator's
  native 0.5s tick, 500 steps covered four minutes and measured nothing.

The final state, its limitations and the live-adapter extension points are in
[PROJECT_STATUS.md](PROJECT_STATUS.md).
