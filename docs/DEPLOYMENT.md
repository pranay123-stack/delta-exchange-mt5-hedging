# Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
docker compose up --build
```

| Service | Host port | URL |
|---|---|---|
| Dashboard (nginx) | 5174 | <http://localhost:5174> |
| Backend API | 8001 | <http://localhost:8001/docs> |
| PostgreSQL | 5468 | |
| Redis | 6479 | |

Ports are deliberately non-default so the stack runs alongside other projects.
Every one is a variable — override in `.env` rather than editing the compose
file.

The frontend's nginx proxies `/api` and `/ws` to the backend, so the browser
sees a single origin and CORS never applies.

### What starts, in order

1. `postgres` and `redis` come up and pass their health checks.
2. `backend` waits for the database (40 attempts, 2s apart), runs
   `alembic upgrade head`, then serves.
3. `frontend` serves the built bundle.

The backend entrypoint **refuses to start** if `HEDGELAB_TRADING_MODE` is
anything but `PAPER`, exiting 78 (`EX_CONFIG`).

## Local development

```bash
# backend
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
export HEDGELAB_DATABASE_URL="sqlite+aiosqlite:///$PWD/hedgelab.db"
.venv/bin/hedgelab serve                 # :8000, creates the SQLite schema itself

# frontend
cd frontend
npm install && npm run dev               # :5173, proxies to :8000
```

Against PostgreSQL instead:

```bash
export HEDGELAB_DATABASE_URL="postgresql+asyncpg://hedgelab:hedgelab@localhost:5468/hedgelab"
.venv/bin/alembic upgrade head
.venv/bin/hedgelab serve
```

The API creates the schema directly only for SQLite. On PostgreSQL, Alembic
owns it.

## Configuration

Every setting is an environment variable prefixed `HEDGELAB_`. The ones that
matter:

| Variable | Default | Notes |
|---|---|---|
| `HEDGELAB_TRADING_MODE` | `PAPER` | Anything else is refused |
| `HEDGELAB_ENVIRONMENT` | `local` | Outside `local`/`test`, API keys are mandatory |
| `HEDGELAB_DATABASE_URL` | PostgreSQL on 5468 | `sqlite+aiosqlite:///…` also supported |
| `HEDGELAB_REDIS_URL` | Redis on 6479 | Degrades to in-process if unreachable |
| `HEDGELAB_API_KEYS` | *(unset)* | `user:role:key` triples, comma separated |
| `HEDGELAB_ACCOUNT_CURRENCY` | `USD` | Reporting currency for every aggregate |
| `HEDGELAB_PAPER_STARTING_BALANCE` | `250000` | Per venue |
| `HEDGELAB_SIMULATOR_SEED` | `20260908` | Same seed ⇒ same prices |
| `HEDGELAB_SIMULATOR_TICK_MS` | `500` | Real-time cadence of the market loop |
| `HEDGELAB_SIMULATOR_SECONDS_PER_TICK` | `60` | Simulated time per tick, so the market runs 120x real time |
| `HEDGELAB_STATS_SAMPLE_SECONDS` | `300` | Volatility sampling interval, in simulated time |
| `HEDGELAB_STATS_WINDOW` / `_MIN_SAMPLES` | `500` / `30` | Estimator window and the floor below which it refuses to answer |
| `HEDGELAB_LOG_LEVEL` / `_LOG_JSON` | `INFO` / `true` | |
| `HEDGELAB_MAX_SPREAD_BPS_FOR_EXECUTION` | `50` | Execution quality gate |

There is deliberately **no setting for an exchange credential** — the platform
has no live path and nothing to authenticate with.

## Security

* Container runs as an unprivileged user (uid 10001).
* No secret is committed; `.env` is gitignored and `.env.example` holds only
  placeholders.
* API keys are stored as SHA-256 hashes and compared in constant time.
* Irreversible actions require `ADMIN` plus `X-Confirm-Action: CONFIRM`.
* Unauthenticated operation is refused outside `local`/`test`.
* Every mutating action writes to `audit_logs`.

## Observability

* **Structured JSON logs** on stdout with `correlation_id`, `cycle_id` and
  `order_id` bound in scope.
* **`GET /api/health`** — liveness. **`GET /api/ready`** — readiness, including
  per-venue connectivity, so a disconnected venue shows as not-ready.
* Docker health checks on all four services.
* Six append-only audit tables; see [DATABASE.md](DATABASE.md).

To trace one hedge end to end:

```bash
curl -H 'X-Correlation-ID: my-trace-1' -X POST localhost:8001/api/hedge/execute \
  -H 'Content-Type: application/json' \
  -d '{"mapping_name":"BTC perp -> BTC CFD","source_quantity":"5000"}'

curl 'localhost:8001/api/audit?correlation_id=my-trace-1'
docker compose logs backend | grep my-trace-1
```

## Operations

```bash
docker compose logs -f backend
docker compose exec backend hedgelab status
docker compose exec backend hedgelab scenario run-all
docker compose exec backend alembic upgrade head
docker compose exec postgres pg_dump -U hedgelab hedgelab > backup.sql

docker compose down          # keeps the volume
docker compose down -v       # deletes the database
```

## Scaling notes

The system is **single-process by design**: one process owns the book. Running
two would need leader election and a per-pair lock, because two coordinators
hedging the same pair would both see the same residual and both trade it.

If you did need to scale, the split that makes sense is read replicas for the
dashboard's polling endpoints, which are the only genuinely read-heavy path.
The execution path is inherently serial per pair.
