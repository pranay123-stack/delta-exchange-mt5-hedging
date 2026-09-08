import { useQuery } from "@/hooks/useApi";
import type { Account, PairRisk, PortfolioRisk, StatisticsResponse } from "@/lib/api";
import { Bar, Empty, Loading, Panel, RiskBadge, Stat } from "@/components/ui";
import { bps, marginLevelClass, money, num, pct } from "@/lib/format";

interface RiskResponse { count: number; worst_level: string; pairs: PairRisk[] }
interface AccountsResponse { accounts: Account[]; currency: string }
interface RiskEventsResponse {
  events: Array<{
    level: string; scope: string; pair: string | null; metric: string;
    value: string; threshold: string; message: string; timestamp: string;
  }>;
}

export function RiskDashboard() {
  const { data: risk } = useQuery<RiskResponse>("/risk", 3000);
  const { data: portfolio } = useQuery<PortfolioRisk>("/portfolio", 3000);
  const { data: accounts } = useQuery<AccountsResponse>("/accounts", 3000);
  const { data: events } = useQuery<RiskEventsResponse>("/risk/events/log?limit=40", 6000);
  const { data: statistics } = useQuery<StatisticsResponse>("/risk/statistics", 8000);

  if (!risk || !portfolio || !accounts) return <Loading what="risk" />;
  const currency = accounts.currency;

  return (
    <>
      <h2 className="page-title">Risk Dashboard</h2>
      <p className="page-sub">
        Graded thresholds at both levels. A pair can be inside its own limits
        while the portfolio is not — five pairs each at 90% of their individual
        limit is a portfolio at 450% of what any one of them was allowed.
      </p>

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat label="Portfolio level" value={<RiskBadge level={portfolio.level} />}
              sub={portfolio.allows_new_trades ? "new hedges permitted" : "new hedges blocked"} />
        <Stat label="Worst pair" value={<RiskBadge level={risk.worst_level} />}
              sub={`${portfolio.breaching_pairs.length} of ${risk.count} breaching`} />
        <Stat label="Margin utilisation" value={pct(portfolio.margin_utilization_pct)}
              sub={<Bar pct={num(portfolio.margin_utilization_pct)} />} />
        <Stat
          label="Tightest liquidation"
          value={
            portfolio.aggregate_liquidation_risk
              ? pct(num(portfolio.aggregate_liquidation_risk) * 100)
              : "—"
          }
          sub="closest adverse move any leg survives"
        />
      </div>

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Panel title="Portfolio limits">
          <dl className="kv">
            <dt>Gross notional</dt><dd>{money(portfolio.gross_notional, currency, 0)}</dd>
            <dt>Net exposure</dt>
            <dd>{money(portfolio.net_exposure, currency)} ({pct(portfolio.net_exposure_pct)})</dd>
            <dt>Aggregate equity</dt><dd>{money(portfolio.aggregate_equity, currency, 0)}</dd>
            <dt>Aggregate margin</dt><dd>{money(portfolio.aggregate_margin, currency, 0)}</dd>
            <dt>Free margin</dt><dd>{money(portfolio.aggregate_free_margin, currency, 0)}</dd>
            <dt>Concentration</dt>
            <dd>{pct(portfolio.concentration_pct)} in {portfolio.concentration_underlying || "—"}</dd>
            <dt>Worst-case loss</dt><dd>{money(portfolio.portfolio_max_loss, currency, 0)}</dd>
            <dt>Carry per day</dt><dd>{money(portfolio.aggregate_funding_per_day, currency)}</dd>
            <dt>Fees paid</dt><dd>{money(portfolio.aggregate_fees, currency)}</dd>
          </dl>
          {portfolio.breaches.length > 0 && (
            <div className="warn-note" style={{ marginTop: 12 }}>
              {portfolio.breaches.map((b) => (
                <div key={b.metric}>
                  <strong>{b.level}</strong> {b.message}
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Exposure by dimension">
          <table>
            <thead><tr><th>Venue</th><th className="num">Exposure</th></tr></thead>
            <tbody>
              {Object.entries(portfolio.exposure_by_venue).map(([venue, value]) => (
                <tr key={venue}>
                  <td>{venue}</td>
                  <td className="num">{money(value, currency, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table style={{ marginTop: 12 }}>
            <thead><tr><th>Underlying</th><th className="num">Source exposure</th></tr></thead>
            <tbody>
              {Object.entries(portfolio.exposure_by_underlying).map(([key, value]) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td className="num">{money(value, currency, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {Object.keys(portfolio.currency_exposure).length > 0 && (
            <table style={{ marginTop: 12 }}>
              <thead><tr><th>Settlement currency</th><th className="num">Net</th></tr></thead>
              <tbody>
                {Object.entries(portfolio.currency_exposure).map(([ccy, value]) => (
                  <tr key={ccy}>
                    <td>{ccy}</td>
                    <td className="num">{Number(value).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      <Panel title="Per-pair risk" flush>
        {risk.pairs.length === 0 ? (
          <Empty what="hedge pairs with positions" />
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Level</th>
                  <th className="num">Ratio</th>
                  <th className="num">Residual</th>
                  <th className="num">Basis</th>
                  <th className="num">Margin</th>
                  <th className="num">Source liq.</th>
                  <th className="num">Hedge liq.</th>
                  <th className="num">Tolerable move</th>
                  <th>Prescribed actions</th>
                </tr>
              </thead>
              <tbody>
                {risk.pairs.map((pair) => (
                  <tr key={pair.pair}>
                    <td>{pair.pair}</td>
                    <td><RiskBadge level={pair.level} /></td>
                    <td className="num">{num(pair.hedge_ratio).toFixed(4)}</td>
                    <td className="num">{bps(pair.residual_bps)}</td>
                    <td className="num">{bps(pair.basis_bps)}</td>
                    <td className="num">{money(pair.total_margin, pair.currency, 0)}</td>
                    <td className="num">{pair.source_liquidation_price ?? "—"}</td>
                    <td className="num">{pair.hedge_liquidation_price ?? "—"}</td>
                    <td className="num">
                      {pair.max_tolerable_move
                        ? pct(num(pair.max_tolerable_move) * 100)
                        : "—"}
                    </td>
                    <td className="small">{pair.actions.join(", ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div style={{ marginTop: 16 }} />
      <Panel
        title="Measured statistics"
        flush
        actions={
          statistics ? (
            <span className="small muted">
              sampled every {statistics.sample_seconds}s, window {statistics.window},
              minimum {statistics.min_samples}
            </span>
          ) : null
        }
      >
        {!statistics ? (
          <Loading what="statistics" />
        ) : (
          <>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Pair</th>
                    <th>Source</th>
                    <th className="num">σ source /day</th>
                    <th className="num">σ hedge /day</th>
                    <th className="num">ρ</th>
                    <th className="num">β ± 1 s.e.</th>
                    <th className="num">Residual σ</th>
                    <th className="num">Samples</th>
                  </tr>
                </thead>
                <tbody>
                  {statistics.pairs.map((row) => (
                    <tr key={row.pair}>
                      <td>{row.pair}</td>
                      <td>
                        <span
                          className={`badge ${row.estimated ? "level-NORMAL" : "level-WARNING"}`}
                        >
                          {row.estimated ? "MEASURED" : "ASSUMED"}
                        </span>
                      </td>
                      <td className="num">{pct(num(row.applied.source_daily_vol) * 100, 3)}</td>
                      <td className="num">{pct(num(row.applied.hedge_daily_vol) * 100, 3)}</td>
                      <td className="num">{num(row.applied.correlation).toFixed(5)}</td>
                      <td className="num">
                        {num(row.applied.beta).toFixed(5)}
                        {row.estimated && (
                          <span className="muted">
                            {" ± "}
                            {num(row.beta_standard_error).toFixed(5)}
                          </span>
                        )}
                        {row.estimated && !row.beta_is_distinguishable_from_one && (
                          <div className="small muted">not distinct from 1</div>
                        )}
                      </td>
                      <td className="num">{pct(num(row.applied.residual_daily_vol) * 100, 4)}</td>
                      <td className="num">{row.overlapping_samples}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="panel-body small muted">
              Volatility and correlation are estimated from prices this process
              has observed, and drive the <code>RISK_WEIGHTED</code>,{" "}
              <code>FUNDING_ADJUSTED</code> and <code>COST_ADJUSTED</code>{" "}
              objectives. A pair marked <strong>ASSUMED</strong> does not yet
              have enough samples, so those objectives fall back to the
              configured defaults and say so on the calculation.{" "}
              β is the OLS slope of source returns on hedge returns; where its
              distance from 1.0 is within two standard errors it is labelled
              <em> not distinct from 1</em>, because trading on that difference
              would be trading sampling noise.
            </div>
          </>
        )}
      </Panel>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <Panel title="Venue margin" flush>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Venue</th>
                  <th className="num">Equity</th>
                  <th className="num">Used</th>
                  <th className="num">Maintenance</th>
                  <th className="num">Margin level</th>
                </tr>
              </thead>
              <tbody>
                {accounts.accounts.map((account) => (
                  <tr key={account.venue}>
                    <td>{account.venue}</td>
                    <td className="num">{money(account.equity, account.currency, 0)}</td>
                    <td className="num">{money(account.used_margin, account.currency, 0)}</td>
                    <td className="num">{money(account.maintenance_margin, account.currency, 0)}</td>
                    <td className={`num ${marginLevelClass(account.margin_level)}`}>
                      {num(account.margin_level) >= 99_999 ? "—" : pct(account.margin_level)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Recorded risk events" flush>
          {!events || events.events.length === 0 ? (
            <Empty what="risk events" />
          ) : (
            <div className="table-scroll" style={{ maxHeight: 320, overflowY: "auto" }}>
              <table>
                <thead>
                  <tr><th>Level</th><th>Metric</th><th>Message</th></tr>
                </thead>
                <tbody>
                  {events.events.map((event, index) => (
                    <tr key={`${event.timestamp}-${index}`}>
                      <td><RiskBadge level={event.level} /></td>
                      <td className="mono small">{event.metric}</td>
                      <td className="small">{event.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
