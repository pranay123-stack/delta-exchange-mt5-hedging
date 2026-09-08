# API

FastAPI, 56 routes, OpenAPI at `/docs`. Everything under `/api`.

**Decimals are strings.** A quantity that round-trips through a JSON float can
come back off the venue's lattice, and the client would then display a number
the venue would reject.

## Headers

| Header | Direction | Meaning |
|---|---|---|
| `X-API-Key` | request | Authentication (see below) |
| `X-Confirm-Action: CONFIRM` | request | Required for irreversible actions |
| `X-Correlation-ID` | both | Echoed if supplied, generated otherwise |
| `X-Paper-Only: true` | response | On every response, always |
| `X-Trading-Mode: PAPER` | response | On every response, always |

## Authentication and roles

`HEDGELAB_API_KEYS` holds comma-separated `user:role:key` triples. Only the
SHA-256 hash is retained, and comparison is constant-time.

| Role | May |
|---|---|
| `VIEWER` | read everything |
| `TRADER` | + execute hedges, rebalance, run scenarios, drive the simulator |
| `ADMIN` | + edit configuration, inject faults, kill switch, adopt venue state |

With no keys configured the API is open — acceptable for a local paper demo,
and **refused** when `HEDGELAB_ENVIRONMENT` is anything but `local` or `test`.
A demo default must not become a production hole.

## Hedging

| Route | Notes |
|---|---|
| `POST /hedge/calculate` | Full derivation: 14 steps with formula and value |
| `POST /hedge/optimize` | Lattice search; returns every candidate scored |
| `POST /hedge/execute` | TRADER. Two-leg cycle with recovery |
| `POST /hedge/open-position` | TRADER. Take naked source exposure to hedge |
| `GET  /hedge/rebalance/{pair}` | Read-only assessment; safe on a timer |
| `POST /hedge/rebalance/{pair}` | TRADER. Trades the adjustment |
| `GET  /hedge/cycles` | History, filterable by state |
| `GET  /hedge/cycles/{id}` | Every transition with its payload |
| `GET  /hedge/state-machine` | The transition graph and its validation |

```bash
curl -X POST localhost:8001/api/hedge/calculate -H 'Content-Type: application/json' -d '{
  "source_key": "PAPER_DELTA:BTCUSDT-PERP",
  "hedge_key":  "PAPER_MT5:BTCUSD",
  "source_quantity": "5000",
  "objective": "QUOTE_PNL_NEUTRAL"
}'
```

## Trading and market data

| Route | Notes |
|---|---|
| `GET /orders` | Filterable by venue and cycle; includes slippage |
| `GET /orders/fills` | Every execution report |
| `GET /positions` | With base units, notional and delta, not just quantity |
| `GET /accounts` | Balance, equity, margin level per venue |
| `GET /market-data` | Top of book for everything |
| `GET /market-data/{key}/book` | Aggregated depth |

## Risk, P&L and emergency

| Route | Notes |
|---|---|
| `GET   /risk` · `GET /risk/{pair}` | Graded assessment with breaches and actions |
| `GET   /risk/statistics` | Measured volatility, correlation and beta per pair, with β's standard error, sample counts, and whether the estimate is trusted |

> Literal paths under `/risk` are registered **before** `/risk/{mapping_name}`.
> FastAPI matches in registration order, so declaring the parameterised route
> first makes it swallow its literal siblings — `/risk/statistics` resolved
> there and returned "unknown hedge mapping named 'statistics'".
| `GET   /portfolio` | Aggregate exposure, limits, concentration |
| `POST  /risk/snapshot` | TRADER. Persist risk, margin and P&L |
| `PATCH /risk/thresholds` | ADMIN. Audited |
| `PATCH /portfolio/limits` | ADMIN. Audited |
| `GET   /pnl` | Attribution; `consistent` asserts the parts sum |
| `GET   /funding` | Forward projection per pair |
| `POST  /funding/settle` | TRADER. Charge one interval / one night |
| `POST  /emergency/kill-switch` | ADMIN + confirmation |
| `DELETE /emergency/kill-switch` | ADMIN |
| `POST  /emergency/apply` | ADMIN. Apply the prescribed actions for a pair |

## Reference data

| Route | Notes |
|---|---|
| `GET  /instruments` · `/instruments/{key}` | Full spec plus derived sizing |
| `POST /instruments` | ADMIN. New instrument, tradable immediately |
| `DELETE /instruments/{key}` | ADMIN. Refuses if a hedge pair uses it |
| `GET  /instrument-mappings` | Includes the computed unit conversion |
| `POST /instrument-mappings` | ADMIN |
| `GET  /hedge-configs` · `/fx-rates` | Effective configuration |

## Simulation, faults and scenarios

| Route | Notes |
|---|---|
| `GET  /paper/simulation` | Clock, seed, active scenarios, the seven profiles |
| `POST /paper/simulation/advance` | TRADER |
| `POST /paper/simulation/scenario` | TRADER. Globally or per venue |
| `POST /paper/simulation/shock` | TRADER. Instantaneous relative move |
| `POST /paper/simulation/funding-rate` | TRADER |
| `GET  /fault-injection` | Armed faults, history, connectivity |
| `POST /fault-injection` | ADMIN. Arms, or applies immediately |
| `DELETE /fault-injection` | ADMIN. Disarm and reconnect everything |
| `GET  /scenarios` · `POST /scenarios/run` · `/run-all` | The ten demos |

## Reconciliation and audit

| Route | Notes |
|---|---|
| `GET  /reconciliation` | Three-way comparison |
| `POST /reconciliation/recover` | TRADER. Restart recovery verdict |
| `POST /reconciliation/adopt` | ADMIN + confirmation |
| `GET  /audit` | Filterable by entity type and correlation id |
| `GET  /audit/system-events` · `/audit/configuration-changes` | |
| `GET  /risk/events/log` · `/emergency/actions` | |
| `GET  /pnl/history` · `/funding/history` · `/margin/history` | |

## System

`GET /api/health` (liveness), `GET /api/ready` (readiness — database plus
venue connectivity), `GET /api/system/status` (everything).

## WebSocket

`ws://host/ws?topics=prices,risk` — omit `topics` for all of them.

```
prices · positions · hedge_ratio · residual · pnl · risk
orders · fills · hedge_cycle · hedge_cycle_state · system · alerts
```

Frames are `{"topic": "...", "ts": "...", "data": {...}}`. The connection is
duplex: send `{"action":"subscribe","topics":[...]}` to change the
subscription, or `{"action":"ping"}`.

Each client has a bounded queue. A client that cannot keep up loses frames and
is logged; it never applies backpressure to the trading engine.

## Errors

| Status | Meaning |
|---|---|
| 401 / 403 | Missing or insufficient credentials |
| 404 | Unknown instrument, pair or cycle |
| 409 | Venue rejected, or trading is paused / kill switch engaged |
| 422 | Invalid specification or parameter, with the reason |
| 428 | Irreversible action without the confirmation header |
| 503 | Venue unreachable or database down (retryable) |

`LiveTradingDisabledError` maps to **403** — the platform will tell you clearly
that it has no live path rather than failing obscurely.
