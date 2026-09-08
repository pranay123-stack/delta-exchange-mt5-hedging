"""FastAPI application.

Composition happens here and only here: settings are read, the service is
built, the WebSocket hub is attached as the engine's event publisher, and the
routers are mounted.  Nothing below this module knows FastAPI exists.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from ..config import Settings, get_settings
from ..logging_setup import configure_logging, correlation_scope, get_logger, new_correlation_id
from ..service import HedgeLabService
from ..venues.base import LiveTradingDisabledError, VenueError
from .routers import hedging, operations, reference, risk_routes, scenario_routes, trading
from .ws import TOPICS, HubPublisher, WebSocketHub

log = get_logger(__name__)

DESCRIPTION = """
Cross-platform hedging between a **perpetual futures exchange** and an
**MT5 broker**, with the contract-specification differences handled properly:
contracts versus lots, linear versus inverse settlement, funding versus swap
points, and quote currencies that do not match the account currency.

### Paper trading only

This platform has **no live order path**. The live adapter classes exist as
typed extension points and every one of their methods raises. The venue factory
refuses to construct anything but a paper adapter. See `PAPER_TRADING.md`.

### What the API exposes

* `/hedge/calculate` -- the full derivation behind a hedge quantity
* `/hedge/optimize` -- lattice search over valid quantities
* `/hedge/execute` -- a supervised two-leg cycle with recovery
* `/risk`, `/portfolio` -- graded thresholds and portfolio limits
* `/pnl`, `/funding` -- cost attribution down to the individual component
* `/reconciliation` -- three-way state comparison and restart recovery
* `/fault-injection` -- fourteen injectable failure modes
* `/scenarios` -- ten predefined end-to-end demonstrations
"""


def build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_json)
        hub = WebSocketHub()
        service = HedgeLabService(settings, publisher=HubPublisher(hub))
        app.state.hub = hub
        app.state.service = service

        create_schema = settings.database_url.startswith("sqlite")
        await service.startup(create_schema=create_schema)
        ticker_task = asyncio.create_task(_market_data_loop(service, hub))
        log.info(
            "application ready",
            extra={"mode": settings.trading_mode.value, "port": settings.api_port},
        )
        try:
            yield
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task
            await hub.close_all()
            await service.shutdown()
            log.info("application stopped")

    return lifespan


async def _market_data_loop(service: HedgeLabService, hub: WebSocketHub) -> None:
    """Advance the simulated market and stream prices to connected dashboards.

    Deliberately does *not* trade.  Automatic rebalancing is driven from the
    risk endpoints and the scenario runner, so the demo stays deterministic.
    """
    interval = max(0.1, service.settings.simulator_tick_ms / 1000)
    while True:
        try:
            await asyncio.sleep(interval)
            service.advance_market(1)
            if hub.client_count == 0:
                continue
            tickers = service.tickers()
            await hub.publish("prices", {
                "clock": service.simulator.clock.isoformat(),
                "tickers": [
                    {
                        "key": key, "bid": str(t.bid), "ask": str(t.ask),
                        "mid": str(t.mid), "spread_bps": str(t.spread_bps),
                        "funding_rate": str(t.funding_rate) if t.funding_rate is not None else None,
                        "is_stale": t.is_stale,
                        "timestamp": t.timestamp.isoformat(),
                    }
                    for key, t in sorted(tickers.items())
                ],
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("market data loop error", extra={"error": str(exc)})
            await asyncio.sleep(1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="HedgeLab -- Perpetual to MT5 Paper Hedging Platform",
        description=DESCRIPTION,
        version=settings.version,
        lifespan=build_lifespan(settings),
        openapi_tags=[
            {"name": "hedging", "description": "Calculate, optimise and execute hedges."},
            {"name": "trading", "description": "Orders, fills, positions and accounts."},
            {"name": "risk", "description": "Thresholds, portfolio limits, P&L and emergency controls."},
            {"name": "reference data", "description": "Instruments, hedge pairs and configuration."},
            {"name": "operations", "description": "Simulation, faults, reconciliation and audit."},
            {"name": "scenarios", "description": "Predefined end-to-end demonstrations."},
        ],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("X-Correlation-ID")
        with correlation_scope(incoming or new_correlation_id()) as correlation_id:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            response.headers["X-Trading-Mode"] = settings.trading_mode.value
            response.headers["X-Paper-Only"] = "true"
            return response

    @app.exception_handler(LiveTradingDisabledError)
    async def live_disabled_handler(
        request: Request, exc: LiveTradingDisabledError
    ) -> JSONResponse:
        log.error("live trading attempt refused", extra={"path": request.url.path})
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(VenueError)
    async def venue_error_handler(request: Request, exc: VenueError) -> JSONResponse:
        return JSONResponse(
            status_code=503 if exc.retryable else 409,
            content={"detail": str(exc), "venue": exc.venue, "retryable": exc.retryable},
        )

    @app.exception_handler(PermissionError)
    async def permission_handler(request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    for router in (
        reference.router, hedging.router, trading.router,
        risk_routes.router, operations.router, scenario_routes.router,
    ):
        app.include_router(router, prefix="/api")

    @app.get("/", tags=["operations"], summary="Service banner")
    async def root() -> dict[str, Any]:
        return {
            "name": "HedgeLab",
            "version": settings.version,
            "trading_mode": settings.trading_mode.value,
            "paper_only": True,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "websocket": "/ws",
        }

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket, topics: str | None = None) -> None:
        """Live updates.

        ``?topics=prices,risk`` narrows the subscription; the default is
        everything.  The connection is duplex: the client may send
        ``{"action":"subscribe","topics":[...]}`` to change it later.
        """
        hub: WebSocketHub = websocket.app.state.hub
        requested = (
            {t.strip() for t in topics.split(",") if t.strip() in TOPICS}
            if topics else set(TOPICS)
        )
        subscriber = await hub.connect(websocket, requested or set(TOPICS))
        pump = asyncio.create_task(hub.pump(subscriber))
        try:
            await websocket.send_json({
                "topic": "system",
                "data": {"connected": True, "topics": sorted(subscriber.topics),
                         "paper_only": True},
            })
            while True:
                message = await websocket.receive_json()
                if message.get("action") == "subscribe":
                    wanted = {t for t in message.get("topics", []) if t in TOPICS}
                    subscriber.topics = wanted or set(TOPICS)
                    await websocket.send_json({
                        "topic": "system",
                        "data": {"topics": sorted(subscriber.topics)},
                    })
                elif message.get("action") == "ping":
                    await websocket.send_json({"topic": "system", "data": {"pong": True}})
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.info("websocket closed", extra={"error": str(exc)})
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await hub.disconnect(subscriber)

    return app


app = create_app()
