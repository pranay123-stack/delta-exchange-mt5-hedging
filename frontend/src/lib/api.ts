/**
 * Typed API client.
 *
 * Every numeric field the backend sends is a **string**, deliberately: a
 * quantity that round-trips through a JavaScript float can come back off the
 * venue's lattice, and the dashboard would then display a number the venue
 * would reject. Formatting happens at render time via `format.ts`; arithmetic
 * on these values happens on the server.
 */

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail =
      (payload && (payload.detail ?? payload.message)) ?? response.statusText;
    throw new ApiError(
      typeof detail === "string" ? detail : JSON.stringify(detail),
      response.status,
      payload,
    );
  }
  return payload as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
      headers,
    }),
  patch: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  delete: <T,>(path: string, headers?: Record<string, string>) =>
    request<T>(path, { method: "DELETE", headers }),
};

/** Header that guards irreversible actions server-side. */
export const CONFIRM = { "X-Confirm-Action": "CONFIRM" };

// ---------------------------------------------------------------------------
// response shapes
// ---------------------------------------------------------------------------
export interface SystemStatus {
  app: string;
  version: string;
  environment: string;
  trading_mode: string;
  paper_only: boolean;
  live_adapters_registered: number;
  database_connected: boolean;
  venues: VenueHealth[];
  instruments: number;
  mappings: number;
  market_scenarios: Record<string, string>;
  armed_faults: ArmedFault[];
  emergency_mode: boolean;
  trading_paused: boolean;
  kill_switch_engaged: boolean;
  paused_pairs: string[];
  simulator_clock: string;
}

export interface VenueHealth {
  venue: string;
  kind: string;
  paper: boolean;
  connected: boolean;
  instruments: number;
  open_positions: number;
  margin_level: string;
  stop_out_level?: string;
}

export interface ArmedFault {
  kind: string;
  venue: string | null;
  symbol: string | null;
  leg: string | null;
  remaining: number | null;
  magnitude: string;
  reason: string;
  fired_count: number;
}

export interface Instrument {
  key: string;
  venue: string;
  symbol: string;
  display_name: string;
  instrument_type: string;
  base_asset: string;
  quote_asset: string;
  settlement_asset: string;
  quantity_unit: string;
  settlement_style: string;
  units_per_quantity: string;
  sizing_description: string;
  is_inverse: boolean;
  contract_size: string;
  tick_size: string;
  min_quantity: string;
  max_quantity: string;
  quantity_step: string;
  max_leverage: string;
  effective_initial_margin_rate: string;
  effective_maintenance_margin_rate: string;
  maker_fee_bps: string;
  taker_fee_bps: string;
  typical_spread_bps: string;
  funding_model: string;
  baseline_funding_rate: string;
  funding_interval_hours: string;
  swap_long_points: string;
  swap_short_points: string;
  effective_underlying_key: string;
  active: boolean;
}

export interface Mapping {
  key: string;
  name: string;
  source_key: string;
  hedge_key: string;
  objective: string;
  target_ratio: string;
  tolerance_bps: string;
  max_notional: string | null;
  enabled: boolean;
  source_sizing: string;
  hedge_sizing: string;
  conversion: string;
}

export interface Ticker {
  key: string;
  venue: string;
  symbol: string;
  bid: string;
  ask: string;
  mid: string;
  last: string;
  spread: string;
  spread_bps: string;
  volume: string;
  bid_size: string;
  ask_size: string;
  funding_rate: string | null;
  is_stale: boolean;
  timestamp: string;
}

export interface CalculationStep {
  label: string;
  formula: string;
  value: string;
  unit: string;
}

export interface HedgeCalculation {
  source: string;
  hedge: string;
  objective: string;
  target_ratio: string;
  required_quantity: string;
  rounded_quantity: string;
  rebalance_quantity: string;
  hedge_ratio: string;
  residual_quote_delta: string;
  residual_bps: string;
  notional_exposure: string;
  estimated_fees: string;
  estimated_spread_cost: string;
  estimated_slippage: string;
  total_execution_cost: string;
  funding_impact_per_day: string;
  margin_requirement: string;
  expected_pnl: string;
  worst_case_pnl: string;
  break_even_move_pct: string | null;
  max_safe_quantity: string;
  min_executable_quantity: string;
  conversion_ratio: string;
  is_executable: boolean;
  warnings: string[];
  steps: CalculationStep[];
}

export interface OptimizerCandidate {
  quantity: string;
  residual_bps: string;
  execution_cost: string;
  funding_per_day: string;
  margin: string;
  slippage: string;
  liquidation_risk: string;
  capital_efficiency: string;
  score: string;
  is_executable: boolean;
}

export interface OptimizerResult {
  mode: string;
  priority: string;
  exact_quantity: string;
  chosen_quantity: string;
  rationale: string;
  candidates: OptimizerCandidate[];
  calculation: HedgeCalculation;
}

export interface Position {
  venue: string;
  symbol: string;
  key: string;
  side: string;
  quantity: string;
  quantity_unit: string;
  sizing: string;
  average_entry: string;
  mark_price: string;
  base_units: string;
  notional_quote: string;
  notional_account: string;
  quote_delta: string;
  account_delta: string;
  unrealized_pnl: string;
  realized_pnl: string;
  funding_paid: string;
  funding_received: string;
  fees_paid: string;
  is_paper: boolean;
}

export interface Account {
  venue: string;
  currency: string;
  balance: string;
  equity: string;
  used_margin: string;
  free_margin: string;
  margin_level: string;
  maintenance_margin: string;
  unrealized_pnl: string;
  realized_pnl: string;
  fees_paid: string;
  funding_net: string;
  open_positions: number;
  margin_utilization_pct: string;
  is_connected: boolean;
}

export interface RiskBreach {
  metric: string;
  level: string;
  value: string;
  threshold: string;
  message: string;
}

export interface PairRisk {
  pair: string;
  source: string;
  hedge: string;
  level: string;
  breaches: RiskBreach[];
  actions: string[];
  source_notional: string;
  hedge_notional: string;
  net_notional: string;
  residual_delta: string;
  residual_bps: string;
  hedge_ratio: string;
  total_margin: string;
  source_margin_level: string;
  hedge_margin_level: string;
  source_liquidation_price: string | null;
  hedge_liquidation_price: string | null;
  max_tolerable_move: string | null;
  funding_risk_per_day: string;
  basis_bps: string;
  liquidity_risk_bps: string;
  unrealized_pnl: string;
  realized_pnl: string;
  daily_pnl: string;
  currency: string;
  is_tradable: boolean;
}

export interface PortfolioRisk {
  level: string;
  allows_new_trades: boolean;
  breaches: RiskBreach[];
  actions: string[];
  total_source_exposure: string;
  total_hedge_exposure: string;
  net_exposure: string;
  gross_notional: string;
  net_exposure_pct: string;
  exposure_by_venue: Record<string, string>;
  exposure_by_underlying: Record<string, string>;
  currency_exposure: Record<string, string>;
  aggregate_equity: string;
  aggregate_margin: string;
  aggregate_free_margin: string;
  margin_utilization_pct: string;
  worst_margin_level: string;
  aggregate_liquidation_risk: string | null;
  portfolio_pnl: string;
  unrealized_pnl: string;
  realized_pnl: string;
  aggregate_funding_per_day: string;
  aggregate_fees: string;
  portfolio_max_loss: string;
  concentration_pct: string;
  concentration_underlying: string;
  pair_count: number;
  breaching_pairs: string[];
}

export interface StatisticsPair {
  pair: string;
  estimated: boolean;
  provenance: string;
  applied: {
    source_daily_vol: string;
    hedge_daily_vol: string;
    correlation: string;
    beta: string;
    residual_daily_vol: string;
  };
  beta_standard_error: string;
  beta_is_distinguishable_from_one: boolean;
  overlapping_samples: number;
  is_reliable: boolean;
  note: string;
  source: { key: string; samples: number; daily_volatility: string; is_reliable: boolean };
  hedge: { key: string; samples: number; daily_volatility: string; is_reliable: boolean };
}

export interface StatisticsResponse {
  sample_seconds: number;
  window: number;
  min_samples: number;
  sample_counts: Record<string, number>;
  pairs: StatisticsPair[];
}

export interface PnLComponent {
  name: string;
  amount: string;
  currency: string;
  explanation: string;
}

export interface PnLBreakdown {
  pair: string;
  scope: string;
  currency: string;
  funding_received: string;
  funding_paid: string;
  swap_financing: string;
  net_funding: string;
  trading_fees: string;
  spread_cost: string;
  slippage_cost: string;
  total_costs: string;
  fx_conversion_impact: string;
  gross_pnl: string;
  net_pnl: string;
  realized_pnl: string;
  unrealized_pnl: string;
  break_even_cost: string;
  components: PnLComponent[];
  consistent: boolean;
}

export interface Order {
  order_id: string;
  client_order_id: string;
  cycle_id: string | null;
  leg: string | null;
  venue: string;
  symbol: string;
  side: string;
  order_type: string;
  quantity: string;
  price: string | null;
  status: string;
  filled_quantity: string;
  average_price: string;
  reference_price: string;
  slippage: string;
  fees_paid: string;
  reject_reason: string | null;
  is_paper: boolean;
  created_at: string;
}

export interface CycleEvent {
  sequence: number;
  from_state: string | null;
  to_state: string;
  event: string;
  payload: Record<string, unknown>;
  timestamp: string;
}

export interface HedgeCycle {
  cycle_id: string;
  pair: string;
  state: string;
  objective: string;
  source: string;
  hedge: string;
  hedge_ratio: string;
  residual_exposure: string;
  source_filled_quantity?: string;
  hedge_filled_quantity?: string;
  error: string | null;
  correlation_id: string | null;
  created_at: string;
  updated_at: string;
  events?: CycleEvent[];
}

export interface Discrepancy {
  issue: string;
  venue: string;
  symbol: string;
  database_value: string | null;
  venue_value: string | null;
  difference: string;
  severity: string;
  message: string;
  suggested_action: string;
}

export interface ReconciliationReport {
  timestamp: string;
  checked_venues: string[];
  unreachable_venues: string[];
  database_positions: number;
  venue_positions: number;
  is_clean: boolean;
  has_critical: boolean;
  issue_counts: Record<string, number>;
  discrepancies: Discrepancy[];
}

export interface ScenarioStep {
  label: string;
  detail: string;
  data: Record<string, unknown>;
}

export interface ScenarioResult {
  key: string;
  name: string;
  passed: boolean;
  summary: string;
  error: string | null;
  steps: ScenarioStep[];
}

export interface AuditEntry {
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  correlation_id: string | null;
  timestamp: string;
}
