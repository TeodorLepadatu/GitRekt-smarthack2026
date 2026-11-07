/**
 * Mock of backend/desk.py: which conditions raise an alert, and the alert book
 * that keeps one alert per condition while it lasts.
 */
import type { Alert, AlertAction, AlertSeverity, AuditAction, AuditEntry, ForecastInterval, Goal, Interval } from "@/api/types";

const FALLBACK_STREAK = 3;
const SPIKE_LOOKAHEAD = 8;

type Condition = Omit<Alert, "id" | "time" | "raised_at">;

const median = (xs: number[]) => {
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

export function alertConditions(done: Interval[], goals: Goal[], day: ForecastInterval[]): Record<string, Condition> {
  const out: Record<string, Condition> = {};

  for (const g of goals) {
    if (g.status !== "behind" && g.status !== "unreachable") continue;
    const unreachable = g.status === "unreachable";
    const actions: AlertAction[] = g.type === "reserve" ? ["override", "relax_goal", "ignore"] : ["override", "ignore"];
    out[`goal:${g.id}:${g.status}`] = {
      kind: unreachable ? "goal_unreachable" : "goal_behind",
      severity: unreachable ? "critical" : "warning",
      title: unreachable ? `'${g.source_text}' can no longer be met` : `'${g.source_text}' is behind`,
      message: g.message || (unreachable ? "Not enough time left to store the target." : "Charging is behind schedule."),
      goal_id: g.id,
      actions,
    };
  }

  let streak = 0;
  for (let i = done.length - 1; i >= 0 && done[i].decision.source === "fallback"; i--) streak++;
  if (streak >= FALLBACK_STREAK) {
    const last = done[done.length - 1];
    out.fallback_streak = {
      kind: "fallback_streak",
      severity: "warning",
      title: `The AI failed ${streak} times in a row`,
      message: `The backup rules have decided since ${done[done.length - streak].time} (last reason: ${last.decision.fallback_reason ?? "unknown"}). Check the model, or set the next intervals yourself.`,
      goal_id: null,
      actions: ["override", "ignore"],
    };
  }

  const prices = day.map((s) => s.price_eur_mwh);
  const med = median(prices);
  const threshold = Math.max(med * 1.5, med + 50);
  const ahead = day.slice(done.length, done.length + SPIKE_LOOKAHEAD).filter((s) => s.price_eur_mwh >= threshold);
  if (ahead.length) {
    const peak = ahead.reduce((a, b) => (b.price_eur_mwh > a.price_eur_mwh ? b : a));
    // one key for the whole episode: the alert stays one alert while the window slides through the peak
    out.price_spike = {
      kind: "price_spike",
      severity: "info",
      title: `Price spike from ${ahead[0].time}`,
      message: `Up to €${peak.price_eur_mwh.toFixed(0)}/MWh at ${peak.time}, against a day median of €${med.toFixed(0)}. Make sure there is energy stored to sell.`,
      goal_id: null,
      actions: ["override", "ignore"],
    };
  }
  // forecast_error: the mock's actuals are its forecast, so it never fires here.
  return out;
}

const ORDER: Record<AlertSeverity, number> = { critical: 0, warning: 1, info: 2 };

export class AlertBook {
  private active = new Map<string, Alert>();
  private ignored = new Set<string>();
  private next = 1;

  update(conditions: Record<string, Condition>, simTime: string | null): Alert[] {
    const raised: Alert[] = [];
    for (const key of [...this.active.keys()]) if (!(key in conditions)) this.active.delete(key);
    for (const key of [...this.ignored]) if (!(key in conditions)) this.ignored.delete(key);
    for (const [key, c] of Object.entries(conditions)) {
      if (this.ignored.has(key)) continue;
      const cur = this.active.get(key);
      if (cur) {
        cur.title = c.title;
        cur.message = c.message;
        continue;
      }
      const a: Alert = { id: `a${this.next++}`, time: simTime, raised_at: new Date().toISOString(), ...c };
      this.active.set(key, a);
      raised.push(a);
    }
    return raised;
  }

  list(): Alert[] {
    return [...this.active.values()].sort((a, b) => ORDER[a.severity] - ORDER[b.severity] || a.id.localeCompare(b.id));
  }

  ignore(id: string): Alert | null {
    for (const [key, a] of this.active) {
      if (a.id === id) {
        this.active.delete(key);
        this.ignored.add(key);
        return a;
      }
    }
    return null;
  }

  clear() {
    this.active.clear();
    this.ignored.clear();
  }
}

export class MockAudit {
  private entries: AuditEntry[] = [];
  private next = 1;

  add(action: AuditAction, summary: string, simTime: string | null, detail: Record<string, unknown> = {}) {
    this.entries.unshift({
      id: `e${this.next++}`,
      at: new Date().toISOString(),
      date: new Date().toISOString().slice(0, 10),
      sim_time: simTime,
      action,
      summary,
      detail,
    });
    this.entries.length = Math.min(this.entries.length, 500);
  }

  list(limit: number) {
    return this.entries.slice(0, limit);
  }
}
