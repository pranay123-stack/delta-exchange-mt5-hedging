"""Structured JSON logging with correlation-ID propagation.

Every log line carries the ambient ``correlation_id``, ``cycle_id`` and
``order_id`` when one is in scope, so a single hedge cycle can be traced from
the API request that started it through both legs and into recovery.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_cycle_id: ContextVar[str | None] = ContextVar("cycle_id", default=None)
_order_id: ContextVar[str | None] = ContextVar("order_id", default=None)

#: Every attribute ``logging.LogRecord`` sets itself.  Anything here must not
#: be re-emitted as a structured field, and must not be accepted from ``extra``.
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


@contextmanager
def correlation_scope(
    correlation_id: str | None = None,
    *,
    cycle_id: str | None = None,
    order_id: str | None = None,
) -> Iterator[str]:
    """Bind IDs for the duration of a block, restoring the previous values."""
    cid = correlation_id or new_correlation_id()
    tokens = [
        _correlation_id.set(cid),
        _cycle_id.set(cycle_id if cycle_id is not None else _cycle_id.get()),
        _order_id.set(order_id if order_id is not None else _order_id.get()),
    ]
    try:
        yield cid
    finally:
        _correlation_id.reset(tokens[0])
        _cycle_id.reset(tokens[1])
        _order_id.reset(tokens[2])


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in (
            ("correlation_id", _correlation_id.get()),
            ("cycle_id", _cycle_id.get()),
            ("order_id", _order_id.get()),
        ):
            if value:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=_default)


class HumanFormatter(logging.Formatter):
    """Readable formatter for local development and CLI demos."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"{record.name:<28} {record.getMessage()}"
        )
        cid = _cycle_id.get() or _correlation_id.get()
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if cid:
            base = f"{base}  [{cid}]"
        if extras:
            base = f"{base}  {json.dumps(extras, default=_default)}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class SafeLogger(logging.LoggerAdapter):
    """Logger adapter that cannot be crashed by a colliding ``extra`` key.

    ``Logger.makeRecord`` raises ``KeyError`` if ``extra`` contains a name that
    already exists on a ``LogRecord`` -- ``name``, ``module``, ``args``,
    ``filename`` and a dozen others.  That turns an innocuous log line into an
    exception, and it only fires once the level is low enough for the call to
    be evaluated, so it hides in development and surfaces in production.

    Colliding keys are renamed with a trailing underscore instead.  A trading
    engine must never be taken down by a log statement.
    """

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"{key}_" if key in _RESERVED or key in ("message", "asctime") else key): value
                for key, value in extra.items()
            }
        return msg, kwargs


def get_logger(name: str) -> SafeLogger:
    return SafeLogger(logging.getLogger(name), {})
