import { useState } from "react";
import { useQuery } from "@/hooks/useApi";
import type { Order, Position } from "@/lib/api";
import { Empty, Loading, Panel, StateBadge } from "@/components/ui";
import { datetime, decimal, money, num, quantity, signClass } from "@/lib/format";

interface OrdersResponse { count: number; orders: Order[] }
interface FillsResponse {
  count: number;
  fills: Array<{
    exec_id: string; order_id: string; quantity: string; price: string;
    fee: string; slippage: string; is_maker: boolean; timestamp: string;
  }>;
}
interface PositionsResponse { positions: Position[]; currency: string }

export function Orders() {
  const [venue, setVenue] = useState("");
  const query = venue ? `/orders?limit=200&venue=${venue}` : "/orders?limit=200";
  const { data: orders } = useQuery<OrdersResponse>(query, 3000);
  const { data: fills } = useQuery<FillsResponse>("/orders/fills?limit=200", 3000);
  const { data: positions } = useQuery<PositionsResponse>("/positions", 3000);

  if (!orders) return <Loading what="orders" />;

  return (
    <>
      <h2 className="page-title">Orders</h2>
      <p className="page-sub">
        Every paper order and fill, with the slippage each one printed against
        the reference price captured at submission.
      </p>

      <Panel title="Open positions" flush>
        {!positions || positions.positions.length === 0 ? (
          <Empty what="open positions" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Instrument</th>
                  <th>Side</th>
                  <th className="num">Quantity</th>
                  <th>Unit</th>
                  <th className="num">Entry</th>
                  <th className="num">Mark</th>
                  <th className="num">Base units</th>
                  <th className="num">Notional</th>
                  <th className="num">Unrealised</th>
                  <th className="num">Fees</th>
                  <th className="num">Net funding</th>
                </tr>
              </thead>
              <tbody>
                {positions.positions.map((position) => (
                  <tr key={position.key}>
                    <td>{position.key}</td>
                    <td className={position.side === "LONG" ? "pos" : "neg"}>{position.side}</td>
                    <td className="num">{quantity(position.quantity)}</td>
                    <td className="small muted">{position.quantity_unit}</td>
                    <td className="num">{decimal(position.average_entry, 2)}</td>
                    <td className="num">{decimal(position.mark_price, 2)}</td>
                    <td className="num">{decimal(position.base_units, 6)}</td>
                    <td className="num">{money(position.notional_account, positions.currency, 0)}</td>
                    <td className={`num ${signClass(position.unrealized_pnl)}`}>
                      {money(position.unrealized_pnl, positions.currency)}
                    </td>
                    <td className="num">{money(position.fees_paid, positions.currency)}</td>
                    <td className={`num ${signClass(num(position.funding_received) - num(position.funding_paid))}`}>
                      {money(num(position.funding_received) - num(position.funding_paid), positions.currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div style={{ margin: "16px 0 8px", display: "flex", gap: 8, alignItems: "center" }}>
        <label style={{ margin: 0 }}>Venue</label>
        <select value={venue} onChange={(e) => setVenue(e.target.value)} style={{ width: 220 }}>
          <option value="">all venues</option>
          <option value="PAPER_DELTA">PAPER_DELTA</option>
          <option value="PAPER_MT5">PAPER_MT5</option>
        </select>
      </div>

      <Panel title={`Orders (${orders.count})`} flush>
        {orders.orders.length === 0 ? (
          <Empty what="orders" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Cycle / leg</th>
                  <th>Venue</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Quantity</th>
                  <th className="num">Filled</th>
                  <th className="num">Avg price</th>
                  <th className="num">Slippage</th>
                  <th className="num">Fees</th>
                  <th>Status</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {orders.orders.map((order) => (
                  <tr key={order.order_id}>
                    <td className="mono small">{order.order_id.slice(-12)}</td>
                    <td className="small muted">
                      {order.cycle_id ? `${order.cycle_id.slice(-8)} / ${order.leg ?? "—"}` : "—"}
                    </td>
                    <td>{order.venue}</td>
                    <td>{order.symbol}</td>
                    <td className={order.side === "BUY" ? "pos" : "neg"}>{order.side}</td>
                    <td className="num">{quantity(order.quantity)}</td>
                    <td className="num">{quantity(order.filled_quantity)}</td>
                    <td className="num">{decimal(order.average_price, 2)}</td>
                    <td className={`num ${signClass(order.slippage)}`}>
                      {decimal(order.slippage, 2)}
                    </td>
                    <td className="num">{money(order.fees_paid)}</td>
                    <td>
                      <StateBadge state={order.status} />
                      {order.reject_reason && (
                        <div className="small neg">{order.reject_reason}</div>
                      )}
                    </td>
                    <td className="small muted">{datetime(order.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title={`Fills (${fills?.count ?? 0})`} flush>
        {!fills || fills.fills.length === 0 ? (
          <Empty what="fills" />
        ) : (
          <div className="table-scroll" style={{ maxHeight: 400, overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Exec ID</th>
                  <th>Order</th>
                  <th className="num">Quantity</th>
                  <th className="num">Price</th>
                  <th className="num">Fee</th>
                  <th className="num">Slippage</th>
                  <th>Liquidity</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {fills.fills.map((fill) => (
                  <tr key={fill.exec_id}>
                    <td className="mono small">{fill.exec_id.slice(-12)}</td>
                    <td className="mono small">{fill.order_id.slice(-12)}</td>
                    <td className="num">{quantity(fill.quantity)}</td>
                    <td className="num">{decimal(fill.price, 2)}</td>
                    <td className="num">{money(fill.fee)}</td>
                    <td className={`num ${signClass(fill.slippage)}`}>
                      {decimal(fill.slippage, 2)}
                    </td>
                    <td className="small muted">{fill.is_maker ? "maker" : "taker"}</td>
                    <td className="small muted">{datetime(fill.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
