# syntax=docker/dockerfile:1
#
# Backend image. Multi-stage so the runtime layer carries no build toolchain.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install . psycopg2-binary


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEDGELAB_TRADING_MODE=PAPER

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 hedgelab

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=hedgelab:hedgelab alembic.ini ./
COPY --chown=hedgelab:hedgelab alembic ./alembic
COPY --chown=hedgelab:hedgelab src ./src
COPY --chown=hedgelab:hedgelab entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Never run as root, and never with a live trading mode.
USER hedgelab
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["serve"]
