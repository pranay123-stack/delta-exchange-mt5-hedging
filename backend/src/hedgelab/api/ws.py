"""WebSocket hub.

One connection per browser tab, subscribed to a set of topics.  A slow or dead
client must never block the trading engine, so publishing is fire-and-forget:
each send is attempted, and a client that errors is dropped rather than retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import WebSocket

from ..logging_setup import get_logger

log = get_logger(__name__)

TOPICS = (
    "prices",
    "positions",
    "hedge_ratio",
    "residual",
    "pnl",
    "risk",
    "orders",
    "fills",
    "hedge_cycle",
    "hedge_cycle_state",
    "system",
    "alerts",
)


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


#: ``eq=False`` keeps the default identity hash: subscribers live in a set and
#: a value-equality dataclass is unhashable, which would break every connect.
@dataclass(eq=False)
class Subscriber:
    websocket: WebSocket
    topics: set[str] = field(default_factory=lambda: set(TOPICS))
    #: Bounded queue: a client that cannot keep up loses frames rather than
    #: applying backpressure to the engine.
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    dropped: int = 0


class WebSocketHub:
    """Fan-out of engine events to connected dashboards."""

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, topics: set[str] | None = None) -> Subscriber:
        await websocket.accept()
        subscriber = Subscriber(websocket=websocket, topics=topics or set(TOPICS))
        async with self._lock:
            self._subscribers.add(subscriber)
        log.info(
            "websocket connected",
            extra={"topics": sorted(subscriber.topics), "clients": len(self._subscribers)},
        )
        return subscriber

    async def disconnect(self, subscriber: Subscriber) -> None:
        async with self._lock:
            self._subscribers.discard(subscriber)
        log.info("websocket disconnected", extra={"clients": len(self._subscribers)})

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Queue a frame for every subscriber interested in ``topic``."""
        if not self._subscribers:
            return
        frame = json.dumps(
            {"topic": topic, "ts": datetime.now(UTC).isoformat(), "data": payload},
            default=_encode,
        )
        for subscriber in list(self._subscribers):
            if topic not in subscriber.topics:
                continue
            try:
                subscriber.queue.put_nowait(frame)
            except asyncio.QueueFull:
                subscriber.dropped += 1
                if subscriber.dropped % 100 == 1:
                    log.warning(
                        "websocket client is not keeping up; dropping frames",
                        extra={"dropped": subscriber.dropped, "topic": topic},
                    )

    async def pump(self, subscriber: Subscriber) -> None:
        """Drain one subscriber's queue until the socket closes."""
        try:
            while True:
                frame = await subscriber.queue.get()
                await subscriber.websocket.send_text(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("websocket send failed; closing", extra={"error": str(exc)})

    @property
    def client_count(self) -> int:
        return len(self._subscribers)

    async def close_all(self) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            with contextlib.suppress(Exception):
                await subscriber.websocket.close()


class HubPublisher:
    """Adapts :class:`WebSocketHub` to the engine's ``EventPublisher`` port."""

    def __init__(self, hub: WebSocketHub) -> None:
        self.hub = hub

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        await self.hub.publish(topic, payload)
