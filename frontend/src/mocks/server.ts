/**
 * In-browser mock of the FastAPI backend. Implements the same routes and
 * shapes as the real API (see src/api/types.ts). Enabled with VITE_USE_MOCKS=true.
 */
import type {
  AddGoalResponse,
  Alert,
  DayResponse,
  DecisionFlag,
  Goal,
  Interval,
  Metrics,
  Override,
  RelaxGoalResponse,
  StateSnapshot,
  UpcomingResponse,
  UpcomingSlot,
} from "@/api/types";
import { INTERVAL_H, indexToTime, timeToIndex } from "@/lib/utils";
import { AlertBook, alertConditions, MockAudit } from "./desk";
import { buildForecast } from "./forecast";
import { parseGoal } from "./goalParser";
import { trackGoals } from "./settlement";
import { CAPACITY, ETA, P_MAX, previewCycle, runCycle } from "./simulator";

const START_SOC = 4.0;
/** Where the demo day starts on page load: just before the noon negative prices. */
const START_INDEX = 45;

const DEFAULT_GOALS: Omit<Goal, "status">[] = [
  { id: "g0", type: "max_profit", hard: false, source_text: "Maximise profit" },
  { id: "g1", type: "reserve", hard: true, mwh: 6, by: "18:00", source_text: "Store 6 MWh by 18:00" },
  { id: "g2", type: "min_sell_price", hard: true, eur_per_mwh: 50, source_text: "Never sell below €50/MWh" },
];

type Listener = (iv: Interval) => void;
type AlertListener = (a: Alert) => void;

const TIME_RE = /^([01]\d|2[0-3]):(00|15|30|45)$/;

class MockBackend {
  private forecast = buildForecast();
  private done: Interval[] = [];
  private goals: Omit<Goal, "status">[] = DEFAULT_GOALS.map((g) => ({ ...g }));
  private nextId = 3;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private speed = 0;
  private listeners = new Set<Listener>();
  private alertListeners = new Set<AlertListener>();
  // operator desk
  private overrides = new Map<string, Override>();
  private flags = new Map<string, DecisionFlag>();
  private alerts = new AlertBook();
  private audit = new MockAudit();

  constructor() {
    this.fastForward(START_INDEX + 1);
  }

  private now() {
    return this.done.length ? this.done[this.done.length - 1].time : null;
  }

  private soc() {
    return this.done.length ? this.done[this.done.length - 1].soc_mwh : START_SOC;
  }

  private step(): Interval | null {
    const i = this.done.length;
    if (i >= 96) return null;
    const iv = runCycle(i, this.forecast[i], this.soc(), this.goals as Goal[], this.overrides.get(indexToTime(i)));
    this.done.push(iv);
    const raised = this.alerts.update(alertConditions(this.done, this.listGoals(), this.forecast), iv.time);
    raised.forEach((a) => this.alertListeners.forEach((l) => l(a)));
    return iv;
  }

  private newDay() {
    this.done = [];
    this.overrides.clear();
    this.flags.clear();
    this.alerts.clear();
  }

  private upcoming(n: number): UpcomingResponse {
    let soc = this.soc();
    const slots: UpcomingSlot[] = [];
    for (let i = this.done.length; i < Math.min(96, this.done.length + n); i++) {
      const t = indexToTime(i);
      const o = this.overrides.get(t) ?? null;
      const p = previewCycle(i, this.forecast[i], soc, this.goals as Goal[], o);
      slots.push({
        time: t,
        price_eur_mwh: this.forecast[i].price_eur_mwh,
        stored_before_mwh: Math.round(soc * 1000) / 1000,
        planned: p.planned,
        override: o && p.override ? { ...o, ...p.override } : null,
      });
      soc = p.socAfter;
    }
    return { now: this.now(), slots };
  }

  /** Index of an interval that has not run yet, or an HTTP error. */
  private futureIndex(time: string) {
    if (!TIME_RE.test(time)) throw new MockHttpError(404, `'${time}' not found`);
    const i = timeToIndex(time);
    if (i < this.done.length) throw new MockHttpError(409, `${time} has already been decided`);
    return i;
  }

  private relax(id: string): RelaxGoalResponse {
    const g = this.goals.find((x) => x.id === id);
    if (!g) throw new MockHttpError(404, "Goal not found");
    if (g.type !== "reserve" || g.mwh == null || !g.by) throw new MockHttpError(409, "only an energy reserve goal can be relaxed");
    const startMin = this.done.length * 15;
    const hours = Math.max(0, (timeToIndex(g.by) * 15 - startMin) / 60 - 0.5);
    const reachable = Math.min(CAPACITY, this.soc() + P_MAX * ETA * hours);
    const next = Math.min(Math.floor(reachable * 2) / 2, g.mwh - 0.5);
    if (next < 0.5) throw new MockHttpError(409, "there is not enough time left to store a useful amount; remove the goal instead");
    const before = g.source_text;
    g.mwh = next;
    g.source_text = g.source_text.replace(/\d+(?:[.,]\d+)?(?=\s*MWh)/i, String(next));
    if (g.source_text === before) g.source_text = `Store ${next} MWh by ${g.by}`;
    for (const a of this.alerts.list()) if (a.goal_id === id) this.alerts.ignore(a.id);
    this.audit.add("goal_relaxed", `Relaxed '${before}' to '${g.source_text}'`, this.now(), { goal_id: id, from: before, to: g.source_text });
    return { goals: this.listGoals(), message: `Now: ${g.source_text}.` };
  }

  private fastForward(count: number) {
    while (this.done.length < count && this.step());
  }

  // ---- goals ---------------------------------------------------------------

  private listGoals(): Goal[] {
    // Same rules as settlement/goals.py, replayed over the settled intervals.
    return trackGoals(this.goals, this.done, this.done.length >= 96, START_SOC);
  }

  // ---- metrics ---------------------------------------------------------------

  private profit() {
    return this.done.reduce((s, iv) => s + iv.settlement.profit_eur, 0);
  }

  private metrics(): Metrics {
    const goals = this.listGoals().filter((g) => g.type !== "max_profit");
    const sum = (f: (iv: Interval) => number) => this.done.reduce((s, iv) => s + f(iv), 0);
    const r2 = (v: number) => Math.round(v * 100) / 100;
    return {
      profit_today_eur: r2(this.profit()),
      energy_cost_eur: r2(sum((iv) => iv.settlement.energy_cost_eur)),
      wear_eur: r2(sum((iv) => iv.settlement.wear_eur)),
      no_battery_profit_eur: r2(sum((iv) => iv.settlement.no_battery_profit_eur)),
      value_added_eur: r2(sum((iv) => iv.settlement.value_added_eur)),
      energy_sold_mwh: sum((iv) => iv.settlement.grid_export_mwh),
      energy_bought_mwh: sum((iv) => iv.settlement.grid_import_mwh),
      curtailed_mwh: sum((iv) => iv.guard.curtail_mw * INTERVAL_H),
      decisions_total: this.done.length,
      decisions_llm: this.done.filter((iv) => iv.decision.source === "llm").length,
      decisions_fallback: this.done.filter((iv) => iv.decision.source === "fallback").length,
      decisions_clipped: this.done.filter((iv) => iv.guard.clipped).length,
      goals_met: goals.filter((g) => g.status === "met").length,
      goals_total: goals.length,
    };
  }

  // ---- simulation ------------------------------------------------------------

  private stop() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.speed = 0;
  }

  private run(speed: number) {
    if (speed !== this.speed)
      this.audit.add("sim_speed", speed <= 0 ? "Paused the simulation" : `Set the simulation speed to ${speed}x`, this.now(), {
        from: this.speed,
        to: speed,
      });
    this.stop();
    if (speed <= 0) return;
    if (this.done.length >= 96) this.newDay();
    this.speed = speed;
    const delay = Math.max(50, (15 * 60 * 1000) / speed);
    const tick = () => {
      const iv = this.step();
      if (!iv) return this.stop();
      this.listeners.forEach((l) => l(iv));
      if (this.done.length >= 96) return this.stop();
      this.timer = setTimeout(tick, delay);
    };
    this.timer = setTimeout(tick, delay);
  }

  subscribe(l: Listener, onAlert?: AlertListener) {
    this.listeners.add(l);
    if (onAlert) this.alertListeners.add(onAlert);
    return () => {
      this.listeners.delete(l);
      if (onAlert) this.alertListeners.delete(onAlert);
    };
  }

  // ---- routing -----------------------------------------------------------------

  handle(method: string, path: string, body?: unknown): unknown {
    const route = `${method} ${path.replace(/\/+$/, "")}`;
    if (route === "GET /api/state") {
      const s: StateSnapshot = {
        capacity_mwh: CAPACITY,
        max_power_mw: P_MAX,
        profit_today_eur: Math.round(this.profit() * 100) / 100,
        current: this.done[this.done.length - 1] ?? null,
        sim: { running: this.timer !== null, speed: this.speed },
      };
      return s;
    }
    if (route === "GET /api/day") {
      const d: DayResponse = {
        date: new Date().toISOString().slice(0, 10),
        now: this.done.length ? indexToTime(this.done.length - 1) : null,
        intervals: this.forecast.map((f, i) => this.done[i] ?? f),
      };
      return d;
    }
    if (route === "GET /api/goals") return this.listGoals();
    if (route === "POST /api/goals") {
      const text = String((body as { text?: string })?.text ?? "");
      const parsed = parseGoal(text);
      if ("question" in parsed) {
        const r: AddGoalResponse = { goals: this.listGoals(), question: parsed.question };
        return r;
      }
      if (parsed.goal.type === "max_profit") this.goals = this.goals.filter((g) => g.type !== "max_profit");
      this.goals.push({ ...parsed.goal, id: `g${this.nextId++}` });
      this.audit.add("goal_added", `Added goal '${text}'`, this.now(), { text });
      const r: AddGoalResponse = { goals: this.listGoals() };
      return r;
    }
    const del = /^DELETE \/api\/goals\/(.+)$/.exec(route);
    if (del) {
      const id = decodeURIComponent(del[1]);
      const gone = this.goals.find((g) => g.id === id);
      if (!gone) throw new MockHttpError(404, "Goal not found");
      this.goals = this.goals.filter((g) => g.id !== id);
      this.audit.add("goal_removed", `Removed goal '${gone.source_text}'`, this.now(), { goal_id: id });
      return { ok: true };
    }
    if (route === "GET /api/metrics") return this.metrics();
    if (route === "POST /api/sim/run") {
      this.run(Number((body as { speed?: number })?.speed ?? 60));
      return { running: this.timer !== null, speed: this.speed };
    }
    if (route === "POST /api/sim/reset") {
      this.audit.add("sim_reset", "Restarted the day at 00:00", this.now());
      this.stop();
      this.newDay();
      this.fastForward(1);
      return { running: false, speed: 0 };
    }

    // ---- operator desk ----
    const [pathOnly, query = ""] = route.split("?");
    const params = new URLSearchParams(query);
    if (pathOnly === "GET /api/alerts") return this.alerts.list();
    let m = /^POST \/api\/alerts\/(.+)\/ignore$/.exec(pathOnly);
    if (m) {
      const a = this.alerts.ignore(decodeURIComponent(m[1]));
      if (!a) throw new MockHttpError(404, "Alert not found");
      this.audit.add("alert_ignored", `Ignored alert: ${a.title}`, this.now(), { alert_id: a.id, kind: a.kind });
      return { ok: true };
    }
    m = /^POST \/api\/goals\/(.+)\/relax$/.exec(pathOnly);
    if (m) return this.relax(decodeURIComponent(m[1]));
    if (pathOnly === "GET /api/upcoming") return this.upcoming(Number(params.get("n") ?? 8));
    m = /^(PUT|DELETE) \/api\/overrides\/(.+)$/.exec(pathOnly);
    if (m) {
      const time = decodeURIComponent(m[2]);
      this.futureIndex(time);
      if (m[1] === "DELETE") {
        if (!this.overrides.delete(time)) throw new MockHttpError(404, "No override for that interval");
        this.audit.add("override_cleared", `Gave ${time} back to the AI`, this.now(), { time });
        return { ok: true };
      }
      const b = body as { battery_fraction?: number; note?: string };
      const fraction = Math.max(-1, Math.min(1, Number(b?.battery_fraction ?? 0)));
      const o: Override = { time, battery_fraction: fraction, note: String(b?.note ?? "").trim(), set_at: new Date().toISOString() };
      this.overrides.set(time, o);
      const what = fraction === 0 ? "hold" : `${fraction > 0 ? "charge" : "discharge"} at ${Math.round(Math.abs(fraction) * 100)}%`;
      this.audit.add("override_set", `Set ${time} to ${what}${o.note ? ` (${o.note})` : ""}`, this.now(), { time, battery_fraction: fraction });
      return o;
    }
    if (pathOnly === "GET /api/flags") return [...this.flags.values()].sort((a, b) => a.time.localeCompare(b.time));
    m = /^(POST|DELETE) \/api\/decisions\/(.+)\/flag$/.exec(pathOnly);
    if (m) {
      const time = decodeURIComponent(m[2]);
      if (m[1] === "DELETE") {
        if (!this.flags.delete(time)) throw new MockHttpError(404, "Not flagged");
        this.audit.add("decision_unflagged", `Removed the flag on ${time}`, this.now(), { time });
        return { ok: true };
      }
      if (!this.done.some((iv) => iv.time === time)) throw new MockHttpError(404, "Interval not decided");
      const reason = String((body as { reason?: string })?.reason ?? "").trim();
      const f: DecisionFlag = { time, reason, flagged_at: new Date().toISOString() };
      this.flags.set(time, f);
      this.audit.add("decision_flagged", `Flagged the ${time} decision: ${reason || "no reason given"}`, this.now(), { time, reason });
      return f;
    }
    if (pathOnly === "GET /api/audit") return this.audit.list(Number(params.get("limit") ?? 200));
    throw new MockHttpError(404, `No mock for ${route}`);
  }
}

export class MockHttpError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export const mockBackend = new MockBackend();
