import { useState } from "react";
import { useAction, useQuery } from "@/hooks/useApi";
import { api } from "@/lib/api";
import type { HedgeCalculation, Instrument, OptimizerResult } from "@/lib/api";
import { Empty, ErrorBox, Loading, Panel, WarnNote } from "@/components/ui";
import { bps, money, num, pct, quantity, ratio, signClass } from "@/lib/format";

interface InstrumentsResponse { instruments: Instrument[]; venues: string[] }

const OBJECTIVES = [
  ["BASE_ASSET_NEUTRAL", "Match base-asset holdings"],
  ["NOTIONAL_NEUTRAL", "Match notional value in the account currency"],
  ["QUOTE_PNL_NEUTRAL", "Match dPnL/dS in quote terms"],
  ["ACCOUNT_CCY_PNL_NEUTRAL", "Match dPnL/dS after FX conversion"],
  ["CUSTOM_RATIO", "Explicit ratio applied to delta"],
  ["FUNDING_ADJUSTED", "Mean-variance ratio using carry only"],
  ["COST_ADJUSTED", "Mean-variance ratio including execution cost"],
  ["RISK_WEIGHTED", "Beta hedge from vol and correlation"],
  ["PARTIAL", "Deliberately partial hedge"],
];

const PRIORITIES = [
  "MIN_RESIDUAL", "MIN_COST", "MAX_FUNDING_BENEFIT", "MIN_MARGIN",
  "MIN_LIQUIDATION_RISK", "MIN_SLIPPAGE", "MAX_CAPITAL_EFFICIENCY",
];

export function Calculator() {
  const { data: instruments } = useQuery<InstrumentsResponse>("/instruments", 0);
  const { run, pending, error } = useAction();

  const [sourceKey, setSourceKey] = useState("PAPER_DELTA:BTCUSDT-PERP");
  const [hedgeKey, setHedgeKey] = useState("PAPER_MT5:BTCUSD");
  const [qty, setQty] = useState("5000");
  const [objective, setObjective] = useState("QUOTE_PNL_NEUTRAL");
  const [targetRatio, setTargetRatio] = useState("1");
  const [useOptimizer, setUseOptimizer] = useState(false);
  const [mode, setMode] = useState("WEIGHTED");
  const [priority, setPriority] = useState("MIN_RESIDUAL");
  const [searchSteps, setSearchSteps] = useState("5");

  const [calculation, setCalculation] = useState<HedgeCalculation | null>(null);
  const [optimisation, setOptimisation] = useState<OptimizerResult | null>(null);

  const sources = (instruments?.instruments ?? []).filter(
    (i) => i.venue === "PAPER_DELTA",
  );
  const hedges = (instruments?.instruments ?? []).filter(
    (i) => i.venue !== "PAPER_DELTA",
  );

  const calculate = async () => {
    const body = {
      source_key: sourceKey,
      hedge_key: hedgeKey,
      source_quantity: qty,
      objective,
      target_ratio: targetRatio,
      use_live_positions: true,
    };
    if (useOptimizer) {
      const result = await run(() =>
        api.post<OptimizerResult>("/hedge/optimize", {
          ...body, mode, priority, search_steps: Number(searchSteps),
        }),
      );
      if (result) {
        setOptimisation(result);
        setCalculation(result.calculation);
      }
    } else {
      const result = await run(() =>
        api.post<HedgeCalculation>("/hedge/calculate", body),
      );
      if (result) {
        setOptimisation(null);
        setCalculation(result);
      }
    }
  };

  if (!instruments) return <Loading what="instruments" />;

  const sourceSpec = instruments.instruments.find((i) => i.key === sourceKey);
  const hedgeSpec = instruments.instruments.find((i) => i.key === hedgeKey);

  return (
    <>
      <h2 className="page-title">Hedge Calculator</h2>
      <p className="page-sub">
        Compute a hedge without trading. Every objective neutralises a different
        measure, and the derivation below shows exactly how the answer was
        reached — including the contract-size conversion and the FX step.
      </p>

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Panel title="Inputs">
          <div className="field">
            <label>Source instrument</label>
            <select value={sourceKey} onChange={(e) => setSourceKey(e.target.value)}>
              {sources.map((i) => (
                <option key={i.key} value={i.key}>
                  {i.key} — {i.sizing_description}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Hedge instrument</label>
            <select value={hedgeKey} onChange={(e) => setHedgeKey(e.target.value)}>
              {hedges.map((i) => (
                <option key={i.key} value={i.key}>
                  {i.key} — {i.sizing_description}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Source quantity (signed, venue units)</label>
            <input value={qty} onChange={(e) => setQty(e.target.value)} />
          </div>
          <div className="field">
            <label>Objective</label>
            <select value={objective} onChange={(e) => setObjective(e.target.value)}>
              {OBJECTIVES.map(([value, label]) => (
                <option key={value} value={value}>{value} — {label}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Target ratio</label>
            <input value={targetRatio} onChange={(e) => setTargetRatio(e.target.value)} />
          </div>

          <div className="field">
            <label style={{ display: "flex", alignItems: "center", gap: 8, textTransform: "none" }}>
              <input
                type="checkbox"
                style={{ width: "auto" }}
                checked={useOptimizer}
                onChange={(e) => setUseOptimizer(e.target.checked)}
              />
              Search the quantity lattice instead of rounding
            </label>
          </div>
          {useOptimizer && (
            <div className="grid cols-3" style={{ gap: 8 }}>
              <div className="field">
                <label>Mode</label>
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option>EXACT</option>
                  <option>STEP_SEARCH</option>
                  <option>WEIGHTED</option>
                </select>
              </div>
              <div className="field">
                <label>Priority</label>
                <select value={priority} onChange={(e) => setPriority(e.target.value)}>
                  {PRIORITIES.map((p) => <option key={p}>{p}</option>)}
                </select>
              </div>
              <div className="field">
                <label>Search steps</label>
                <input value={searchSteps} onChange={(e) => setSearchSteps(e.target.value)} />
              </div>
            </div>
          )}

          <button className="primary" disabled={pending} onClick={() => void calculate()}>
            {pending ? "Calculating…" : "Calculate"}
          </button>
        </Panel>

        <Panel title="Contract specifications">
          {sourceSpec && hedgeSpec ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>{sourceSpec.symbol}</th>
                    <th>{hedgeSpec.symbol}</th>
                  </tr>
                </thead>
                <tbody>
                  {([
                    ["Quantity unit", "quantity_unit"],
                    ["Units per unit", "units_per_quantity"],
                    ["Settlement style", "settlement_style"],
                    ["Quote / settle", null],
                    ["Tick size", "tick_size"],
                    ["Quantity step", "quantity_step"],
                    ["Min quantity", "min_quantity"],
                    ["Max quantity", "max_quantity"],
                    ["Initial margin", "effective_initial_margin_rate"],
                    ["Taker fee (bps)", "taker_fee_bps"],
                    ["Typical spread (bps)", "typical_spread_bps"],
                    ["Financing", "funding_model"],
                  ] as Array<[string, keyof Instrument | null]>).map(([label, field]) => (
                    <tr key={label}>
                      <td className="muted">{label}</td>
                      <td className="mono">
                        {field
                          ? String(sourceSpec[field])
                          : `${sourceSpec.quote_asset} / ${sourceSpec.settlement_asset}`}
                      </td>
                      <td className="mono">
                        {field
                          ? String(hedgeSpec[field])
                          : `${hedgeSpec.quote_asset} / ${hedgeSpec.settlement_asset}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty what="instrument selection" />
          )}
        </Panel>
      </div>

      <ErrorBox message={error} />

      {calculation && (
        <>
          <div className="grid cols-4" style={{ marginBottom: 16 }}>
            <div className="panel"><div className="stat">
              <div className="label">Required quantity</div>
              <div className="value">{quantity(calculation.required_quantity)}</div>
              <div className="sub">rounded to {quantity(calculation.rounded_quantity)}</div>
            </div></div>
            <div className="panel"><div className="stat">
              <div className="label">Hedge ratio</div>
              <div className="value">{ratio(calculation.hedge_ratio)}</div>
              <div className="sub">target {calculation.target_ratio}</div>
            </div></div>
            <div className="panel"><div className="stat">
              <div className="label">Residual exposure</div>
              <div className="value">{bps(calculation.residual_bps)}</div>
              <div className="sub">delta {num(calculation.residual_quote_delta).toFixed(8)}</div>
            </div></div>
            <div className="panel"><div className="stat">
              <div className="label">Executable</div>
              <div className={`value ${calculation.is_executable ? "pos" : "neg"}`}>
                {calculation.is_executable ? "yes" : "no"}
              </div>
              <div className="sub">min {quantity(calculation.min_executable_quantity)}, max safe {quantity(calculation.max_safe_quantity)}</div>
            </div></div>
          </div>

          {calculation.warnings.length > 0 && (
            <WarnNote>
              {calculation.warnings.map((w) => <div key={w}>• {w}</div>)}
            </WarnNote>
          )}

          <div className="grid cols-2" style={{ marginBottom: 16 }}>
            <Panel title="Costs and outcome">
              <dl className="kv">
                <dt>Fees</dt><dd className={signClass(`-${calculation.estimated_fees}`)}>{money(calculation.estimated_fees)}</dd>
                <dt>Spread (half)</dt><dd>{money(calculation.estimated_spread_cost)}</dd>
                <dt>Slippage</dt><dd>{money(calculation.estimated_slippage)}</dd>
                <dt>Total execution cost</dt><dd>{money(calculation.total_execution_cost)}</dd>
                <dt>Carry per day</dt>
                <dd className={signClass(calculation.funding_impact_per_day)}>
                  {money(calculation.funding_impact_per_day)}
                </dd>
                <dt>Margin required</dt><dd>{money(calculation.margin_requirement)}</dd>
                <dt>Expected P&amp;L</dt>
                <dd className={signClass(calculation.expected_pnl)}>{money(calculation.expected_pnl)}</dd>
                <dt>Worst case (99%)</dt>
                <dd className={signClass(calculation.worst_case_pnl)}>{money(calculation.worst_case_pnl)}</dd>
                <dt>Break-even move</dt>
                <dd>{calculation.break_even_move_pct ? pct(calculation.break_even_move_pct, 4) : "—"}</dd>
                <dt>Conversion ratio</dt><dd>{calculation.conversion_ratio}</dd>
              </dl>
            </Panel>

            {optimisation ? (
              <Panel title={`Optimiser candidates (${optimisation.mode})`} flush>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th className="num">Quantity</th>
                        <th className="num">Residual</th>
                        <th className="num">Cost</th>
                        <th className="num">Margin</th>
                        <th className="num">Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {optimisation.candidates.map((candidate) => (
                        <tr
                          key={candidate.quantity}
                          style={
                            candidate.quantity === optimisation.chosen_quantity
                              ? { background: "rgba(77,159,255,0.12)" }
                              : undefined
                          }
                        >
                          <td className="num">
                            {quantity(candidate.quantity)}
                            {candidate.quantity === optimisation.chosen_quantity && " ←"}
                          </td>
                          <td className="num">{bps(candidate.residual_bps)}</td>
                          <td className="num">{money(candidate.execution_cost)}</td>
                          <td className="num">{money(candidate.margin, "USD", 0)}</td>
                          <td className="num">{num(candidate.score).toFixed(4)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="panel-body small muted">{optimisation.rationale}</div>
              </Panel>
            ) : (
              <Panel title="Currency exposure">
                <p className="small muted">
                  Notional per settlement currency. Two legs settling in
                  different currencies leave FX exposure even when the price
                  exposure is flat.
                </p>
                <dl className="kv">
                  <dt>Notional exposure</dt>
                  <dd>{money(calculation.notional_exposure, "USD", 0)}</dd>
                  <dt>Rebalance quantity</dt>
                  <dd>{quantity(calculation.rebalance_quantity)}</dd>
                </dl>
              </Panel>
            )}
          </div>

          <Panel title="Derivation" flush>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: "1%" }}>#</th>
                    <th>Step</th>
                    <th>Formula</th>
                    <th>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {calculation.steps.map((step, index) => (
                    <tr key={`${step.label}-${index}`}>
                      <td className="muted mono">{index + 1}</td>
                      <td>{step.label}</td>
                      <td className="mono small">{step.formula}</td>
                      <td className="mono small">{step.value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}
    </>
  );
}
