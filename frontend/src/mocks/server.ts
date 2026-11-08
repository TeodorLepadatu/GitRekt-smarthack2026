/**
 * In-browser mock of the FastAPI backend. Implements the same routes and
 * shapes as the real API (see src/api/types.ts). Enabled with VITE_USE_MOCKS=true.
 */
import type {
  AddGoalResponse,
  Alert,
  Autonomy,
  AutonomyMode,
  ControlState,
  DayResponse,
  DecisionFlag,
  Goal,
  Interval,
  IntervalControl,
  Metrics,
  IntervalPlan,
  PendingApproval,
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
import { CAPACITY, ETA, P_MAX, previewCycle, runCommand, runCycle, type MockCommand } from "./simulator";

const START_SOC = 4.0;
/** Where the demo day starts on page load: just before the noon negative prices. */
const START_INDEX = 45;

const DEFAULT_GOALS: Omit<Goal, "status">[] = [
  { id: "g0", type: "max_profit", hard: false, source_text: "Maximise profit" },
  { id: "g1", type: "reserve", hard: true, mwh: 6, by: "18:00", source_text: "Store 6 MWh by 18:00" },
  { id: "g2", type: "min_sell_price", hard: true, eur_per_mwh: 50, source_text: "Never sell below €50/MWh" },
];

const DEFAULT_AUTONOMY: Autonomy = { mode: "autonomous", max_auto_mw: 1.5, ask_on_low_confidence: true, timeout_s: 60 };
const MODES: AutonomyMode[] = ["autonomous", "supervised", "advisory"];

/** Same rule as control/operator.py Autonomy.needs_review. */
function needsReview(a: Autonomy, mw: number, confidence: string): boolean {
  if (a.mode === "autonomous" || Math.abs(mw) < 0.005) return false;
  if (a.mode === "advisory") return true;
  return Math.abs(mw) > a.max_auto_mw + 1e-9 || (a.ask_on_low_confidence && confidence === "low");
}

/** "HH:MM" -> interval index, with "24:00" = 96 (the end of the day). */
const indexOf = (t: string) => (t === "24:00" ? 96 : timeToIndex(t));

type Listener = (iv: Interval) => void;
type AlertListener = (a: Alert) => void;

const TIME_RE = /^([01]\d|2[0-3]):(00|15|30|45)$/;
type ControlListener = (c: ControlState) => void;
type Verdict = "approved" | "rejected" | "timeout" | "superseded";

interface Waiting {
  id: number;
  /** The AI's interval, run as it is if approved. */
  iv: Interval;
  deadline: number;
  timer: ReturnType<typeof setTimeout>;
}

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
  private plans = new Map<string, IntervalPlan>();
  private flags = new Map<string, DecisionFlag>();
  private alerts = new AlertBook();
  private audit = new MockAudit();

  // operator control (control/operator.py)
  private hold = false;
  private override: { setpoint_mw: number; until: string } | null = null;
  private autonomy: Autonomy = { ...DEFAULT_AUTONOMY };
  private waiting: Waiting | null = null;
  private nextApprovalId = 1;
  private controlListeners = new Set<ControlListener>();

  constructor() {
    this.fastForward(START_INDEX + 1);
  }

  private now() {
    return this.done.length ? this.done[this.done.length - 1].time : null;
  }

  private soc() {
    return this.done.length ? this.done[this.done.length - 1].soc_mwh : START_SOC;
  }

  /** Decide the next interval. Null when the day is over, or while the AI's proposal waits for the operator. */
  private step(): Interval | null {
    const i = this.done.length;
    if (i >= 96 || this.waiting) return null;
    const cmd = this.commandFor(i);
    if (cmd) return this.push(runCommand(i, this.forecast[i], this.soc(), this.goals as Goal[], cmd), cmd.kind as IntervalControl["action"]);
    // an interval set ahead of time on the Day tab: the AI is not asked
    const plan = this.plans.get(indexToTime(i));
    if (plan) return this.push(runCycle(i, this.forecast[i], this.soc(), this.goals as Goal[], plan), "override");
    const iv = runCycle(i, this.forecast[i], this.soc(), this.goals as Goal[]);
    const sp = iv.guard.battery_setpoint_mw;
    if (!needsReview(this.autonomy, sp, iv.decision.confidence)) return this.push(iv, "auto");
    const id = this.nextApprovalId++;
    const timeout = this.autonomy.timeout_s * 1000;
    this.waiting = { id, iv, deadline: Date.now() + timeout, timer: setTimeout(() => this.resolve("timeout"), timeout) };
    this.emitControl();
    return null;
  }

  private push(iv: Interval, action: IntervalControl["action"], proposed_mw: number | null = null) {
    iv.control = { action, mode: this.autonomy.mode, proposed_mw };
    this.done.push(iv);
    const raised = this.alerts.update(alertConditions(this.done, this.listGoals(), this.forecast), iv.time);
    raised.forEach((a) => this.alertListeners.forEach((l) => l(a)));
    return iv;
  }

  private newDay() {
    this.done = [];
    this.plans.clear();
    this.flags.clear();
    this.alerts.clear();
  }

  private upcoming(n: number): UpcomingResponse {
    let soc = this.soc();
    const slots: UpcomingSlot[] = [];
    for (let i = this.done.length; i < Math.min(96, this.done.length + n); i++) {
      const t = indexToTime(i);
      const o = this.plans.get(t) ?? null;
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

  // ---- operator control ---------------------------------------------------

  private commandFor(i: number): MockCommand | null {
    if (this.hold) return { kind: "hold" };
    if (this.override && i >= indexOf(this.override.until)) {
      this.override = null; // ran its course: back to the AI
      this.emitControl();
    }
    return this.override && { kind: "override", ...this.override };
  }

  /** Answer the waiting proposal; the interval it was for is decided now. */
  private resolve(verdict: Verdict) {
    const w = this.waiting;
    if (!w) return;
    clearTimeout(w.timer);
    this.waiting = null;
    const i = this.done.length;
    const proposed = w.iv.guard.battery_setpoint_mw;
    const run = (cmd: MockCommand) => runCommand(i, this.forecast[i], this.soc(), this.goals as Goal[], cmd);
    const cmd = verdict === "superseded" ? this.commandFor(i) : null;
    let iv: Interval;
    if (verdict === "approved") iv = this.push(w.iv, "approved", proposed);
    else if (cmd) iv = this.push(run(cmd), cmd.kind as IntervalControl["action"], proposed);
    else if (verdict === "rejected") iv = this.push(run({ kind: "reject", proposed_mw: proposed }), "rejected", proposed);
    else iv = this.push(run({ kind: "timeout", proposed_mw: proposed }), "timeout", proposed);
    this.emitControl();
    this.listeners.forEach((l) => l(iv));
    if (this.speed > 0) this.schedule(); // the clock waited with the AI; it runs on now
  }

  private controlState(): ControlState {
    const w = this.waiting;
    const pending: PendingApproval | null = w && {
      id: w.id,
      time: w.iv.time,
      setpoint_mw: w.iv.guard.battery_setpoint_mw,
      source: w.iv.decision.source,
      confidence: w.iv.decision.confidence,
      explanation_for_operator: w.iv.decision.explanation_for_operator,
      expires_in_s: Math.max(0, Math.round((w.deadline - Date.now()) / 100) / 10),
    };
    return { hold: this.hold, override: this.override && { ...this.override }, autonomy: { ...this.autonomy }, pending };
  }

  private emitControl() {
    const c = this.controlState();
    this.controlListeners.forEach((l) => l(c));
  }

  private setOverride(body: unknown): ControlState {
    const { setpoint_mw, until } = (body ?? {}) as { setpoint_mw?: unknown; until?: unknown };
    const next = this.done.length;
    if (typeof setpoint_mw !== "number" || !Number.isFinite(setpoint_mw) || Math.abs(setpoint_mw) > P_MAX + 1e-9)
      throw new MockHttpError(422, `setpoint must be between -${P_MAX} and ${P_MAX} MW`);
    if (typeof until !== "string" || !/^\d{2}:\d{2}$/.test(until) || (until !== "24:00" && indexToTime(timeToIndex(until)) !== until))
      throw new MockHttpError(422, `${String(until)} is not the start of an interval`);
    if (next >= 96) throw new MockHttpError(422, "the day is over");
    if (indexOf(until) <= next) throw new MockHttpError(422, `${until} has already passed: the next interval starts at ${indexToTime(next)}`);
    this.override = { setpoint_mw: Math.round(setpoint_mw * 1e4) / 1e4, until };
    this.resolve("superseded");
    this.emitControl();
    return this.controlState();
  }

  private setAutonomy(body: unknown): ControlState {
    const b = (body ?? {}) as Partial<Autonomy>;
    const next: Autonomy = { ...this.autonomy, ...Object.fromEntries(Object.entries(b).filter(([, v]) => v != null)) };
    if (!MODES.includes(next.mode)) throw new MockHttpError(422, `mode must be one of ${MODES.join(", ")}`);
    if (!(next.max_auto_mw >= 0)) throw new MockHttpError(422, "max_auto_mw must be a number >= 0");
    if (!(next.timeout_s >= 5 && next.timeout_s <= 900)) throw new MockHttpError(422, "timeout_s must be between 5 and 900");
    this.autonomy = next;
    const w = this.waiting;
    if (w && !needsReview(next, w.iv.guard.battery_setpoint_mw, w.iv.decision.confidence)) this.resolve("approved");
    this.emitControl();
    return this.controlState();
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
    const count = (f: (iv: Interval) => boolean) => this.done.filter(f).length;
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
      decisions_llm: count((iv) => iv.decision.source === "llm"),
      decisions_fallback: count((iv) => iv.decision.source === "fallback"),
      decisions_operator: count((iv) => iv.decision.source === "operator"),
      decisions_clipped: count((iv) => iv.guard.clipped),
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

  private schedule() {
    if (this.timer) clearTimeout(this.timer);
    const delay = Math.max(50, (15 * 60 * 1000) / this.speed);
    this.timer = setTimeout(() => {
      this.timer = null;
      const iv = this.step();
      if (iv) this.listeners.forEach((l) => l(iv));
      if (this.done.length >= 96) return this.stop();
      if (!this.waiting) this.schedule(); // while a proposal waits, resolve() restarts the clock
    }, delay);
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
    if (!this.waiting) this.schedule();
  }

  subscribe(l: Listener) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }

  subscribeAlerts(l: AlertListener) {
    this.alertListeners.add(l);
    return () => this.alertListeners.delete(l);
  }

  subscribeControl(l: ControlListener) {
    this.controlListeners.add(l);
    return () => this.controlListeners.delete(l);
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
        sim: { running: this.speed > 0, speed: this.speed },
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

    if (route === "GET /api/control") return this.controlState();
    if (route === "POST /api/override") {
      const out = this.setOverride(body);
      const o = this.override!;
      const verb = o.setpoint_mw > 0.005 ? `charge ${o.setpoint_mw.toFixed(2)} MW` : o.setpoint_mw < -0.005 ? `discharge ${(-o.setpoint_mw).toFixed(2)} MW` : "hold";
      this.audit.add("control_override", `Took over: ${verb} until ${o.until}`, this.now(), { ...o });
      return out;
    }
    if (route === "DELETE /api/override") {
      this.override = null;
      this.emitControl();
      this.audit.add("control_released", "Gave control back to the AI", this.now());
      return this.controlState();
    }
    if (route === "POST /api/hold" || route === "DELETE /api/hold") {
      this.hold = method === "POST";
      if (this.hold) this.resolve("superseded");
      this.emitControl();
      this.audit.add("emergency_hold", this.hold ? "Emergency hold on" : "Emergency hold released", this.now(), { on: this.hold });
      return this.controlState();
    }
    if (route === "PUT /api/autonomy") {
      const out = this.setAutonomy(body);
      const what = Object.entries((body ?? {}) as Record<string, unknown>)
        .filter(([, v]) => v != null)
        .map(([k, v]) => `${k} = ${String(v)}`)
        .join(", ");
      this.audit.add("autonomy_changed", `Changed AI autonomy: ${what}`, this.now(), { ...(body as object) });
      return out;
    }
    const approval = /^POST \/api\/approvals\/(\d+)$/.exec(route);
    if (approval) {
      if (this.waiting?.id !== Number(approval[1])) throw new MockHttpError(409, "that proposal is no longer waiting for an answer");
      const approve = Boolean((body as { approve?: boolean })?.approve);
      this.resolve(approve ? "approved" : "rejected");
      this.audit.add("approval_answered", `${approve ? "Approved" : "Rejected"} the AI's proposal #${approval[1]}`, this.now(), {
        proposal_id: Number(approval[1]),
        approve,
      });
      return this.controlState();
    }

    if (route === "POST /api/sim/run") {
      this.run(Number((body as { speed?: number })?.speed ?? 60));
      return { running: this.speed > 0, speed: this.speed };
    }
    if (route === "POST /api/sim/reset") {
      this.audit.add("sim_reset", "Restarted the day at 00:00", this.now());
      this.stop();
      if (this.waiting) clearTimeout(this.waiting.timer);
      this.waiting = null;
      this.override = null; // the old day's clock; an emergency hold stays until released
      this.newDay();
      this.fastForward(1);
      this.emitControl();
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
    m = /^(PUT|DELETE) \/api\/plan\/(.+)$/.exec(pathOnly);
    if (m) {
      const time = decodeURIComponent(m[2]);
      this.futureIndex(time);
      if (m[1] === "DELETE") {
        if (!this.plans.delete(time)) throw new MockHttpError(404, "No override for that interval");
        this.audit.add("override_cleared", `Gave ${time} back to the AI`, this.now(), { time });
        return { ok: true };
      }
      const b = body as { battery_fraction?: number; note?: string };
      const fraction = Math.max(-1, Math.min(1, Number(b?.battery_fraction ?? 0)));
      const o: IntervalPlan = { time, battery_fraction: fraction, note: String(b?.note ?? "").trim(), set_at: new Date().toISOString() };
      this.plans.set(time, o);
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
