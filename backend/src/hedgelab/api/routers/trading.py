"""Orders, fills, positions and account state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...domain.quantity import QuantityConverter
from ...venues.base import VenueError
from ..deps import Service

router = APIRouter(tags=["trading"])


@router.get("/orders", summary="Order history")
async def list_orders(
    service: Service, limit: int = 100, venue: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    from ...db.repositories import OrderRepository

    async with service.database.session() as session:
        rows = await OrderRepository(session).list(limit=limit, venue=venue, cycle_id=cycle_id)
        return {
            "count": len(rows),
            "orders": [
                {
                    "order_id": r.order_id, "client_order_id": r.client_order_id,
                    "cycle_id": r.cycle_id, "leg": r.leg, "venue": r.venue,
                    "symbol": r.symbol, "side": r.side, "order_type": r.order_type,
                    "quantity": str(r.quantity),
                    "price": str(r.price) if r.price is not None else None,
                    "status": r.status, "filled_quantity": str(r.filled_quantity),
                    "average_price": str(r.average_price),
                    "reference_price": str(r.reference_price),
                    "slippage": str(r.average_price - r.reference_price)
                    if r.reference_price else "0",
                    "fees_paid": str(r.fees_paid), "reject_reason": r.reject_reason,
                    "is_paper": r.is_paper,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }


@router.get("/orders/fills", summary="Fill history")
async def list_fills(
    service: Service, limit: int = 200, order_id: str | None = None
) -> dict[str, Any]:
    from ...db.repositories import OrderRepository

    async with service.database.session() as session:
        rows = await OrderRepository(session).fills(limit=limit, order_id=order_id)
        return {
            "count": len(rows),
            "fills": [
                {
                    "exec_id": r.exec_id, "order_id": r.order_id,
                    "quantity": str(r.quantity), "price": str(r.price),
                    "fee": str(r.fee), "slippage": str(r.slippage),
                    "is_maker": r.is_maker, "timestamp": r.timestamp.isoformat(),
                }
                for r in rows
            ],
        }


@router.get(
    "/positions",
    summary="Live positions across both venues",
    description="Includes the economic exposure each position represents, "
                "not just its quantity in venue units.",
)
async def list_positions(service: Service) -> dict[str, Any]:
    account_currency = service.settings.account_currency
    payload: list[dict[str, Any]] = []
    for position in await service.positions():
        spec = service.registry.find(position.key)
        if spec is None:
            payload.append({
                "venue": position.venue, "symbol": position.symbol,
                "quantity": str(position.quantity),
                "warning": "instrument is not in the registry",
            })
            continue
        ticker = service.simulator.ticker(spec.key)
        converter = QuantityConverter(spec)
        fx_rate = service.fx.try_rate(spec.quote_asset, account_currency)
        exposure = converter.exposure(
            position.quantity, ticker.mid, account_currency=account_currency,
            fx_rate=fx_rate, entry_price=position.average_entry or None,
        )
        unrealized = (
            position.unrealized_pnl_inverse(ticker.mid, spec.units_per_quantity)
            if spec.is_inverse
            else position.unrealized_pnl_linear(ticker.mid, spec.units_per_quantity)
        )
        payload.append({
            "venue": position.venue, "symbol": position.symbol, "key": position.key,
            "side": position.side.value, "quantity": str(position.quantity),
            "quantity_unit": spec.quantity_unit.value,
            "sizing": spec.describe_sizing(),
            "average_entry": str(position.average_entry),
            "mark_price": str(ticker.mid),
            "base_units": str(exposure.base_units),
            "notional_quote": str(exposure.notional_quote),
            "notional_account": str(exposure.notional_account),
            "quote_delta": str(exposure.quote_delta),
            "account_delta": str(exposure.account_delta),
            "unrealized_pnl": str(unrealized * fx_rate),
            "realized_pnl": str(position.realized_pnl * fx_rate),
            "funding_paid": str(position.funding_paid),
            "funding_received": str(position.funding_received),
            "fees_paid": str(position.fees_paid),
            "is_paper": True,
            "updated_at": position.updated_at.isoformat(),
        })
    return {"count": len(payload), "currency": account_currency, "positions": payload}


@router.get("/accounts", summary="Balance, equity and margin per venue")
async def accounts(service: Service) -> dict[str, Any]:
    snapshots = await service.accounts()
    return {
        "currency": service.settings.account_currency,
        "accounts": [
            {
                "venue": a.venue, "currency": a.currency, "balance": str(a.balance),
                "equity": str(a.equity), "used_margin": str(a.used_margin),
                "free_margin": str(a.free_margin), "margin_level": str(a.margin_level),
                "maintenance_margin": str(a.maintenance_margin),
                "unrealized_pnl": str(a.unrealized_pnl),
                "realized_pnl": str(a.realized_pnl),
                "fees_paid": str(a.fees_paid), "funding_net": str(a.funding_net),
                "open_positions": a.open_positions,
                "margin_utilization_pct": str(a.margin_utilization_pct),
                "is_connected": a.is_connected,
                "timestamp": a.timestamp.isoformat(),
            }
            for a in snapshots
        ],
        "totals": {
            "equity": str(sum(a.equity for a in snapshots)),
            "used_margin": str(sum(a.used_margin for a in snapshots)),
            "free_margin": str(sum(a.free_margin for a in snapshots)),
            "unrealized_pnl": str(sum(a.unrealized_pnl for a in snapshots)),
        },
    }


@router.get("/market-data", summary="Current top of book for every instrument")
async def market_data(service: Service, venue: str | None = None) -> dict[str, Any]:
    tickers = service.tickers(venue)
    return {
        "count": len(tickers),
        "clock": service.simulator.clock.isoformat(),
        "scenarios": service.simulator.active_scenarios(),
        "tickers": [
            {
                "key": key, "venue": t.venue, "symbol": t.symbol,
                "bid": str(t.bid), "ask": str(t.ask), "mid": str(t.mid),
                "last": str(t.last), "spread": str(t.spread),
                "spread_bps": str(t.spread_bps), "volume": str(t.volume),
                "bid_size": str(t.bid_size), "ask_size": str(t.ask_size),
                "funding_rate": str(t.funding_rate) if t.funding_rate is not None else None,
                "next_funding_time": t.next_funding_time.isoformat()
                if t.next_funding_time else None,
                "is_stale": t.is_stale, "sequence": t.sequence,
                "timestamp": t.timestamp.isoformat(),
            }
            for key, t in sorted(tickers.items())
        ],
    }


@router.get("/market-data/{key:path}/book", summary="Order book depth")
async def orderbook(key: str, service: Service, depth: int = 10) -> dict[str, Any]:
    if service.registry.find(key) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown instrument {key!r}")
    try:
        book = service.simulator.orderbook(key, depth)
    except VenueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {
        "key": key, "venue": book.venue, "symbol": book.symbol,
        "timestamp": book.timestamp.isoformat(),
        "bids": [{"price": str(lv.price), "size": str(lv.size)} for lv in book.bids],
        "asks": [{"price": str(lv.price), "size": str(lv.size)} for lv in book.asks],
    }
