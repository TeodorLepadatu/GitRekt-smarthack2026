/** All plain-language wording for the operator lives here. */
import type { AutonomyMode, Confidence, DaySlot, Decision, Goal, GoalStatus, Interval, IntervalControl } from "@/api/types";

export type ActionKind = "charge" | "discharge" | "hold";

const HOLD_EPS = 0.005;

export function actionKind(setpointMw: number): ActionKind {
  if (setpointMw > HOLD_EPS) return "charge";
  if (setpointMw < -HOLD_EPS) return "discharge";
  return "hold";
}

export const fmtMw = (v: number) => `${v.toFixed(2)} MW`;
export const fmtMwh = (v: number) => `${v.toFixed(2)} MWh`;
export const fmtEur = (v: number) =>
  `${v < 0 ? "−" : ""}€${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const fmtPrice = (v: number) => `${fmtEur(v)}/MWh`;
export const fmtPct = (v: number) => `${Math.round(v)}%`;

export function actionLabel(setpointMw: number): string {
  const kind = actionKind(setpointMw);
  if (kind === "charge") return `Charging ${fmtMw(setpointMw)}`;
  if (kind === "discharge") return `Discharging ${fmtMw(Math.abs(setpointMw))}`;
  return "Holding";
}

export const actionShort: Record<ActionKind, string> = {
  charge: "Charge",
  discharge: "Discharge",
  hold: "Hold",
};

export type SourceKind = "ai" | "backup" | "operator" | "safety";

/** Who decided this interval. Safety adjustment wins over the decision source. */
export function sourceKind(iv: Pick<Interval, "decision" | "guard">): SourceKind {
  if (iv.guard.clipped) return "safety";
  if (iv.decision.source === "operator") return "operator";
  return iv.decision.source === "fallback" ? "backup" : "ai";
}

export const sourceLabel: Record<SourceKind, string> = {
  ai: "AI",
  backup: "Backup rules",
  operator: "Operator",
  safety: "Adjusted by safety",
};

export const sourceHint: Record<SourceKind, string> = {
  ai: "The AI made this decision and it passed all safety checks.",
  backup: "The AI's answer could not be used, so the backup rules decided.",
  operator:
    "A person set the battery: a manual override, an interval set ahead on the Day tab, the emergency hold, or a rejected AI proposal.",
  safety: "The safety layer changed the decision to respect battery limits or your goals.",
};

/** Battery fraction (−1 … 1) as an action, e.g. "Charge 60%", "Hold". */
export function fractionText(f: number): string {
  if (Math.abs(f) < 0.005) return "Hold";
  return `${f > 0 ? "Charge" : "Discharge"} ${Math.round(Math.abs(f) * 100)}%`;
}

export const alertActionLabel = {
  override: "Set the next intervals",
  relax_goal: "Relax goal",
  ignore: "Ignore",
} as const;

/** "12:30" from a wall-clock ISO string. */
export function clockTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** What the operator had to do with this interval, or null when the AI acted on its own. */
export function controlText(c: IntervalControl): string | null {
  const proposed = c.proposed_mw == null ? "" : ` (the AI proposed to ${actionPhrase(c.proposed_mw)})`;
  switch (c.action) {
    case "approved":
      return `Approved by the operator${proposed}`;
    case "rejected":
      return `Rejected by the operator${proposed}, battery held`;
    case "timeout":
      return `No answer from the operator in time${proposed}, battery held`;
    case "override":
      return `Manual override${proposed}`;
    case "hold":
      return "Emergency hold: battery idle, nothing exported";
    default:
      return null;
  }
}

/** "charge 3.00 MW" / "discharge 1.20 MW" / "hold" */
export function actionPhrase(mw: number): string {
  const k = actionKind(mw);
  if (k === "charge") return `charge ${fmtMw(mw)}`;
  if (k === "discharge") return `discharge ${fmtMw(-mw)}`;
  return "hold";
}

export const autonomyLabel: Record<AutonomyMode, string> = {
  autonomous: "Autonomous",
  supervised: "Ask before big moves",
  advisory: "Advisory only",
};

export const autonomyHint: Record<AutonomyMode, string> = {
  autonomous: "The AI's decisions run as they are. The safety layer still checks every one.",
  supervised: "Small, confident moves run by themselves. Bigger or unsure ones wait for your click.",
  advisory: "The AI only suggests. Nothing moves the battery until you approve it.",
};

export function fallbackReasonText(d: Decision): string | null {
  if (d.source !== "fallback") return null;
  switch (d.fallback_reason) {
    case "timeout":
      return "AI took too long, backup rules decided";
    case "llm_error":
      return "AI was not reachable, backup rules decided";
    case "feature_error":
    case "prompt_error":
      return "Some site data was missing, backup rules decided";
    case "approval_timeout":
      return "No answer from the operator in time, battery held";
    default:
      return "AI answer was unusable, backup rules decided";
  }
}

export const confidenceLabel: Record<Confidence, string> = {
  low: "Low confidence",
  medium: "Medium confidence",
  high: "High confidence",
};

export const goalStatusLabel: Record<GoalStatus, string> = {
  on_track: "On track",
  behind: "Behind",
  met: "Met",
  unreachable: "Can't be met",
  missed: "Missed",
  violated: "Broken",
};

/** Text used to quote a goal inside rule messages. */
function goalName(id: string, goals: Goal[]): string {
  return goals.find((g) => g.id === id)?.source_text ?? `goal ${id}`;
}

/**
 * Translate guard applied_rules into operator sentences.
 * Rules that are not interesting to the operator return nothing.
 */
export function ruleText(rule: string, goals: Goal[]): string | null {
  switch (rule) {
    case "soc_max_limit":
      return "Battery almost full, charging limited";
    case "soc_min_limit":
      return "Battery almost empty, discharging limited";
    case "curtail_applied":
    case "curtail_forced_min_sell":
      return "Part of the solar/wind output was switched off";
    case "no_soc_hold":
    case "guard_error_hold":
      return "Safe mode: battery held";
    case "emergency_hold":
      return "Emergency hold: battery idle, nothing exported";
    case "import_cap_discharge":
      return "Discharging to keep grid import under the limit";
    case "import_cap_charge_limited":
      return "Charging limited to keep grid import under the limit";
    case "import_cap_unmet":
      return "Warning: grid import above the limit, the battery can't cover it";
  }
  const m = /^goal_(.+)_(forced_charge|blocked_discharge|blocked_export|unreachable)$/.exec(rule);
  if (m) {
    const name = goalName(m[1], goals);
    switch (m[2]) {
      case "forced_charge":
        return `Charging at full power to meet '${name}'`;
      case "blocked_discharge":
        return `Discharge limited to keep '${name}'`;
      case "blocked_export":
        return "Not selling: price below your minimum";
      case "unreachable":
        return `Warning: '${name}' can no longer be met`;
    }
  }
  // *_ok, *_hold, curtail_blocked_*, fraction_clamped, ... are not shown.
  return null;
}

export function rulesText(rules: string[], goals: Goal[]): string[] {
  const out = rules.map((r) => ruleText(r, goals)).filter((t): t is string => t !== null);
  return [...new Set(out)];
}

/** Short description of a goal's progress, e.g. "3.50 of 6.00 MWh, 3 h left". */
export function goalProgressText(g: Goal): string | null {
  if (g.type !== "reserve" || g.mwh == null) return null;
  const stored = Math.min(g.stored_mwh ?? 0, g.mwh);
  let text = `${stored.toFixed(2)} of ${fmtMwh(g.mwh)}`;
  if (g.status !== "met" && g.hours_left != null && g.hours_left > 0) {
    text += `, ${fmtHours(g.hours_left)} left`;
  }
  return text;
}

export function fmtHours(h: number): string {
  if (h >= 1) {
    const whole = Math.floor(h);
    const min = Math.round((h - whole) * 60);
    return min ? `${whole} h ${min} min` : `${whole} h`;
  }
  return `${Math.round(h * 60)} min`;
}

export const goalTypeLabel: Record<Goal["type"], string> = {
  reserve: "Energy reserve",
  min_sell_price: "Minimum sell price",
  max_profit: "Objective",
};

/** The next notable thing in the price forecast, for the "Coming up" line. */
export function upNextText(slots: DaySlot[], now: string | null): string | null {
  const start = now ? slots.findIndex((s) => s.time === now) + 1 : 0;
  const future = slots.slice(start);
  if (future.length === 0) return null;
  const current = start > 0 ? slots[start - 1] : null;
  if (current && current.price_eur_mwh < 0) {
    const back = future.find((s) => s.price_eur_mwh >= 0);
    return back
      ? `Prices stay below zero until ${back.time}, so charging is paid for until then.`
      : "Prices stay below zero for the rest of the day.";
  }
  const firstNeg = future.find((s) => s.price_eur_mwh < 0);
  const firstPeak = future.find((s) => s.price_eur_mwh >= s.break_even);
  const pick =
    firstNeg && (!firstPeak || firstNeg.time < firstPeak.time)
      ? { s: firstNeg, text: "prices go below zero — the battery can charge and get paid for it." }
      : firstPeak
        ? { s: firstPeak, text: "prices rise above break-even — a good time to sell stored energy." }
        : null;
  if (!pick) return "No price spikes or negative prices expected for the rest of today.";
  return pick.s === future[0] ? `From now, ${pick.text}` : `From ${pick.s.time}, ${pick.text}`;
}

/** The interval's explanation in one sentence: the backend's (built from the numbers), else the AI's own. */
export function explanationText(iv: Pick<Interval, "decision" | "explanation">): string {
  return iv.explanation?.summary || iv.decision.explanation_for_operator;
}
