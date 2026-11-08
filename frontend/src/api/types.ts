/**
 * API contract shared with the FastAPI backend.
 * Sign convention: battery_setpoint_mw > 0 = charging, < 0 = discharging.
 * Times are local "HH:MM" strings on 15-minute steps.
 */

/** Who produced the setpoint the guard checked: the AI, the backup rules, or a person
 * (a standing override, the emergency hold, a rejection, or a Day-tab plan for one interval). */
export type DecisionSource = "llm" | "fallback" | "operator";
export type Confidence = "low" | "medium" | "high";

export interface Decision {
  battery_fraction: number;
  curtail: boolean;
  source: DecisionSource;
  confidence: Confidence;
  reasoning: string;
  explanation_for_operator: string;
  /** Set when source = "fallback": timeout, llm_error, bad_json, approval_timeout, ... */
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

  // Imbalance: the cost of not delivering what was promised. Present when the
  // day has imbalance prices (settlement/settle.py).
  /** Actual grid exchange − the exchange promised on the forecast. */
  deviation_mwh?: number;
  /** ≤ 0: what that deviation cost beyond the day-ahead price. */
  imbalance_eur?: number;
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
  control: IntervalControl;
}

/**
 * What happened to the battery command in this interval (loop/day.py):
 * auto      the AI's decision ran without asking
 * approved  it waited and the operator approved it
 * rejected  the operator rejected it: the battery held
 * timeout   nobody answered in time: the battery held
 * override  the operator's setpoint ran (still checked by the safety layer)
 * hold      emergency hold: battery idle, nothing exported
 */
export type ControlAction = "auto" | "approved" | "rejected" | "timeout" | "override" | "hold";

/** autonomous: the AI acts; supervised: big or unsure moves wait for a click; advisory: every move waits. */
export type AutonomyMode = "autonomous" | "supervised" | "advisory";

export interface IntervalControl {
  action: ControlAction;
  mode: AutonomyMode;
  /** The AI's proposed setpoint when it had to wait for the operator, else null. */
  proposed_mw: number | null;
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

/** POST /api/override. Runs from the next interval up to, not including, `until`. */
export interface OperatorOverride {
  /** > 0 charge, < 0 discharge. */
  setpoint_mw: number;
  /** "HH:MM" on a 15-minute step, or "24:00" for the end of the day. */
  until: string;
}

/** PUT /api/autonomy (send only the fields to change). */
export interface Autonomy {
  mode: AutonomyMode;
  /** supervised: moves above this many MW wait for the operator. */
  max_auto_mw: number;
  /** supervised: low-confidence moves (including backup-rule ones) wait too. */
  ask_on_low_confidence: boolean;
  /** How long a proposal waits before the battery holds instead. */
  timeout_s: number;
}

/** An AI decision waiting for the operator. Answer with POST /api/approvals/{id} {approve}. */
export interface PendingApproval {
  id: number;
  /** The interval it is for. */
  time: string;
  setpoint_mw: number;
  source: DecisionSource;
  confidence: Confidence;
  explanation_for_operator: string;
  /** Seconds left when this was sent. */
  expires_in_s: number;
}

/** GET /api/control, every control command's response, and the SSE "control" event. */
export interface ControlState {
  /** Emergency hold: battery idle and nothing exported until released. */
  hold: boolean;
  override: OperatorOverride | null;
  autonomy: Autonomy;
  pending: PendingApproval | null;
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
  /** Intervals where a person set the battery: override, emergency hold or a rejected proposal. */
  decisions_operator: number;
  decisions_clipped: number;
  goals_met: number;
  goals_total: number;

  // The backend's own evaluation numbers (settlement/ledger.py metrics()). The
  // dashboard does not need them, but they come with the response.
  /** MWh charged + discharged. */
  battery_throughput_mwh?: number;
  /** ≤ 0: the day's cost of not delivering what was promised. */
  imbalance_eur?: number;
  /** Total MWh the site drifted from its promises, summed without sign. */
  deviation_mwh?: number;
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

  // Peak shaving (grid import cap). Import power is the 15-minute average, MW.
  /** Configured import cap; null when no cap is set. */
  import_limit_mw?: number | null;
  /** Highest settled grid import today, and when it happened. */
  peak_import_mw?: number;
  peak_import_time?: string | null;
  /** The same peak without the battery: max(0, demand − production). */
  peak_import_mw_no_battery?: number;
  /** Intervals whose settled import went over the cap. */
  import_limit_exceeded_intervals?: number;
}

// --------------------------------------------------------------------------
// Operator desk (backend/desk.py): alerts, upcoming decisions, flags, audit.

export type AlertKind = "goal_behind" | "goal_unreachable" | "fallback_streak" | "price_spike" | "forecast_error";
export type AlertSeverity = "critical" | "warning" | "info";
/** override = go set the upcoming intervals; relax_goal = lower a reserve goal; ignore = hide it. */
export type AlertAction = "override" | "relax_goal" | "ignore";

/** GET /api/alerts, and the SSE "alert" event when one is raised. */
export interface Alert {
  id: string;
  kind: AlertKind;
  severity: AlertSeverity;
  title: string;
  message: string;
  /** Simulated time of the interval that raised it. */
  time: string | null;
  /** Wall-clock ISO time. */
  raised_at: string;
  goal_id: string | null;
  actions: AlertAction[];
}

/** An action the operator set for one interval that has not run yet (Day tab, "Skip"/"Change"). */
export interface IntervalPlan {
  time: string;
  /** −1 full discharge … 0 hold … 1 full charge. */
  battery_fraction: number;
  note: string;
  set_at: string;
}

export interface PlannedAction {
  battery_fraction: number;
  /** After the safety checks, on the forecast. */
  setpoint_mw: number;
  explanation: string;
}

export interface UpcomingOverride {
  time: string;
  battery_fraction: number;
  note: string;
  set_at: string;
  setpoint_mw: number;
  /** The safety layer will not let the override run as set. */
  limited_by_safety: boolean;
}

export interface UpcomingSlot {
  time: string;
  price_eur_mwh: number;
  stored_before_mwh: number;
  /** Preview by the backup rules on the forecast; the AI decides when the interval comes. */
  planned: PlannedAction;
  override: UpcomingOverride | null;
}

/** GET /api/upcoming */
export interface UpcomingResponse {
  now: string | null;
  slots: UpcomingSlot[];
}

/** GET /api/flags: decisions the operator disagreed with. */
export interface DecisionFlag {
  time: string;
  reason: string;
  flagged_at: string;
}

export type AuditAction =
  | "goal_added"
  | "goal_removed"
  | "goal_relaxed"
  | "override_set"
  | "override_cleared"
  | "alert_ignored"
  | "decision_flagged"
  | "decision_unflagged"
  | "sim_speed"
  | "sim_reset"
  // standing operator control (control/)
  | "control_override"
  | "control_released"
  | "emergency_hold"
  | "autonomy_changed"
  | "approval_answered";

/** GET /api/audit, latest first. */
export interface AuditEntry {
  id: string;
  /** Wall-clock ISO time. */
  at: string;
  date: string;
  /** Latest decided interval when it happened. */
  sim_time: string | null;
  action: AuditAction;
  summary: string;
  detail: Record<string, unknown>;
}

/** POST /api/goals/{id}/relax */
export interface RelaxGoalResponse {
  goals: Goal[];
  message: string;
}
