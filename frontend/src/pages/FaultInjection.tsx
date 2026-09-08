import { useState } from "react";
import { useAction, useQuery } from "@/hooks/useApi";
import { CONFIRM, api } from "@/lib/api";
import type { ReconciliationReport, ScenarioResult, SystemStatus } from "@/lib/api";
import { Empty, ErrorBox, Loading, Notice, Panel, RiskBadge, WarnNote } from "@/components/ui";
import { datetime } from "@/lib/format";

interface FaultsResponse {
  available: Array<{ kind: string; immediate: boolean }>;
  armed: Array<{ kind: string; venue: string | null; leg: string | null; remaining: number | null; magnitude: string; reason: string }>;
  history: Array<Record<string, unknown>>;
  venue_connectivity: Record<string, boolean>;
  active_scenarios: Record<string, string>;
}
interface ScenariosResponse { scenarios: Array<{ key: string; name: string; description: string }> }

const QUICK_FAULTS: Array<[string, string, Record<string, unknown>]> = [
  ["Simulate exchange disconnect", "DELTA_DISCONNECT", {}],
  ["Simulate broker disconnect", "MT5_DISCONNECT", {}],
  ["Simulate stale market data", "STALE_MARKET_DATA", {}],
  ["Simulate spread widening", "WIDE_SPREAD", {}],
  ["Simulate price gap (-5%)", "PRICE_GAP", { magnitude: "0.05", symbol: "BTC" }],
  ["Arm leg-1 partial fill (50%)", "LEG1_PARTIAL_FILL", { leg: "SOURCE", magnitude: "0.5" }],
  ["Arm leg-2 partial fill (50%)", "LEG2_PARTIAL_FILL", { leg: "HEDGE", magnitude: "0.5" }],
  ["Arm order rejection", "ORDER_REJECTION", { venue: "PAPER_MT5" }],
  ["Arm API timeout", "API_TIMEOUT", { venue: "PAPER_MT5", leg: "HEDGE" }],
  ["Arm high slippage", "HIGH_SLIPPAGE", { magnitude: "0.01" }],
  ["Arm duplicate execution report", "DUPLICATE_EXECUTION_REPORT", {}],
  ["Create an unexpected venue position", "UNEXPECTED_POSITION", { venue: "PAPER_MT5", symbol: "XAUUSD" }],
];

export function FaultInjection() {
  const { data: status, refresh: refreshStatus } = useQuery<SystemStatus>("/system/status", 4000);
  const { data: faults, refresh: refreshFaults } = useQuery<FaultsResponse>("/fault-injection", 3000);
  const { data: scenarios } = useQuery<ScenariosResponse>("/scenarios", 0);
  const { data: reconciliation, refresh: refreshReconciliation } =
    useQuery<ReconciliationReport>("/reconciliation", 6000);
  const { run, pending, error } = useAction();

  const [message, setMessage] = useState<string | null>(null);
  const [scenarioResult, setScenarioResult] = useState<ScenarioResult | null>(null);
  const [killReason, setKillReason] = useState("operator decision");

  const refreshAll = () => {
    refreshStatus();
    refreshFaults();
    refreshReconciliation();
  };

  const inject = async (kind: string, extra: Record<string, unknown>) => {
    const result = await run(() =>
      api.post<{ kind: string; armed: boolean; applied: boolean }>("/fault-injection", {
        kind, ...extra,
      }),
    );
    if (result) {
      setMessage(
        result.applied
          ? `${kind} applied immediately.`
          : `${kind} armed — it fires the next time that code path runs.`,
      );
      refreshAll();
    }
  };

  const clearFaults = async () => {
    const result = await run(() => api.delete<{ disarmed: number }>("/fault-injection"));
    if (result) {
      setMessage(`Cleared ${result.disarmed} armed fault(s); venues reconnected and scenario reset.`);
      refreshAll();
    }
  };

  const runScenario = async (key: string) => {
    setScenarioResult(null);
    const result = await run(() =>
      api.post<ScenarioResult>("/scenarios/run", { scenario: key }),
    );
    if (result) {
      setScenarioResult(result);
      refreshAll();
    }
  };

  const restartRecovery = async () => {
    const result = await run(() =>
      api.post<{ resumable: boolean; required_actions: string[]; notes: string[] }>(
        "/reconciliation/recover",
      ),
    );
    if (result) {
      setMessage(
        result.resumable
          ? `Recovery: safe to resume. ${result.notes.join(" ")}`
          : `Recovery: NOT safe to resume — ${result.required_actions.join("; ")}`,
      );
      refreshAll();
    }
  };

  const adopt = async () => {
    const result = await run(() =>
      api.post<{ adopted: number }>("/reconciliation/adopt", undefined, CONFIRM),
    );
    if (result) {
      setMessage(`Adopted ${result.adopted} venue position(s) into the database.`);
      refreshAll();
    }
  };

  const killSwitch = async () => {
    const result = await run(() =>
      api.post<{ engaged: boolean; flatten_result: string; orders_cancelled: number }>(
        "/emergency/kill-switch", { reason: killReason }, CONFIRM,
      ),
    );
    if (result) {
      setMessage(`Kill switch engaged: ${result.flatten_result}; ${result.orders_cancelled} order(s) cancelled.`);
      refreshAll();
    }
  };

  const clearKillSwitch = async () => {
    const result = await run(() => api.delete<{ engaged: boolean }>("/emergency/kill-switch"));
    if (result) {
      setMessage("Kill switch cleared; trading re-enabled.");
      refreshAll();
    }
  };

  if (!faults || !status) return <Loading what="fault injection" />;

  return (
    <>
      <h2 className="page-title">Fault Injection</h2>
      <p className="page-sub">
        Break the system on purpose and watch it respond. Most faults are
        <em> armed</em> and fire the next time the matching code path runs, which
        keeps them reproducible; disconnects, stale data, wide spreads and price
        gaps are states and apply immediately.
      </p>

      <ErrorBox message={error} />
      {message && <Notice>{message}</Notice>}
      {status.kill_switch_engaged && (
        <WarnNote>
          The kill switch is engaged: trading is disabled until it is cleared.
        </WarnNote>
      )}

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Panel title="Inject a fault">
          <div className="button-row">
            {QUICK_FAULTS.map(([label, kind, extra]) => (
              <button key={label} disabled={pending} onClick={() => void inject(kind, extra)}>
                {label}
              </button>
            ))}
          </div>
          <div className="button-row" style={{ marginTop: 12 }}>
            <button className="primary" disabled={pending} onClick={() => void clearFaults()}>
              Clear all faults and reconnect
            </button>
          </div>
        </Panel>

        <Panel title="Recovery and emergency controls">
          <div className="button-row">
            <button disabled={pending} onClick={() => void restartRecovery()}>
              Run restart recovery
            </button>
            <button disabled={pending} onClick={() => void refreshReconciliation()}>
              Reconcile now
            </button>
            <button disabled={pending} onClick={() => void adopt()}>
              Adopt venue state (confirmed)
            </button>
          </div>
          <div className="field" style={{ marginTop: 14 }}>
            <label>Kill switch reason (recorded in the audit trail)</label>
            <input value={killReason} onChange={(e) => setKillReason(e.target.value)} />
          </div>
          <div className="button-row">
            <button className="danger" disabled={pending} onClick={() => void killSwitch()}>
              Engage kill switch
            </button>
            <button disabled={pending} onClick={() => void clearKillSwitch()}>
              Clear kill switch
            </button>
          </div>
          <p className="small muted" style={{ marginTop: 10 }}>
            The kill switch cancels every working order, flattens every position
            and then verifies it actually reached flat — a partial fill does not
            end the attempt.
          </p>
        </Panel>
      </div>

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Panel title="Venue connectivity and scenarios">
          <dl className="kv">
            {Object.entries(faults.venue_connectivity).map(([venue, connected]) => (
              <div key={venue} style={{ display: "contents" }}>
                <dt>{venue}</dt>
                <dd className={connected ? "pos" : "neg"}>
                  {connected ? "connected" : "DISCONNECTED"}
                </dd>
              </div>
            ))}
            {Object.entries(faults.active_scenarios).map(([scope, scenario]) => (
              <div key={scope} style={{ display: "contents" }}>
                <dt>scenario · {scope}</dt>
                <dd>{scenario}</dd>
              </div>
            ))}
          </dl>
          <h4 style={{ marginBottom: 6 }}>Armed faults</h4>
          {faults.armed.length === 0 ? (
            <p className="small muted">none armed</p>
          ) : (
            <table>
              <thead><tr><th>Kind</th><th>Scope</th><th className="num">Remaining</th><th className="num">Magnitude</th></tr></thead>
              <tbody>
                {faults.armed.map((fault, index) => (
                  <tr key={`${fault.kind}-${index}`}>
                    <td className="small">{fault.kind}</td>
                    <td className="small muted">
                      {[fault.venue, fault.leg].filter(Boolean).join(" / ") || "any"}
                    </td>
                    <td className="num">{fault.remaining ?? "∞"}</td>
                    <td className="num">{fault.magnitude}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        <Panel title="Reconciliation">
          {!reconciliation ? (
            <Loading what="reconciliation" />
          ) : (
            <>
              <dl className="kv">
                <dt>Status</dt>
                <dd className={reconciliation.is_clean ? "pos" : "neg"}>
                  {reconciliation.is_clean ? "clean" : "discrepancies found"}
                </dd>
                <dt>Database positions</dt><dd>{reconciliation.database_positions}</dd>
                <dt>Venue positions</dt><dd>{reconciliation.venue_positions}</dd>
                <dt>Venues checked</dt><dd>{reconciliation.checked_venues.join(", ")}</dd>
                {reconciliation.unreachable_venues.length > 0 && (
                  <>
                    <dt>Unreachable</dt>
                    <dd className="neg">{reconciliation.unreachable_venues.join(", ")}</dd>
                  </>
                )}
              </dl>
              {reconciliation.discrepancies.length > 0 && (
                <div className="warn-note" style={{ marginTop: 12 }}>
                  {reconciliation.discrepancies.map((d, index) => (
                    <div key={index} style={{ marginBottom: 6 }}>
                      <strong>{d.issue}</strong> — {d.message}
                      <div className="small">→ {d.suggested_action}</div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </Panel>
      </div>

      <Panel title="Predefined scenarios">
        <div className="button-row" style={{ marginBottom: 12 }}>
          {(scenarios?.scenarios ?? []).map((scenario) => (
            <button
              key={scenario.key}
              disabled={pending}
              title={scenario.description}
              onClick={() => void runScenario(scenario.key)}
            >
              {scenario.key} · {scenario.name}
            </button>
          ))}
        </div>
        {pending && <p className="small muted">running…</p>}
        {scenarioResult && (
          <div>
            <h4>
              {scenarioResult.key} — {scenarioResult.name}{" "}
              <RiskBadge level={scenarioResult.passed ? "NORMAL" : "EMERGENCY"} />
            </h4>
            <p className="small">{scenarioResult.summary}</p>
            {scenarioResult.error && <div className="error">{scenarioResult.error}</div>}
            <ol className="timeline">
              {scenarioResult.steps.map((step, index) => (
                <li key={index}>
                  <span className="seq">{index + 1}</span>
                  <div style={{ flex: 1 }}>
                    <strong>{step.label}</strong>
                    <div className="small muted">{step.detail}</div>
                    {Object.keys(step.data).length > 0 && (
                      <details>
                        <summary className="small muted" style={{ cursor: "pointer" }}>
                          data
                        </summary>
                        <pre className="payload">{JSON.stringify(step.data, null, 2)}</pre>
                      </details>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </div>
        )}
      </Panel>

      <Panel title="Fault history" flush>
        {faults.history.length === 0 ? (
          <Empty what="fired faults" />
        ) : (
          <div className="table-scroll" style={{ maxHeight: 260, overflowY: "auto" }}>
            <table>
              <thead><tr><th>Kind</th><th>Venue</th><th>Symbol</th><th>Leg</th><th>Fired at</th></tr></thead>
              <tbody>
                {faults.history.slice().reverse().map((entry, index) => (
                  <tr key={index}>
                    <td className="small">{String(entry.kind)}</td>
                    <td className="small muted">{String(entry.venue ?? "—")}</td>
                    <td className="small muted">{String(entry.symbol ?? "—")}</td>
                    <td className="small muted">{String(entry.leg ?? "—")}</td>
                    <td className="small muted">{datetime(String(entry.fired_at))}</td>
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
