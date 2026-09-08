# Database

PostgreSQL in production, SQLite for tests. Twenty-one tables in four groups,
managed by Alembic.

## Portability

Two types need dialect-aware handling (`db/base.py`):

* **`Money`** — `NUMERIC(38,18)` on PostgreSQL, **text** on SQLite. SQLite's
  NUMERIC affinity converts to float, which would silently destroy exchange
  quantity precision. Text round-trips exactly.
* **`JSONType`** — `JSONB` on PostgreSQL (indexable, which the audit queries
  want), plain `JSON` on SQLite.

`UTCDateTime` keeps timestamps timezone-aware on both.

This is why the whole test suite runs on SQLite with no Docker while production
gets JSONB and real fixed-point decimals.

## The tables

### Reference data

| Table | Purpose |
|---|---|
| `users` | username, role (`VIEWER`/`TRADER`/`ADMIN`), API key **hash** |
| `instruments` | the full `InstrumentSpec`, field for field, ~40 columns |
| `instrument_mappings` | source ↔ hedge pairs, unique on the pair |
| `hedge_configs` | objective, tolerance, rebalance and optimiser settings |

`instruments` mirrors the domain model exactly so a spec round-trips between
YAML, the database and the API without translation loss.

### Trading

| Table | Purpose |
|---|---|
| `hedge_cycles` | one row per two-leg execution |
| `hedge_cycle_events` | append-only transition log, unique on `(cycle, sequence)` |
| `orders` | `client_order_id` is **UNIQUE** — the idempotency key |
| `fills` | `exec_id` is **UNIQUE** — a replayed report cannot double-count |
| `positions` | unique on `(venue, symbol)`, signed quantity |

Two unique constraints are load-bearing rather than decorative:

* `orders.client_order_id` — a retry after a timeout fails to insert instead of
  creating a duplicate position.
* `fills.exec_id` — venues do resend execution reports. Applying one twice
  would double the position. The constraint is the last line of defence behind
  the in-memory de-duplication in `Order.apply_fill`.

Positions store a **signed** quantity rather than a side plus a magnitude,
which removes an entire class of bug where the two disagree.

### Market data

| Table | Purpose |
|---|---|
| `market_data` | ticker snapshots, indexed on `(venue, symbol, timestamp)` |
| `funding` | applied funding and swap charges, with the rate used |
| `fees` | per-fill fee attribution by kind |
| `fx_rates` | conversion rates with their source |

### Observability — append-only

| Table | Purpose |
|---|---|
| `risk_events` | every threshold breach, with value and threshold |
| `margin_snapshots` | balance, equity, used/free margin, margin level |
| `pnl_records` | the full attribution, plus the breakdown as JSON |
| `system_events` | recovery, reconciliation, rehydration, reference sync |
| `audit_logs` | who did what, with before/after state |
| `emergency_actions` | every action the risk engine took, and its result |
| `configuration_changes` | every config edit, before and after |
| `scenario_runs` | demo scenario runs and their outcome |

Nothing updates or deletes from these. That is what makes "why did the system
do that?" answerable after the fact.

## Correlation

`correlation_id` threads through `hedge_cycles`, `hedge_cycle_events`,
`orders`, `risk_events`, `audit_logs`, `system_events` and every structured log
line. One request produces one id, and `GET /api/audit?correlation_id=…`
retrieves everything that happened under it.

## Migrations

```bash
export HEDGELAB_DATABASE_URL="postgresql+asyncpg://hedgelab:hedgelab@localhost:5468/hedgelab"
alembic upgrade head          # apply
alembic downgrade base        # roll back
alembic revision --autogenerate -m "description"
```

The URL comes from application settings via `env.py`, never from `alembic.ini`,
so no connection string or password is ever committed.

Verified on PostgreSQL 16: 21 tables plus `alembic_version`, `audit_logs.before`
is `jsonb`, `positions.quantity` is `numeric(38,18)`, and
`downgrade base` → `upgrade head` round-trips cleanly.

The API creates the schema directly only when the URL is SQLite. On PostgreSQL,
Alembic owns the schema and the container entrypoint runs `alembic upgrade head`
before serving.

## Retention

Not implemented, and it would be needed for a long-running deployment.
`market_data` grows fastest — one row per instrument per persisted snapshot.
The append-only audit tables are the ones you would want to keep longest and
partition by month rather than prune. Called out in
[PROJECT_STATUS.md](../PROJECT_STATUS.md) rather than half-built.
