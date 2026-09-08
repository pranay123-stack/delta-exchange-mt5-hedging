"""Declarative base and portable column types.

The platform targets PostgreSQL in production and SQLite in tests, so a few
types need dialect-aware variants:

* ``JSONB`` on PostgreSQL, ``JSON`` on SQLite -- JSONB is indexable and is what
  the audit queries want, but SQLite has no such type.
* ``NUMERIC(38, 18)`` everywhere for money.  SQLite has no native decimal, so
  values round-trip through text; the ``DecimalString`` type makes that
  explicit rather than silently returning floats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.types import JSON, String


class Base(DeclarativeBase):
    """Common declarative base."""


#: JSONB where available, JSON elsewhere.
JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Money(TypeDecorator[Decimal]):
    """Fixed-point decimal that survives SQLite.

    SQLite's NUMERIC affinity converts to float, which is unacceptable for
    quantities that must round-trip exactly onto an exchange lattice.  On
    SQLite the value is stored as text and parsed back to ``Decimal``.
    """

    impl = Numeric(38, 18)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(64))
        return dialect.type_descriptor(Numeric(38, 18))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        if dialect.name == "sqlite":
            return format(value, "f")
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that stays aware on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp_column(**kwargs: Any) -> Any:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False, **kwargs)
