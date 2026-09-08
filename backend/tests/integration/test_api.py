"""API surface tests, run against the real app over an in-process ASGI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hedgelab.api.app import create_app
from hedgelab.config import Settings

D = Decimal
SPEC_DIR = Path(__file__).resolve().parents[2] / "src" / "hedgelab" / "instruments" / "specs"


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        instrument_spec_dir=SPEC_DIR,
        simulator_seed=987654,
        environment="test",
        simulator_tick_ms=50_000,     # freeze the background loop's real-time cadence
        paper_starting_balance=D("500000"),
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        yield http


# ======================================================================
# system
# ======================================================================
async def test_root_advertises_paper_only(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["paper_only"] is True
    assert response.json()["trading_mode"] == "PAPER"


async def test_health_and_readiness(client: AsyncClient) -> None:
    health = await client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["paper_only"] is True

    ready = await client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["database"] is True


async def test_every_response_carries_a_correlation_id(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.headers["X-Correlation-ID"]
    assert response.headers["X-Paper-Only"] == "true"


async def test_incoming_correlation_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/api/health", headers={"X-Correlation-ID": "abc123"})
    assert response.headers["X-Correlation-ID"] == "abc123"


async def test_status_reports_no_live_adapters(client: AsyncClient) -> None:
    payload = (await client.get("/api/system/status")).json()
    assert payload["paper_only"] is True
    assert payload["live_adapters_registered"] == 0
    assert set(payload["market_scenarios"]) >= {"GLOBAL"}


async def test_openapi_is_generated(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert schema["info"]["version"]
    assert "/api/hedge/calculate" in schema["paths"]
    assert len(schema["paths"]) > 40


# ======================================================================
# reference data
# ======================================================================
async def test_instruments_expose_their_sizing(client: AsyncClient) -> None:
    payload = (await client.get("/api/instruments")).json()
    assert payload["count"] >= 10
    by_key = {i["key"]: i for i in payload["instruments"]}
    assert by_key["PAPER_DELTA:BTCUSDT-PERP"]["units_per_quantity"] == "0.001"
    assert by_key["PAPER_MT5:XAUUSD"]["sizing_description"] == "1 lot = 100 XAU"
    assert by_key["PAPER_DELTA:BTCUSD-PERP-INV"]["is_inverse"] is True


async def test_unknown_instrument_is_404(client: AsyncClient) -> None:
    assert (await client.get("/api/instruments/NOPE:NOPE")).status_code == 404


async def test_mappings_describe_the_conversion(client: AsyncClient) -> None:
    payload = (await client.get("/api/instrument-mappings")).json()
    assert payload["count"] >= 5
    for mapping in payload["mappings"]:
        assert "=" in mapping["conversion"]


async def test_a_new_instrument_can_be_added_through_the_api(client: AsyncClient) -> None:
    """The generic-instrument guarantee, over HTTP."""
    spec = {
        "symbol": "SILVER-H7", "venue": "PAPER_MT5", "venue_kind": "MT5_BROKER",
        "instrument_type": "CFD", "base_asset": "XAG", "quote_asset": "USD",
        "settlement_asset": "USD", "underlying_key": "XAG",
        "quantity_unit": "LOT", "contract_size": "5000", "units_per_lot": "5000",
        "tick_size": "0.001", "min_quantity": "0.01", "quantity_step": "0.01",
        "max_quantity": "100", "price_precision": 3, "quantity_precision": 2,
        "max_leverage": "50", "margin_model": "BROKER_LEVERAGE",
        "funding_model": "SWAP_POINTS", "swap_long_points": "-12",
        "swap_short_points": "3",
    }
    created = await client.post("/api/instruments", json=spec)
    assert created.status_code == 200, created.text
    assert created.json()["sizing_description"] == "1 lot = 5000 XAG"

    fetched = await client.get("/api/instruments/PAPER_MT5:SILVER-H7")
    assert fetched.status_code == 200

    # It has a live price immediately, with no simulator change.
    market = (await client.get("/api/market-data")).json()
    keys = {t["key"] for t in market["tickers"]}
    assert "PAPER_MT5:SILVER-H7" in keys

    change_log = (await client.get("/api/audit/configuration-changes")).json()
    assert any(c["entity_id"] == "PAPER_MT5:SILVER-H7" for c in change_log["changes"])


async def test_invalid_instrument_is_rejected_with_detail(client: AsyncClient) -> None:
    bad = {"symbol": "BAD", "venue": "PAPER_MT5", "venue_kind": "MT5_BROKER",
           "instrument_type": "CFD", "base_asset": "X", "quote_asset": "USD",
           "settlement_asset": "USD", "quantity_unit": "LOT",
           "min_quantity": "0.05", "quantity_step": "0.03"}
    response = await client.post("/api/instruments", json=bad)
    assert response.status_code == 422
    assert "multiple of" in response.text


async def test_instrument_in_use_cannot_be_deleted(client: AsyncClient) -> None:
    response = await client.delete("/api/instruments/PAPER_MT5:BTCUSD")
    assert response.status_code == 409
    assert "hedge pair" in response.json()["detail"]


# ======================================================================
# hedge calculation
# ======================================================================
async def test_calculate_returns_the_full_derivation(client: AsyncClient) -> None:
    response = await client.post("/api/hedge/calculate", json={
        "source_key": "PAPER_DELTA:BTCUSDT-PERP",
        "hedge_key": "PAPER_MT5:BTCUSD",
        "source_quantity": "5000",
        "objective": "QUOTE_PNL_NEUTRAL",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["rounded_quantity"] == "-5"
    assert Decimal(payload["margin_requirement"]) > 0
    assert len(payload["steps"]) >= 9
    assert all(step["formula"] for step in payload["steps"])


@pytest.mark.parametrize("objective", [
    "BASE_ASSET_NEUTRAL", "NOTIONAL_NEUTRAL", "QUOTE_PNL_NEUTRAL",
    "ACCOUNT_CCY_PNL_NEUTRAL", "CUSTOM_RATIO", "FUNDING_ADJUSTED",
    "COST_ADJUSTED", "RISK_WEIGHTED", "PARTIAL",
])
async def test_every_objective_is_reachable_over_http(
    client: AsyncClient, objective: str
) -> None:
    response = await client.post("/api/hedge/calculate", json={
        "source_key": "PAPER_DELTA:BTCUSDT-PERP",
        "hedge_key": "PAPER_MT5:BTCUSD",
        "source_quantity": "5000",
        "objective": objective,
        "target_ratio": "0.5" if objective == "PARTIAL" else "1",
    })
    assert response.status_code == 200, response.text
    assert response.json()["objective"] == objective


async def test_calculate_rejects_an_unknown_instrument(client: AsyncClient) -> None:
    response = await client.post("/api/hedge/calculate", json={
        "source_key": "PAPER_DELTA:NOPE", "hedge_key": "PAPER_MT5:BTCUSD",
        "source_quantity": "1",
    })
    assert response.status_code == 404


async def test_optimize_returns_scored_candidates(client: AsyncClient) -> None:
    response = await client.post("/api/hedge/optimize", json={
        "source_key": "PAPER_DELTA:ETHUSDT-PERP",
        "hedge_key": "PAPER_MT5:ETHUSD",
        "source_quantity": "3737",
        "objective": "BASE_ASSET_NEUTRAL",
        "mode": "WEIGHTED", "search_steps": 3,
    })
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["candidates"]) == 7
    assert payload["rationale"]
    assert payload["calculation"]["rounded_quantity"] == payload["chosen_quantity"]


async def test_optimize_rejects_a_bad_priority(client: AsyncClient) -> None:
    response = await client.post("/api/hedge/optimize", json={
        "source_key": "PAPER_DELTA:BTCUSDT-PERP", "hedge_key": "PAPER_MT5:BTCUSD",
        "source_quantity": "1000", "priority": "MAXIMISE_VIBES",
    })
    assert response.status_code == 422


# ======================================================================
# execution
# ======================================================================
async def test_full_hedge_cycle_over_http(client: AsyncClient) -> None:
    opened = await client.post("/api/hedge/open-position", json={
        "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "5000",
    })
    assert opened.status_code == 200
    assert opened.json()["is_paper"] is True

    executed = await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "0",
    })
    assert executed.status_code == 200, executed.text
    payload = executed.json()
    assert payload["succeeded"] is True
    assert payload["cycle"]["state"] == "COMPLETED"

    cycle_id = payload["cycle"]["cycle_id"]
    detail = (await client.get(f"/api/hedge/cycles/{cycle_id}")).json()
    assert len(detail["events"]) >= 8
    assert detail["events"][0]["to_state"] == "VALIDATED"
    assert detail["events"][-1]["to_state"] == "COMPLETED"

    positions = (await client.get("/api/positions")).json()
    keys = {p["key"] for p in positions["positions"]}
    assert {"PAPER_DELTA:BTCUSDT-PERP", "PAPER_MT5:BTCUSD"} <= keys
    assert all(p["is_paper"] for p in positions["positions"])

    orders = (await client.get("/api/orders")).json()
    assert orders["count"] >= 2
    fills = (await client.get("/api/orders/fills")).json()
    assert fills["count"] >= 2


async def test_execute_unknown_pair_is_404(client: AsyncClient) -> None:
    response = await client.post("/api/hedge/execute", json={
        "mapping_name": "nope", "source_quantity": "1",
    })
    assert response.status_code == 404


async def test_state_machine_graph_is_exposed(client: AsyncClient) -> None:
    payload = (await client.get("/api/hedge/state-machine")).json()
    assert payload["problems"] == []
    assert "COMPLETED" in payload["terminal"]
    assert "LEG_1_FILLED" in payload["exposed"]


# ======================================================================
# risk, portfolio, P&L
# ======================================================================
async def test_risk_and_portfolio_endpoints(client: AsyncClient) -> None:
    await client.post("/api/hedge/open-position", json={
        "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "3000"})
    await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "0"})

    risk = (await client.get("/api/risk")).json()
    assert risk["count"] >= 1
    btc = next(p for p in risk["pairs"] if p["pair"] == "BTC perp -> BTC CFD")
    assert Decimal(btc["residual_bps"]) < 50
    assert btc["is_tradable"] is True

    portfolio = (await client.get("/api/portfolio")).json()
    assert Decimal(portfolio["gross_notional"]) > 0
    assert portfolio["allows_new_trades"] is True

    pnl = (await client.get("/api/pnl")).json()
    btc_pnl = next(p for p in pnl["pairs"] if p["pair"] == "BTC perp -> BTC CFD")
    assert btc_pnl["consistent"] is True

    funding = (await client.get("/api/funding?horizon_days=7")).json()
    assert any(p["pair"] == "BTC perp -> BTC CFD" for p in funding["pairs"])


async def test_risk_snapshot_persists(client: AsyncClient) -> None:
    await client.post("/api/hedge/open-position", json={
        "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "1000"})
    written = (await client.post("/api/risk/snapshot")).json()
    assert written["margin_snapshots"] == 2
    history = (await client.get("/api/margin/history")).json()
    assert history["count"] >= 2


async def test_thresholds_can_be_updated_and_are_audited(client: AsyncClient) -> None:
    response = await client.patch("/api/risk/thresholds",
                                  json={"warning_residual_bps": "12"})
    assert response.status_code == 200
    assert response.json()["warning_residual_bps"] == "12"
    configs = (await client.get("/api/hedge-configs")).json()
    assert configs["risk_thresholds"]["warning_residual_bps"] == "12"
    changes = (await client.get("/api/audit/configuration-changes")).json()
    assert any(c["entity"] == "risk_thresholds" for c in changes["changes"])


async def test_funding_settlement_records_accruals(client: AsyncClient) -> None:
    await client.post("/api/hedge/open-position", json={
        "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "2000"})
    await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "0"})
    settled = (await client.post("/api/funding/settle", json={})).json()
    assert settled["count"] >= 2
    kinds = {a["kind"] for a in settled["accruals"]}
    assert kinds == {"FUNDING", "SWAP"}
    history = (await client.get("/api/funding/history")).json()
    assert history["count"] >= 2


# ======================================================================
# simulation and faults
# ======================================================================
async def test_simulation_controls(client: AsyncClient) -> None:
    state = (await client.get("/api/paper/simulation")).json()
    assert len(state["available_scenarios"]) == 7

    before = (await client.get("/api/market-data")).json()["clock"]
    await client.post("/api/paper/simulation/advance", json={"steps": 5})
    after = (await client.get("/api/market-data")).json()["clock"]
    assert after > before

    widened = await client.post("/api/paper/simulation/scenario",
                                json={"scenario": "SPREAD_WIDENING"})
    assert widened.status_code == 200
    market = (await client.get("/api/market-data")).json()
    btc = next(t for t in market["tickers"] if t["key"] == "PAPER_MT5:BTCUSD")
    assert Decimal(btc["spread_bps"]) > 50
    await client.delete("/api/paper/simulation/scenario")


async def test_shock_moves_the_price(client: AsyncClient) -> None:
    response = await client.post("/api/paper/simulation/shock",
                                 json={"underlying": "BTC", "pct_move": "-0.05"})
    assert response.status_code == 200
    assert Decimal(response.json()["price"]) > 0


async def test_unknown_underlying_shock_is_404(client: AsyncClient) -> None:
    response = await client.post("/api/paper/simulation/shock",
                                 json={"underlying": "NOPE", "pct_move": "0.1"})
    assert response.status_code == 404


async def test_fault_injection_lifecycle(client: AsyncClient) -> None:
    listing = (await client.get("/api/fault-injection")).json()
    assert len(listing["available"]) == 14

    armed = await client.post("/api/fault-injection", json={
        "kind": "ORDER_REJECTION", "venue": "PAPER_MT5", "count": 1,
    })
    assert armed.status_code == 200
    assert armed.json()["armed"] is True

    after = (await client.get("/api/fault-injection")).json()
    assert len(after["armed"]) == 1

    cleared = (await client.delete("/api/fault-injection")).json()
    assert cleared["disarmed"] == 1


async def test_disconnect_fault_applies_immediately(client: AsyncClient) -> None:
    response = await client.post("/api/fault-injection", json={"kind": "MT5_DISCONNECT"})
    assert response.json()["applied"] is True
    ready = (await client.get("/api/ready")).json()
    assert ready["ready"] is False
    assert ready["venues"]["PAPER_MT5"] is False

    await client.post("/api/fault-injection/reconnect/PAPER_MT5")
    assert (await client.get("/api/ready")).json()["ready"] is True


async def test_unexpected_position_is_caught_by_reconciliation(client: AsyncClient) -> None:
    clean = (await client.get("/api/reconciliation")).json()
    assert clean["is_clean"] is True

    await client.post("/api/fault-injection", json={
        "kind": "UNEXPECTED_POSITION", "venue": "PAPER_MT5", "symbol": "XAUUSD"})
    dirty = (await client.get("/api/reconciliation")).json()
    assert dirty["is_clean"] is False
    assert dirty["issue_counts"]["UNKNOWN_POSITION"] == 1
    assert dirty["has_critical"] is True


# ======================================================================
# emergency
# ======================================================================
async def test_kill_switch_requires_confirmation(client: AsyncClient) -> None:
    response = await client.post("/api/emergency/kill-switch",
                                 json={"reason": "testing"})
    assert response.status_code == 428
    assert "X-Confirm-Action" in response.json()["detail"]


async def test_kill_switch_flattens_and_blocks_trading(client: AsyncClient) -> None:
    await client.post("/api/hedge/open-position", json={
        "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "2000"})
    await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "0"})

    engaged = await client.post(
        "/api/emergency/kill-switch", json={"reason": "integration test"},
        headers={"X-Confirm-Action": "CONFIRM"},
    )
    assert engaged.status_code == 200
    assert engaged.json()["engaged"] is True

    positions = (await client.get("/api/positions")).json()
    assert positions["count"] == 0

    blocked = await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "1000"})
    assert blocked.status_code == 409
    assert "kill switch" in blocked.json()["detail"]

    actions = (await client.get("/api/emergency/actions")).json()
    assert {a["action"] for a in actions["actions"]} >= {
        "STOP_NEW_TRADES", "FLATTEN_POSITIONS", "CANCEL_OPEN_ORDERS"}

    cleared = await client.delete("/api/emergency/kill-switch")
    assert cleared.json()["engaged"] is False

    resumed = await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "1000"})
    assert resumed.status_code == 200


async def test_adopt_requires_confirmation(client: AsyncClient) -> None:
    assert (await client.post("/api/reconciliation/adopt")).status_code == 428
    confirmed = await client.post("/api/reconciliation/adopt",
                                  headers={"X-Confirm-Action": "CONFIRM"})
    assert confirmed.status_code == 200


# ======================================================================
# audit
# ======================================================================
async def test_audit_trail_records_execution(client: AsyncClient) -> None:
    await client.post("/api/hedge/open-position", json={
        "instrument_key": "PAPER_DELTA:BTCUSDT-PERP", "quantity": "1000"})
    await client.post("/api/hedge/execute", json={
        "mapping_name": "BTC perp -> BTC CFD", "source_quantity": "0"})
    audit = (await client.get("/api/audit")).json()
    assert any(e["action"] == "EXECUTE_HEDGE" for e in audit["entries"])
    assert all(e["timestamp"] for e in audit["entries"])


async def test_system_events_are_recorded(client: AsyncClient) -> None:
    events = (await client.get("/api/audit/system-events")).json()
    assert any(e["kind"] == "REFERENCE_DATA_SYNC" for e in events["events"])


# ======================================================================
# route shadowing
# ======================================================================
async def test_literal_risk_routes_are_not_shadowed(client: AsyncClient) -> None:
    """FastAPI matches in registration order.

    ``/risk/{mapping_name}`` declared before its literal siblings swallowed
    them: ``/api/risk/statistics`` returned 404 "unknown hedge mapping named
    'statistics'". Every literal path under /risk must resolve to its own
    handler.
    """
    for path in ("/api/risk/statistics", "/api/risk/events/log"):
        response = await client.get(path)
        assert response.status_code == 200, f"{path} -> {response.text}"
        assert "unknown hedge mapping" not in response.text

    # The parameterised route still works for a real pair, and still 404s for
    # a name that is not one.
    named = await client.get("/api/risk/BTC%20perp%20-%3E%20BTC%20CFD")
    assert named.status_code == 200
    assert named.json()["pair"] == "BTC perp -> BTC CFD"
    assert (await client.get("/api/risk/not-a-pair")).status_code == 404


async def test_statistics_endpoint_reports_provenance(client: AsyncClient) -> None:
    payload = (await client.get("/api/risk/statistics")).json()
    assert payload["min_samples"] > 0
    assert payload["pairs"]
    for pair in payload["pairs"]:
        # Before any data the estimate must declare itself an assumption
        # rather than presenting the default as a measurement.
        assert "provenance" in pair
        assert isinstance(pair["estimated"], bool)
        assert "beta" in pair["applied"]
