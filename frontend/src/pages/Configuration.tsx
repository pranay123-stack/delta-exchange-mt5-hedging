import { useEffect, useState } from "react";
import { useAction, useQuery } from "@/hooks/useApi";
import { api } from "@/lib/api";
import type { Instrument, Mapping } from "@/lib/api";
import { ErrorBox, Loading, Notice, Panel } from "@/components/ui";
import { datetime } from "@/lib/format";

interface InstrumentsResponse { count: number; venues: string[]; instruments: Instrument[] }
interface MappingsResponse { mappings: Mapping[] }
interface ConfigResponse {
  risk_thresholds: Record<string, string>;
  portfolio_limits: Record<string, string>;
  pairs: Array<{ name: string; objective: string; target_ratio: string; tolerance_bps: string; enabled: boolean }>;
}
interface ChangesResponse {
  changes: Array<{
    actor: string; entity: string; entity_id: string | null;
    before: unknown; after: unknown; note: string; timestamp: string;
  }>;
}
interface FxResponse {
  pivot: string;
  account_currency: string;
  rates: Array<{ pair: string; rate: string; source: string }>;
}

export function Configuration() {
  const { data: instruments } = useQuery<InstrumentsResponse>("/instruments", 0);
  const { data: mappings } = useQuery<MappingsResponse>("/instrument-mappings", 0);
  const { data: config, refresh: refreshConfig } = useQuery<ConfigResponse>("/hedge-configs", 0);
  const { data: changes, refresh: refreshChanges } =
    useQuery<ChangesResponse>("/audit/configuration-changes?limit=25", 0);
  const { data: fx } = useQuery<FxResponse>("/fx-rates", 0);
  const { run, pending, error } = useAction();

  const [thresholds, setThresholds] = useState<Record<string, string>>({});
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | null>(null);
  const [specJson, setSpecJson] = useState("");

  useEffect(() => {
    if (config) {
      setThresholds(config.risk_thresholds);
      setLimits(config.portfolio_limits);
    }
  }, [config]);

  const saveThresholds = async () => {
    const result = await run(() => api.patch("/risk/thresholds", thresholds));
    if (result) {
      setSaved("Risk thresholds updated and recorded in the change log.");
      refreshConfig();
      refreshChanges();
    }
  };

  const saveLimits = async () => {
    const result = await run(() => api.patch("/portfolio/limits", limits));
    if (result) {
      setSaved("Portfolio limits updated and recorded in the change log.");
      refreshConfig();
      refreshChanges();
    }
  };

  const addInstrument = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(specJson);
    } catch {
      setSaved(null);
      return;
    }
    const result = await run(() => api.post<Instrument>("/instruments", parsed));
    if (result) {
      setSaved(`Instrument ${result.key} saved — it is tradable immediately, with no code change.`);
      refreshChanges();
    }
  };

  if (!instruments || !config) return <Loading what="configuration" />;

  return (
    <>
      <h2 className="page-title">Configuration</h2>
      <p className="page-sub">
        Instruments, hedge pairs and limits are data, not code. Every change
        here is written to the configuration change log with its before and
        after state.
      </p>

      <ErrorBox message={error} />
      {saved && <Notice>{saved}</Notice>}

      <Panel title={`Instrument specifications (${instruments.count})`} flush>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Key</th><th>Type</th><th>Unit</th><th>Sizing</th>
                <th className="num">Tick</th><th className="num">Step</th>
                <th className="num">Min</th><th className="num">Max</th>
                <th className="num">IM</th><th className="num">Taker bps</th>
                <th>Financing</th>
              </tr>
            </thead>
            <tbody>
              {instruments.instruments.map((instrument) => (
                <tr key={instrument.key}>
                  <td>
                    {instrument.key}
                    {instrument.is_inverse && (
                      <span className="badge level-WARNING" style={{ marginLeft: 6 }}>INVERSE</span>
                    )}
                  </td>
                  <td className="small">{instrument.instrument_type}</td>
                  <td className="small">{instrument.quantity_unit}</td>
                  <td className="small mono">{instrument.sizing_description}</td>
                  <td className="num">{instrument.tick_size}</td>
                  <td className="num">{instrument.quantity_step}</td>
                  <td className="num">{instrument.min_quantity}</td>
                  <td className="num">{instrument.max_quantity}</td>
                  <td className="num">{instrument.effective_initial_margin_rate}</td>
                  <td className="num">{instrument.taker_fee_bps}</td>
                  <td className="small">
                    {instrument.funding_model === "PERPETUAL_FUNDING"
                      ? `funding ${instrument.baseline_funding_rate}/${instrument.funding_interval_hours}h`
                      : instrument.funding_model === "SWAP_POINTS"
                        ? `swap ${instrument.swap_long_points}/${instrument.swap_short_points} pts`
                        : "none"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <Panel title="Risk thresholds">
          {Object.entries(thresholds).map(([key, value]) => (
            <div className="field" key={key}>
              <label>{key.replace(/_/g, " ")}</label>
              <input
                value={value}
                onChange={(e) =>
                  setThresholds((prev) => ({ ...prev, [key]: e.target.value }))
                }
              />
            </div>
          ))}
          <button className="primary" disabled={pending} onClick={() => void saveThresholds()}>
            Save thresholds
          </button>
        </Panel>

        <Panel title="Portfolio limits">
          {Object.entries(limits).map(([key, value]) => (
            <div className="field" key={key}>
              <label>{key.replace(/_/g, " ")}</label>
              <input
                value={value}
                onChange={(e) =>
                  setLimits((prev) => ({ ...prev, [key]: e.target.value }))
                }
              />
            </div>
          ))}
          <button className="primary" disabled={pending} onClick={() => void saveLimits()}>
            Save limits
          </button>
        </Panel>
      </div>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <Panel title="Hedge pairs">
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Pair</th><th>Objective</th><th className="num">Ratio</th><th className="num">Tolerance</th><th>Enabled</th></tr>
              </thead>
              <tbody>
                {(mappings?.mappings ?? []).map((mapping) => (
                  <tr key={mapping.key}>
                    <td>
                      {mapping.name}
                      <div className="small muted mono">{mapping.conversion}</div>
                    </td>
                    <td className="small">{mapping.objective}</td>
                    <td className="num">{mapping.target_ratio}</td>
                    <td className="num">{mapping.tolerance_bps} bps</td>
                    <td>{mapping.enabled ? "yes" : "no"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Add an instrument">
          <p className="small muted">
            Paste a full specification. The engine has no hardcoded knowledge of
            any symbol, so a new instrument is tradable as soon as it is saved.
          </p>
          <div className="field">
            <textarea
              rows={12}
              value={specJson}
              placeholder={JSON.stringify(
                {
                  symbol: "SILVER-H7", venue: "PAPER_MT5", venue_kind: "MT5_BROKER",
                  instrument_type: "CFD", base_asset: "XAG", quote_asset: "USD",
                  settlement_asset: "USD", underlying_key: "XAG",
                  quantity_unit: "LOT", contract_size: "5000", units_per_lot: "5000",
                  tick_size: "0.001", min_quantity: "0.01", quantity_step: "0.01",
                  max_quantity: "100", price_precision: 3, quantity_precision: 2,
                  max_leverage: "50", margin_model: "BROKER_LEVERAGE",
                  funding_model: "SWAP_POINTS", swap_long_points: "-12",
                  swap_short_points: "3",
                },
                null,
                2,
              )}
              onChange={(e) => setSpecJson(e.target.value)}
              style={{ fontFamily: "var(--mono)", fontSize: 11 }}
            />
          </div>
          <button className="primary" disabled={pending || !specJson} onClick={() => void addInstrument()}>
            Save instrument
          </button>
        </Panel>
      </div>

      <div className="grid cols-2" style={{ marginTop: 16 }}>
        <Panel title="FX rates" flush>
          <div className="table-scroll">
            <table>
              <thead><tr><th>Pair</th><th className="num">Rate</th><th>Source</th></tr></thead>
              <tbody>
                {(fx?.rates ?? []).map((rate) => (
                  <tr key={rate.pair}>
                    <td>{rate.pair}</td>
                    <td className="num">{rate.rate}</td>
                    <td className="small muted">{rate.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Configuration change log" flush>
          <div className="table-scroll" style={{ maxHeight: 320, overflowY: "auto" }}>
            <table>
              <thead><tr><th>When</th><th>Actor</th><th>Entity</th><th>Note</th></tr></thead>
              <tbody>
                {(changes?.changes ?? []).map((change, index) => (
                  <tr key={`${change.timestamp}-${index}`}>
                    <td className="small muted">{datetime(change.timestamp)}</td>
                    <td className="small">{change.actor}</td>
                    <td className="small">{change.entity} {change.entity_id ?? ""}</td>
                    <td className="small">{change.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </>
  );
}
