"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings
from ..logging_setup import get_logger
from .base import Base

log = get_logger(__name__)


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        connect_args: dict[str, object] = {}
        if url.startswith("sqlite"):
            # SQLite refuses cross-thread use by default; the async driver
            # multiplexes over a thread pool, so this must be relaxed.
            connect_args["check_same_thread"] = False
        self.engine: AsyncEngine = create_async_engine(
            url, echo=echo, future=True, pool_pre_ping=not url.startswith("sqlite"),
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(settings.database_url, echo=settings.database_echo)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope.  Commits on success, rolls back on error."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def create_all(self) -> None:
        """Create the schema directly.

        Used by tests and the SQLite quick-start path.  PostgreSQL deployments
        go through Alembic so migrations stay the single source of truth.
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("database schema created", extra={"url": self._safe_url()})

    async def drop_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def ping(self) -> bool:
        from sqlalchemy import text

        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.warning("database ping failed", extra={"error": str(exc)})
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()

    def _safe_url(self) -> str:
        """URL with any password removed, safe to log."""
        if "@" not in self.url:
            return self.url
        scheme, rest = self.url.split("://", 1)
        credentials, host = rest.split("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
