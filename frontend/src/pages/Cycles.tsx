import { useState } from "react";
import { useQuery } from "@/hooks/useApi";
import type { HedgeCycle } from "@/lib/api";
import { Empty, Loading, Panel, StateBadge } from "@/components/ui";
import { datetime, num } from "@/lib/format";

interface CyclesResponse { count: number; cycles: HedgeCycle[] }
interface StateMachineResponse {
  states: string[];
  terminal: string[];
  exposed: string[];
  transitions: Record<string, string[]>;
  problems: string[];
}

export function Cycles() {
  const { data: cycles } = useQuery<CyclesResponse>("/hedge/cycles?limit=60", 3000);
  const { data: graph } = useQuery<StateMachineResponse>("/hedge/state-machine", 0);
  const [selected, setSelected] = useState<string | null>(null);
  const { data: detail } = useQuery<HedgeCycle>(
    selected ? `/hedge/cycles/${selected}` : null,
    0,
  );

  if (!cycles) return <Loading what="hedge cycles" />;

  return (
    <>
      <h2 className="page-title">Hedge Cycles</h2>
      <p className="page-sub">
        Every two-leg execution and the full state machine it walked. Each
        transition is written before the side effect it describes is attempted,
        which is what lets restart recovery tell "submitted but unknown" from
        "never submitted".
      </p>

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Panel title={`Cycles (${cycles.count})`} flush>
          {cycles.cycles.length === 0 ? (
            <Empty what="hedge cycles" />
          ) : (
            <div className="table-scroll" style={{ maxHeight: 460, overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th>Cycle</th>
                    <th>Pair</th>
                    <th>State</th>
                    <th className="num">Ratio</th>
                    <th className="num">Residual</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {cycles.cycles.map((cycle) => (
                    <tr
                      key={cycle.cycle_id}
                      onClick={() => setSelected(cycle.cycle_id)}
                      style={{
                        cursor: "pointer",
                        background:
                          selected === cycle.cycle_id
                            ? "rgba(77,159,255,0.12)"
                            : undefined,
                      }}
                    >
                      <td className="mono small">{cycle.cycle_id.slice(-10)}</td>
                      <td className="small">{cycle.pair}</td>
                      <td><StateBadge state={cycle.state} /></td>
                      <td className="num">{num(cycle.hedge_ratio).toFixed(4)}</td>
                      <td className="num">{num(cycle.residual_exposure).toFixed(6)}</td>
                      <td className="small muted">{datetime(cycle.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title={selected ? `Transitions — ${selected.slice(-10)}` : "Transitions"}>
          {!selected ? (
            <p className="muted small">Select a cycle to see every state transition it recorded.</p>
          ) : !detail ? (
            <Loading what="cycle" />
          ) : (
            <>
              <dl className="kv" style={{ marginBottom: 12 }}>
                <dt>Pair</dt><dd>{detail.pair}</dd>
                <dt>Objective</dt><dd>{detail.objective}</dd>
                <dt>Source / hedge</dt><dd>{detail.source} → {detail.hedge}</dd>
                <dt>Correlation ID</dt><dd>{detail.correlation_id ?? "—"}</dd>
                {detail.error && (<><dt>Error</dt><dd className="neg">{detail.error}</dd></>)}
              </dl>
              <ol className="timeline">
                {(detail.events ?? []).map((event) => (
                  <li key={event.sequence}>
                    <span className="seq">{event.sequence}</span>
                    <div style={{ flex: 1 }}>
                      <div>
                        <StateBadge state={event.to_state} />{" "}
                        <span className="small muted">
                          {event.from_state ?? "—"} → {event.to_state} · {event.event}
                        </span>
                      </div>
                      {Object.keys(event.payload).length > 0 && (
                        <details>
                          <summary className="small muted" style={{ cursor: "pointer" }}>
                            payload
                          </summary>
                          <pre className="payload">
                            {JSON.stringify(event.payload, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                    <span className="small muted">{datetime(event.timestamp)}</span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </Panel>
      </div>

      {graph && (
        <Panel title="State machine">
          {graph.problems.length > 0 ? (
            <div className="error">
              {graph.problems.map((p) => <div key={p}>{p}</div>)}
            </div>
          ) : (
            <p className="small muted">
              Every non-terminal state can reach a terminal one and every state is
              reachable from CREATED — verified on each request.
            </p>
          )}
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>State</th><th>Can move to</th><th>Notes</th></tr>
              </thead>
              <tbody>
                {graph.states.map((state) => (
                  <tr key={state}>
                    <td><StateBadge state={state} /></td>
                    <td className="small mono">
                      {graph.transitions[state]?.join(", ") || "—"}
                    </td>
                    <td className="small muted">
                      {graph.terminal.includes(state) && "terminal"}
                      {graph.exposed.includes(state) && "may hold exposure"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </>
  );
}
