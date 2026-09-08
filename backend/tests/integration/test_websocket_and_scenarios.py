"""WebSocket streaming and the ten predefined demo scenarios."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hedgelab.api.app import create_app
from hedgelab.api.ws import TOPICS, WebSocketHub
from hedgelab.config import Settings
from hedgelab.scenarios.definitions import SCENARIOS, get_scenario
from hedgelab.scenarios.runner import ScenarioRunner
from hedgelab.service import HedgeLabService

D = Decimal
SPEC_DIR = Path(__file__).resolve().parents[2] / "src" / "hedgelab" / "instruments" / "specs"


@pytest.fixture
def ws_client(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}",
        instrument_spec_dir=SPEC_DIR,
        environment="test",
        simulator_tick_ms=120,
        simulator_seed=31337,
    )
    with TestClient(create_app(settings)) as client:
        yield client


# ======================================================================
# WebSocket
# ======================================================================
def test_websocket_greets_with_the_topic_list(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/ws") as socket:
        greeting = socket.receive_json()
        assert greeting["topic"] == "system"
        assert greeting["data"]["connected"] is True
        assert greeting["data"]["paper_only"] is True
        assert set(greeting["data"]["topics"]) == set(TOPICS)


def test_websocket_topic_filter_is_honoured(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/ws?topics=prices,risk") as socket:
        greeting = socket.receive_json()
        assert set(greeting["data"]["topics"]) == {"prices", "risk"}


def test_websocket_streams_prices(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/ws?topics=prices") as socket:
        socket.receive_json()                      # greeting
        frame = socket.receive_json()
        assert frame["topic"] == "prices"
        assert frame["data"]["tickers"]
        first = frame["data"]["tickers"][0]
        assert {"key", "bid", "ask", "mid", "spread_bps"} <= set(first)
        # Decimals arrive as strings, never floats.
        assert isinstance(first["bid"], str)


def test_websocket_can_resubscribe(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/ws?topics=prices") as socket:
        socket.receive_json()
        socket.send_json({"action": "subscribe", "topics": ["risk", "orders"]})
        for _ in range(10):
            frame = socket.receive_json()
            if frame["topic"] == "system":
                assert set(frame["data"]["topics"]) == {"risk", "orders"}
                return
        pytest.fail("did not receive the subscription acknowledgement")


def test_websocket_ping(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/ws?topics=alerts") as socket:
        socket.receive_json()
        socket.send_json({"action": "ping"})
        frame = socket.receive_json()
        assert frame["data"]["pong"] is True


def test_websocket_receives_hedge_cycle_events(ws_client: TestClient) -> None:
    """Executing a hedge over REST must push state transitions to the socket."""
    with ws_client.websocket_connect("/ws?topics=hedge_cycle") as socket:
        socket.receive_json()
        ws_client.post("/api/hedge/open-position", json={
            "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "2000"})
        response = ws_client.post("/api/hedge/execute", json={
            "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "0"})
        assert response.status_code == 200

        seen: list[str] = []
        for _ in range(30):
            frame = socket.receive_json()
            if frame["topic"] == "hedge_cycle":
                seen.append(frame["data"]["to_state"])
                if "COMPLETED" in seen:
                    break
        assert "VALIDATED" in seen
        assert "COMPLETED" in seen


async def test_hub_drops_frames_rather_than_blocking() -> None:
    """A stalled client must never apply backpressure to the engine."""

    class DeadSocket:
        async def accept(self) -> None:
            return None

    hub = WebSocketHub()
    subscriber = await hub.connect(DeadSocket(), {"prices"})  # type: ignore[arg-type]
    for index in range(400):
        await hub.publish("prices", {"index": index})
    assert subscriber.queue.full()
    assert subscriber.dropped > 0
    await hub.disconnect(subscriber)


async def test_hub_ignores_unsubscribed_topics() -> None:
    class DeadSocket:
        async def accept(self) -> None:
            return None

    hub = WebSocketHub()
    subscriber = await hub.connect(DeadSocket(), {"risk"})  # type: ignore[arg-type]
    await hub.publish("prices", {"x": 1})
    assert subscriber.queue.empty()
    await hub.publish("risk", {"x": 1})
    assert not subscriber.queue.empty()


async def test_hub_serialises_decimals_as_strings() -> None:
    class DeadSocket:
        async def accept(self) -> None:
            return None

    hub = WebSocketHub()
    subscriber = await hub.connect(DeadSocket(), {"pnl"})  # type: ignore[arg-type]
    await hub.publish("pnl", {"net": D("12.3456789")})
    frame = json.loads(subscriber.queue.get_nowait())
    assert frame["data"]["net"] == "12.3456789"


# ======================================================================
# scenarios
# ======================================================================
def test_ten_scenarios_are_defined() -> None:
    assert sorted(SCENARIOS) == list("ABCDEFGHIJ")
    assert all(d.description for d in SCENARIOS.values())


def test_scenario_lookup_accepts_key_or_name() -> None:
    assert get_scenario("a").key == "A"
    assert get_scenario("Normal BTC hedge").key == "A"
    with pytest.raises(KeyError):
        get_scenario("Z")


@pytest.mark.parametrize("key", list("ABCDEFGHIJ"))
async def test_each_scenario_runs_and_passes(service: HedgeLabService, key: str) -> None:
    """Every scenario drives the real engine and reaches its stated outcome."""
    result = await ScenarioRunner(service).run(key)
    assert result.error is None, result.error
    assert result.steps, "scenario recorded no steps"
    assert result.summary
    assert result.passed, f"scenario {key} did not meet its success condition: {result.summary}"


async def test_scenario_runs_are_persisted(service: HedgeLabService) -> None:
    runner = ScenarioRunner(service)
    await runner.run("A")
    history = await runner.history()
    assert history
    assert history[0]["scenario"] == "A"
    assert history[0]["status"] == "PASSED"


async def test_scenario_b_proves_the_conversions_differ(service: HedgeLabService) -> None:
    result = await ScenarioRunner(service).run("B")
    sizings = {
        (step.data["source_sizing"], step.data["hedge_sizing"])
        for step in result.steps if "source_sizing" in step.data
    }
    ratios = {
        step.data["conversion_ratio"] for step in result.steps
        if "conversion_ratio" in step.data
    }
    # Four pairs, four distinct sizing combinations, at least three distinct
    # ratios -- BTC and ETH coincide at 0.001 despite unrelated contract sizes.
    assert len(sizings) == 4
    assert len(ratios) >= 3


async def test_scenario_d_reaches_recovery(service: HedgeLabService) -> None:
    result = await ScenarioRunner(service).run("D")
    recovery_step = next(s for s in result.steps if s.label == "Automatic recovery")
    assert recovery_step.data["reached_recovery"] is True
    assert recovery_step.data["final_state"] == "COMPLETED"
