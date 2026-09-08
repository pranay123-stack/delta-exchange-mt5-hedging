# delta-exchange-mt5-hedging

A cross-venue hedging platform that holds a position in a **crypto perpetual**
on a Delta-Exchange-style venue and the economically offsetting position at an
**Exness-style MT5 broker** — handling the fact that the two venues agree on
almost nothing about what a "position" is.

Python 3.12, FastAPI, PostgreSQL, React. Every number in this README was
produced by running the system, and every one of them is reproducible from a
fresh clone.

> **Paper trading only.** There is no live order path in this repository. The
> live adapter classes exist as typed extension points and every one of their
> methods raises; the venue factory refuses to construct anything but a paper
> adapter; no setting anywhere holds an exchange credential. Five structural
> barriers, each covered by a test — see [PAPER_TRADING.md](docs/PAPER_TRADING.md).

---

## Run it

```bash
git clone https://github.com/pranay123-stack/delta-exchange-mt5-hedging
cd delta-exchange-mt5-hedging
cp .env.example .env
docker compose up --build
```

Dashboard <http://localhost:5174> · API docs <http://localhost:8001/docs>

**Start here.** The 25-step acceptance demonstration — it drives the real
engine end to end and exits non-zero if any step fails:

```bash
docker compose exec backend hedgelab demo
```

Then the ten failure scenarios, and the contract conversions that motivate the
whole project:

```bash
docker compose exec backend hedgelab scenario run-all
docker compose exec backend hedgelab instruments pairs
```

No Docker? The whole test suite runs on SQLite with nothing else installed:

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
export HEDGELAB_DATABASE_URL="sqlite+aiosqlite:///$PWD/hedgelab.db"
.venv/bin/python -m pytest tests/ -q      # 402 tests, ~57s
.venv/bin/hedgelab demo                   # the same 25 steps
```

---

## The problem

Hold BTC exposure on a perpetual exchange, hedge it at an MT5 broker. The two
venues disagree on everything that determines the trade:

| | Perpetual exchange | MT5 broker |
|---|---|---|
| Quantity unit | contracts | lots |
| One unit is | `contract_size × multiplier` base units | `units_per_lot` base units |
| Settlement | linear (USDT) **or** inverse (coin-margined) | linear, account currency |
| Financing | funding every N hours | swap points per night, tripled once a week |
| Fees | maker/taker commission | paid through a wider spread |
| Margin | initial/maintenance rate on notional | margin level %, stop-out level |
| Quote currency | USDT | USD |

So `1 contract ≠ 1 lot`, and the ratio is different for every pair. From the
shipped catalogue, computed by the engine:

```
1 BTCUSDT-PERP contract (1 contract = 0.001 BTC)  = 0.001     BTCUSD  lot (1 lot = 1 BTC)
1 ETHUSDT-PERP contract (1 contract = 0.01 ETH)   = 0.001     ETHUSD  lot (1 lot = 10 ETH)
1 PAXGUSDT-PERP contract (1 contract = 0.01 PAXG) = 0.0001    XAUUSD  lot (1 lot = 100 XAU)
1 SOLUSDT-PERP contract (1 contract = 1 SOL)      = 0.01      SOLUSD  lot (1 lot = 100 SOL)
1 BTCUSD-PERP-INV contract (1 contract = 1 USD)   = 0.0000975 BTCUSDm lot (1 lot = 0.1 BTC)
```

Hedging 1000 perpetual contracts with "1000 lots" is a **1000× position
error**, not a rounding problem. Note also that BTC and ETH happen to share a
ratio of `0.001` while having completely unrelated contract sizes — equal
ratios do not mean equal economics, which is why the engine converts through
base units on every call instead of caching a ratio per pair.

### The inverse-contract trap

An inverse perpetual is denominated in *quote* units. Long `N` quote units
entered at `E`, marked at `S`:

```
PnL_quote = N × (S/E − 1)        ⟹   dPnL/dS = N / E
```

The delta depends on the **entry** price, not the mark — while the
mark-to-market base holding is `N / S`. For a $500,000 inverse position entered
at 100,000 and marked at 125,000:

| Objective | Hedge | Because |
|---|---|---|
| `BASE_ASSET_NEUTRAL` | **4.00 lots** | matches the 4 BTC you currently hold |
| `QUOTE_PNL_NEUTRAL` | **5.00 lots** | matches the actual price sensitivity |

A 25% difference in hedge size on the same position, depending on which
question you are asking. Both answers are correct; picking the wrong one leaves
a quarter of the book unhedged. The platform shows you the difference rather
than choosing on your behalf.

---

## What it does

**Nine hedge objectives** — base-asset-, notional-, quote-P&L- and
account-currency-P&L-neutral; custom ratio; partial; funding-adjusted and
cost-adjusted (mean-variance, `h* = β − k/(λσ_h²)`); risk-weighted (beta). Every
result carries a 14-step derivation with the formula and value at each stage,
rendered in the dashboard and stored in the audit log.

**A quantity optimiser** that searches the venue's lattice instead of blindly
rounding. On a live ETH example, rounding toward zero leaves 16.73 bps of
residual where the neighbouring lattice point leaves 10.03 bps — for $0.19 more
cost. Three modes (exact, single-priority step search, weighted score over
seven priorities), cross-checked against brute force.

**Statistics measured, not assumed.** A rolling estimator recovers the
simulator's configured volatilities within 4% across a 6.6× range, and is
invariant to the feed rate across a 24× range of tick intervals. β is reported
with its standard error and flagged when its distance from 1.0 is within
sampling noise — reporting a point estimate alone invites reading noise as
signal, and for a hedge ratio that means trading it.

**A supervised two-leg execution cycle** — sixteen states, transitions declared
as data and validated on every move, each one persisted *before* the side effect
it describes is attempted. That write-ahead ordering is what lets restart
recovery distinguish "submitted, outcome unknown" from "never submitted".

**Recovery that recovers.** Leg 2 rejected, timed out, partially filled or
disconnected each have a specific, tested response:

```
 1. CREATED                -> VALIDATED              validated
 2. VALIDATED              -> AWAITING_MARKET_DATA   awaiting_market_data
 3. AWAITING_MARKET_DATA   -> CALCULATED             hedge_calculated
 4. CALCULATED             -> RISK_APPROVED          risk_approved
 5. RISK_APPROVED          -> LEG_1_SUBMITTED        leg_1_submitted
 6. LEG_1_SUBMITTED        -> LEG_1_FILLED           leg_1_filled
 7. LEG_1_FILLED           -> LEG_2_SUBMITTED        leg_2_submitted
 8. LEG_2_SUBMITTED        -> RECOVERY_REQUIRED      cycle_failed
 9. RECOVERY_REQUIRED      -> REBALANCING            recovery_rebalance
10. REBALANCING            -> COMPLETED              recovered
```

A timeout is not a failure — it is an *unknown outcome*. The coordinator queries
the venue for the client order id it sent rather than retrying blindly, because
retrying is how a book ends up double-hedged.

**Cost attribution that adds up.** A hedged book's P&L is small relative to its
gross legs, so "we lost $563" is useless without the decomposition — and the
components are asserted to sum to the net:

```
source_price_pnl   +7122.50   BTCUSDT-PERP: price P&L at mark 102424.5
hedge_price_pnl    -7003.48   BTCUSD: price P&L at mark 102450.695
source_fees         -252.50   1 fill at 5 bps taker
hedge_spread        -204.92   half of the 8.00 bps quoted spread
funding_paid        -120.50   perpetual funding debited
swap_financing       -90.00   broker overnight rollover
fx_conversion         -1.42   restating the USDT leg into USD
                   ────────
NET                 -562.82
```

**Risk at two levels** — per pair and portfolio, four-level threshold ladder
with prescribed actions, closed-form margin and liquidation for both linear and
inverse instruments, and a kill switch that verifies it actually reached flat
rather than assuming it did.

**Everything is configuration.** There is no `BTCPerpetual` class. An instrument
is a ~40-field record loaded from YAML or the API, and a test registers a wheat
future quoted in EUR at 5000 units per lot — an instrument the code has never
seen — and hedges it with no source change.

---

## Verified

Not claims — output:

```
402 tests passing in ~57s          ruff:  all checks passed
 25/25 acceptance steps            mypy:  strict, 74 files, no issues
 10/10 failure scenarios           tsc:   no issues, frontend builds
 83% coverage overall              21 tables migrated on PostgreSQL 16,
 93–100% on the calculation core   with a clean downgrade round-trip
```

A 500-step backtest (≈21 simulated days, one hour per step) on the BTC pair:

| Hedge ratio | Price drawdown removed | Net P&L | of which carry |
|---|---|---|---|
| 1.00 (full) | **98.8%** | −5,413 | −4,814 |
| 0.50 (partial) | **49.7%** | −12,746 | −3,884 |
| ~0.00 (none) | **1.0%** | −19,933 | −2,973 |

Risk reduction tracks the hedge ratio linearly — that is the correctness check.
The hedge **costs money**: that is what a hedge does, and the platform reports
it rather than dressing it up.

---

## Fourteen bugs the tests and the acceptance run actually found

Listed because they are the reason to trust the rest. Four were found *only* by
the 25-step acceptance run, and three more only by feeding the objectives real
estimates instead of assumptions:

1. **The mean-variance objective divided by the wrong variance.** The carry
   penalty was scaled by the *residual* variance instead of the hedge leg's,
   making it ~300× too large and returning a hedge ratio of **zero** for a pair
   whose hedge removes 98.8% of drawdown. Invisible against the shipped
   assumptions — it produced a plausible 0.92 — and unmissable the moment
   correlation was measured at 0.9998 rather than assumed at 0.995.
2. **The volatility estimator scaled by the configured sampling interval**
   rather than the observed one, overstating volatility by
   `sqrt(actual/configured)` — a silent 3.4× error when a five-minute setting
   met an hourly feed.
3. **The risk gate compared a stale source position** against the projected
   hedge, inventing a 40% residual and blocking the very trade that fixes the
   exposure.
4. **The kill switch could be defeated by a partial fill** while reporting
   success — and separately, **raised** on an unreachable venue, the case where
   it matters most.
5. **IOC orders were left `PARTIALLY_FILLED` forever**, stranding terminal
   orders in the open set and producing a permanent `ORDER_MISMATCH` after every
   restart.
6. **FOK was checked against the fault-capped quantity**, letting a partial fill
   satisfy a fill-or-kill order.
7. **Concentration warned at 45%** when a two-asset portfolio has a 50% floor,
   so every balanced two-asset book was permanently in breach.
8. **Two mappings shared a hedge instrument**, double-counting the same broker
   position — reporting −615,208 of "net exposure" on a flat book.
9. **A log call with `extra={"name": ...}`** collided with a reserved
   `LogRecord` field and raised at INFO level only, so it hid in development.
10. **`Subscriber` was an unhashable dataclass** — the WebSocket hub raised on
    every connection.
11. **Simulated time was welded to real time**, so an 8-hour funding interval
    took 8 real hours.
12. **A parameterised route shadowed its literal siblings** — FastAPI matches in
    registration order, so `/risk/{mapping_name}` swallowed `/risk/statistics`.
13. **The acceptance run was not repeatable** — 25/25 on a clean database,
    18/25 after a scenario sweep, because it inherited state.
14. **Rounding promoted sub-minimum quantities up to the venue minimum**,
    executing more than the calculation asked for.

Full detail in [TESTING.md](docs/TESTING.md).

---

## Architecture

```
   React + TS UI ──── FastAPI (57 routes, REST + WebSocket, OpenAPI)
   (8 pages)                        │
   ┌─────────────────────────────────▼──────────────────────────────────┐
   │ HedgeLabService — the composition root. The CLI and the API are    │
   │ both thin shells over it, so they cannot disagree about behaviour. │
   └──┬────────┬─────────┬──────────┬───────────┬──────────┬────────────┘
   hedge/   risk/    costs/   execution/  reconciliation/ faults/
   9 obj.   margin   funding  16-state    three-way       14 faults
   optim.   liquid.  P&L      machine     restart
   calc.    portf.   attrib.  recovery    recovery
      └────────┴─────────┴─────┬────┴───────────┴──────────┘
   ┌───────────────────────────▼────────────────────────────────────────┐
   │ DOMAIN — InstrumentSpec · QuantityConverter · Exposure · Position  │
   │ Order · decimal maths. Pure functions, no I/O, no database.        │
   └──┬─────────────────────────────────────────┬───────────────────────┘
   venues/ (TradingVenue ABC, paper adapters,  marketdata/ (seeded
   live stubs that raise)                      simulator, 7 scenarios,
      │                                        FX, rolling statistics)
   ┌──▼─────────────────────────────────────────────────────────────────┐
   │ db/ SQLAlchemy 2.0 async · PostgreSQL / SQLite · Alembic ·         │
   │     21 tables, six of them append-only audit                       │
   └────────────────────────────────────────────────────────────────────┘
```

The dependency rule: `domain/` imports nothing above it, and `hedge/`, `risk/`
and `costs/` are pure calculators that receive market data and account state as
arguments rather than fetching them. That is why 402 tests run in under a minute
with no database, no Docker and nothing to mock.

**Decimal everywhere, strings on the wire.** Binary floats break exchange
rounding rules; `0.1 + 0.2 != 0.3` is a curiosity in most software and a
rejected order here. The one deliberate exception is the statistics module,
where sampling error after 200 observations is fourteen orders of magnitude
larger than float precision — and that boundary is explicit.

---

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design decisions and why each one |
| [HEDGE_ENGINE.md](docs/HEDGE_ENGINE.md) | The objectives, the maths, the estimator |
| [EXECUTION_ENGINE.md](docs/EXECUTION_ENGINE.md) | The 16-state machine and its recovery paths |
| [RISK_ENGINE.md](docs/RISK_ENGINE.md) | Margin, liquidation, thresholds, kill switch |
| [FAILURE_RECOVERY.md](docs/FAILURE_RECOVERY.md) | Fourteen faults and their responses |
| [PAPER_TRADING.md](docs/PAPER_TRADING.md) | Why no real order can be placed |
| [DATABASE.md](docs/DATABASE.md) · [API.md](docs/API.md) | Schema and every endpoint |
| [TESTING.md](docs/TESTING.md) | What is covered, and what is not |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) · [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | Running it, and a guided tour |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | What is done, what is not, extension points |

---

## What this is not

Stated plainly, because a portfolio is only useful if you can tell the
difference:

- **It has never traded real money**, and cannot. That is enforced
  structurally, not by a flag.
- **The instrument specifications are illustrative** — modelled on the shape of
  public contract specs, not transcribed from live venue documentation. They are
  configuration, and the YAML says so.
- **The statistics are validated against the simulator's known answer**, not
  against a real market.
- **Single process by design.** Two coordinators hedging the same pair would
  both see the same residual and both trade it; multi-process operation would
  need leader election and a per-pair lock.
- **No load testing, no property-based testing, no browser tests.** The frontend
  typechecks and builds and its endpoints are covered server-side, but nothing
  drives the UI.

The full list, with the live-adapter extension points, is in
[PROJECT_STATUS.md](PROJECT_STATUS.md).
