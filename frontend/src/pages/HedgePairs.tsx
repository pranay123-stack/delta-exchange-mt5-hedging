import { useState } from "react";
import { useAction, useQuery } from "@/hooks/useApi";
import { api } from "@/lib/api";
import type { Mapping, PairRisk, PnLBreakdown, Position } from "@/lib/api";
import { Empty, ErrorBox, Loading, Panel, RiskBadge } from "@/components/ui";
import { bps, money, num, quantity, ratio, signClass } from "@/lib/format";

interface MappingsResponse { count: number; mappings: Mapping[] }
interface RiskResponse { pairs: PairRisk[] }
interface PnLResponse { pairs: PnLBreakdown[]; currency: string }
interface PositionsResponse { positions: Position[]; currency: string }

export function HedgePairs() {
  const { data: mappings } = useQuery<MappingsResponse>("/instrument-mappings", 10000);
  const { data: risk, refresh: refreshRisk } = useQuery<RiskResponse>("/risk", 4000);
  const { data: pnl, refresh: refreshPnl } = useQuery<PnLResponse>("/pnl", 6000);
  const { data: positions, refresh: refreshPositions } =
    useQuery<PositionsResponse>("/positions", 4000);
  const { run, pending, error } = useAction();
  const [message, setMessage] = useState<string | null>(null);
  const [busyPair, setBusyPair] = useState<string | null>(null);

  const riskByPair = new Map((risk?.pairs ?? []).map((r) => [r.pair, r]));
  const pnlByPair = new Map((pnl?.pairs ?? []).map((p) => [p.pair, p]));
  const positionByKey = new Map((positions?.positions ?? []).map((p) => [p.key, p]));

  const refreshAll = () => {
    refreshRisk();
    refreshPnl();
    refreshPositions();
  };

  const hedgeNow = async (mapping: Mapping) => {
    setBusyPair(mapping.name);
    const result = await run(() =>
      api.post<{ succeeded: boolean; cycle: { state: string }; messages: string[] }>(
        "/hedge/execute",
        { mapping_name: mapping.name, source_quantity: "0", reason: "dashboard" },
      ),
    );
    setBusyPair(null);
    if (result) {
      setMessage(
        `${mapping.name}: cycle ended ${result.cycle.state}` +
          (result.messages.length ? ` — ${result.messages.join("; ")}` : ""),
      );
      refreshAll();
    }
  };

  const rebalance = async (mapping: Mapping) => {
    setBusyPair(mapping.name);
    const result = await run(() =>
      api.post<{ rebalanced: boolean; assessment?: { reason: string }; after?: { residual_bps: string } }>(
        `/hedge/rebalance/${encodeURIComponent(mapping.name)}`,
      ),
    );
    setBusyPair(null);
    if (result) {
      setMessage(
        result.rebalanced
          ? `${mapping.name}: rebalanced, residual now ${result.after?.residual_bps} bps`
          : `${mapping.name}: ${result.assessment?.reason ?? "no rebalance needed"}`,
      );
      refreshAll();
    }
  };

  if (!mappings) return <Loading what="hedge pairs" />;

  return (
    <>
      <h2 className="page-title">Hedge Pairs</h2>
      <p className="page-sub">
        Each pair maps a source instrument to the instrument that hedges it. The
        conversion line shows what one source unit is actually worth in hedge
        units — this is where <code>1 contract ≠ 1 lot</code> is resolved.
      </p>

      <ErrorBox message={error} />
      {message && <div className="notice">{message}</div>}

      <div className="grid" style={{ gap: 16 }}>
        {mappings.mappings.map((mapping) => {
          const pairRisk = riskByPair.get(mapping.name);
          const pairPnl = pnlByPair.get(mapping.name);
          const sourcePosition = positionByKey.get(mapping.source_key);
          const hedgePosition = positionByKey.get(mapping.hedge_key);
          const currency = pairPnl?.currency ?? "USD";

          return (
            <Panel
              key={mapping.key}
              title={mapping.name}
              actions={
                pairRisk ? <RiskBadge level={pairRisk.level} /> : null
              }
            >
              <div className="small muted mono" style={{ marginBottom: 12 }}>
                {mapping.conversion}
              </div>

              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Leg</th>
                      <th>Instrument</th>
                      <th className="num">Position</th>
                      <th className="num">Entry</th>
                      <th className="num">Mark</th>
                      <th className="num">Base units</th>
                      <th className="num">Notional</th>
                      <th className="num">Unrealised</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["Source", mapping.source_key, sourcePosition, mapping.source_sizing],
                      ["Hedge", mapping.hedge_key, hedgePosition, mapping.hedge_sizing],
                    ].map(([label, key, position, sizing]) => (
                      <tr key={String(key)}>
                        <td>{label as string}</td>
                        <td>
                          {String(key)}
                          <div className="small muted">{sizing as string}</div>
                        </td>
                        <td className="num">
                          {position ? quantity((position as Position).quantity) : "—"}
                        </td>
                        <td className="num">
                          {position ? Number((position as Position).average_entry).toFixed(2) : "—"}
                        </td>
                        <td className="num">
                          {position ? Number((position as Position).mark_price).toFixed(2) : "—"}
                        </td>
                        <td className="num">
                          {position ? Number((position as Position).base_units).toFixed(6) : "—"}
                        </td>
                        <td className="num">
                          {position
                            ? money((position as Position).notional_account, currency, 0)
                            : "—"}
                        </td>
                        <td className={`num ${position ? signClass((position as Position).unrealized_pnl) : ""}`}>
                          {position ? money((position as Position).unrealized_pnl, currency) : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <dl className="kv" style={{ marginTop: 14 }}>
                <dt>Objective</dt>
                <dd>{mapping.objective} @ ratio {mapping.target_ratio}</dd>
                <dt>Hedge ratio</dt>
                <dd>{pairRisk ? ratio(pairRisk.hedge_ratio) : "—"}</dd>
                <dt>Residual exposure</dt>
                <dd>
                  {pairRisk ? `${bps(pairRisk.residual_bps)} (tolerance ${mapping.tolerance_bps} bps)` : "—"}
                </dd>
                <dt>Cross-venue basis</dt>
                <dd>{pairRisk ? bps(pairRisk.basis_bps) : "—"}</dd>
                <dt>Funding / swap per day</dt>
                <dd className={pairRisk ? signClass(pairRisk.funding_risk_per_day) : ""}>
                  {pairRisk ? money(pairRisk.funding_risk_per_day, currency) : "—"}
                </dd>
                <dt>Fees + spread + slippage</dt>
                <dd className={pairPnl ? signClass(pairPnl.total_costs) : ""}>
                  {pairPnl ? money(pairPnl.total_costs, currency) : "—"}
                </dd>
                <dt>Net P&amp;L</dt>
                <dd className={pairPnl ? signClass(pairPnl.net_pnl) : ""}>
                  {pairPnl ? money(pairPnl.net_pnl, currency) : "—"}
                </dd>
                <dt>Margin in use</dt>
                <dd>{pairRisk ? money(pairRisk.total_margin, currency, 0) : "—"}</dd>
                <dt>Liquidation distance</dt>
                <dd>
                  {pairRisk?.max_tolerable_move
                    ? `${(num(pairRisk.max_tolerable_move) * 100).toFixed(2)}%`
                    : "—"}
                </dd>
              </dl>

              {pairRisk && pairRisk.breaches.length > 0 && (
                <div className="warn-note" style={{ marginTop: 12 }}>
                  {pairRisk.breaches.map((breach) => (
                    <div key={breach.metric}>• {breach.message}</div>
                  ))}
                </div>
              )}

              <div className="button-row" style={{ marginTop: 12 }}>
                <button
                  className="primary"
                  disabled={pending && busyPair === mapping.name}
                  onClick={() => void hedgeNow(mapping)}
                >
                  Hedge existing position
                </button>
                <button
                  disabled={pending && busyPair === mapping.name}
                  onClick={() => void rebalance(mapping)}
                >
                  Rebalance
                </button>
              </div>
            </Panel>
          );
        })}
      </div>

      {mappings.mappings.length === 0 && <Empty what="configured hedge pairs" />}
    </>
  );
}
