/**
 * API contract shared with the FastAPI backend.
 * Sign convention: battery_setpoint_mw > 0 = charging, < 0 = discharging.
 * Times are local "HH:MM" strings on 15-minute steps.
 */

export type DecisionSource = "llm" | "fallback";
export type Confidence = "low" | "medium" | "high";

export interface Decision {
  battery_fraction: number;
  curtail: boolean;
  source: DecisionSource;
  confidence: Confidence;
  reasoning: string;
  explanation_for_operator: string;
  /** Set when source = "fallback": timeout, llm_error, bad_json, ... */
  fallback_reason: string | null;
}

export interface GuardResult {
  battery_setpoint_mw: number;
  curtail_mw: number;
  clipped: boolean;
  applied_rules: string[];
}

/** Money for one interval, settled on actual values (settlement/settle.py). Costs < 0. */
export interface Settlement {
  grid_import_mwh: number;
  grid_export_mwh: number;
  /** export × price − import × price */
  energy_cost_eur: number;
  /** −€2.5 per MWh charged or discharged */
  wear_eur: number;
  /** energy_cost_eur + wear_eur */
  profit_eur: number;
  /** Same interval without the battery or curtailment. */
  no_battery_profit_eur: number;
  /** profit_eur − no_battery_profit_eur: what the battery earned. */
  value_added_eur: number;
}

/** One 15-minute interval. Also the payload of the SSE "cycle" event. */
export interface Interval {
  time: string;
  price_eur_mwh: number;
  break_even: number;
  demand_mw: number;
  production_mw: number;
  solar_mw: number;
  wind_mw: number;
  /** Stored energy at the END of the interval. */
  soc_mwh: number;
  decision: Decision;
  guard: GuardResult;
  settlement: Settlement;
}

/**
 * GET /api/day. 96 slots for today. Intervals that have not run yet carry only
 * the forecast part (no decision/guard/settlement/soc).
 */
export interface ForecastInterval {
  time: string;
  price_eur_mwh: number;
  break_even: number;
  demand_mw: number;
  production_mw: number;
  solar_mw: number;
  wind_mw: number;
}

export type DaySlot = Interval | ForecastInterval;

export interface DayResponse {
  date: string;
  /** Time of the latest decided interval, e.g. "15:00", or null before the first cycle. */
  now: string | null;
  intervals: DaySlot[];
}

export function isDecided(slot: DaySlot): slot is Interval {
  return "decision" in slot && slot.decision != null;
}

export interface SimStatus {
  running: boolean;
  speed: number;
}

/** GET /api/state */
export interface StateSnapshot {
  capacity_mwh: number;
  max_power_mw: number;
  profit_today_eur: number;
  /** Latest decided interval, null before the first cycle of the day. */
  current: Interval | null;
  sim: SimStatus;
}

/** missed = reserve not stored by its deadline; violated = reserve discharged below / sold below the minimum. */
export type GoalStatus = "on_track" | "behind" | "met" | "unreachable" | "missed" | "violated";
export type GoalType = "reserve" | "min_sell_price" | "max_profit";

export interface Goal {
  id: string;
  type: GoalType;
  hard: boolean;
  source_text: string;
  status: GoalStatus;
  // reserve
  mwh?: number;
  by?: string;
  until?: string;
  stored_mwh?: number;
  need_mwh?: number;
  hours_left?: number;
  /** Reserve: first time the target was stored, e.g. "16:00". */
  reached_at?: string | null;
  violated_at?: string | null;
  /** Settlement's one-line status, e.g. "6 MWh reached at 16:00". */
  message?: string;
  // min_sell_price
  eur_per_mwh?: number;
}

/** POST /api/goals */
export interface AddGoalResponse {
  goals: Goal[];
  /** The parser could not understand the goal and asks the operator. */
  question?: string;
}

/** GET /api/metrics */
export interface Metrics {
  profit_today_eur: number;
  energy_cost_eur: number;
  wear_eur: number;
  no_battery_profit_eur: number;
  value_added_eur: number;
  energy_sold_mwh: number;
  energy_bought_mwh: number;
  curtailed_mwh: number;
  decisions_total: number;
  decisions_llm: number;
  decisions_fallback: number;
  decisions_clipped: number;
  goals_met: number;
  goals_total: number;

  // The backend's own evaluation numbers (settlement/ledger.py metrics()). The
  // dashboard does not need them, but they come with the response.
  /** MWh charged + discharged. */
  battery_throughput_mwh?: number;
  /** Stored energy after the latest interval. */
  soc_mwh?: number;
  /** decisions_fallback / decisions_total, and decisions_clipped / decisions_total. */
  fallback_rate?: number;
  clip_rate?: number;
  hard_goals_met?: number;
  hard_goals_total?: number;
  /** Hard goals that ended up missed or violated. */
  hard_goals_violated?: number;
  /** Forecast error against the actuals; null when nothing was measured. */
  demand_mae_mw?: number | null;
  production_mae_mw?: number | null;
  /** How often the actual production fell inside its 10-90 % band. */
  production_range_coverage?: number | null;
}
