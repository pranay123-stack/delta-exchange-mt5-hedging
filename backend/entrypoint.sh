#!/usr/bin/env bash
# Container entrypoint.
#
# Applies migrations before serving, so the schema is always Alembic's and
# never whatever SQLAlchemy happened to create.
set -euo pipefail

if [[ "${HEDGELAB_TRADING_MODE:-PAPER}" != "PAPER" ]]; then
  echo "refusing to start: HEDGELAB_TRADING_MODE=${HEDGELAB_TRADING_MODE} but this" >&2
  echo "image has no live trading implementation. See PAPER_TRADING.md." >&2
  exit 78   # EX_CONFIG
fi

wait_for_database() {
  local attempts=${DB_WAIT_ATTEMPTS:-40}
  for ((i = 1; i <= attempts; i++)); do
    if python -c "
import asyncio, sys
from hedgelab.config import get_settings
from hedgelab.db.session import Database
async def main():
    db = Database.from_settings(get_settings())
    ok = await db.ping()
    await db.dispose()
    sys.exit(0 if ok else 1)
asyncio.run(main())
" 2>/dev/null; then
      echo "database is reachable"
      return 0
    fi
    echo "waiting for the database (${i}/${attempts})"
    sleep 2
  done
  echo "database never became reachable" >&2
  return 1
}

case "${1:-serve}" in
  serve)
    wait_for_database
    echo "applying migrations"
    alembic upgrade head
    echo "starting API on ${HEDGELAB_API_HOST:-0.0.0.0}:${HEDGELAB_API_PORT:-8000} (PAPER mode)"
    # No --log-config: the application installs its own structured JSON
    # logging during startup and clears uvicorn's handlers. Access logs are
    # disabled because every request is already traceable by correlation ID.
    exec uvicorn hedgelab.api.app:app \
      --host "${HEDGELAB_API_HOST:-0.0.0.0}" \
      --port "${HEDGELAB_API_PORT:-8000}" \
      --no-access-log
    ;;
  migrate)
    wait_for_database
    exec alembic upgrade head
    ;;
  demo|scenario|backtest|instruments|status|calculate|optimize|init-db)
    wait_for_database
    exec hedgelab "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
