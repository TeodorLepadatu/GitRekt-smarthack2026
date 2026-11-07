/**
 * In-browser mock of the FastAPI backend. Implements the same routes and
 * shapes as the real API (see src/api/types.ts). Enabled with VITE_USE_MOCKS=true.
 */
import type {
  AddGoalResponse,
  Autonomy,
  AutonomyMode,
  ControlState,
  DayResponse,
  Goal,
  Interval,
  IntervalControl,
  Metrics,
  PendingApproval,
  StateSnapshot,
} from "@/api/types";
import { INTERVAL_H, indexToTime, timeToIndex } from "@/lib/utils";
import { buildForecast } from "./forecast";
import { parseGoal } from "./goalParser";
import { trackGoals } from "./settlement";
import { CAPACITY, P_MAX, runCommand, runCycle, type MockCommand } from "./simulator";

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

  private soc() {
    return this.done.length ? this.done[this.done.length - 1].soc_mwh : START_SOC;
  }

  /** Decide the next interval. Null when the day is over, or while the AI's proposal waits for the operator. */
  private step(): Interval | null {
    const i = this.done.length;
    if (i >= 96 || this.waiting) return null;
    const cmd = this.commandFor(i);
    if (cmd) return this.push(runCommand(i, this.forecast[i], this.soc(), this.goals as Goal[], cmd), cmd.kind as IntervalControl["action"]);
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
    return iv;
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
    this.stop();
    if (speed <= 0) return;
    if (this.done.length >= 96) this.done = [];
    this.speed = speed;
    if (!this.waiting) this.schedule();
  }

  subscribe(l: Listener) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
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
      const r: AddGoalResponse = { goals: this.listGoals() };
      return r;
    }
    const del = /^DELETE \/api\/goals\/(.+)$/.exec(route);
    if (del) {
      const before = this.goals.length;
      this.goals = this.goals.filter((g) => g.id !== decodeURIComponent(del[1]));
      if (this.goals.length === before) throw new MockHttpError(404, "Goal not found");
      return { ok: true };
    }
    if (route === "GET /api/metrics") return this.metrics();

    if (route === "GET /api/control") return this.controlState();
    if (route === "POST /api/override") return this.setOverride(body);
    if (route === "DELETE /api/override") {
      this.override = null;
      this.emitControl();
      return this.controlState();
    }
    if (route === "POST /api/hold" || route === "DELETE /api/hold") {
      this.hold = method === "POST";
      if (this.hold) this.resolve("superseded");
      this.emitControl();
      return this.controlState();
    }
    if (route === "PUT /api/autonomy") return this.setAutonomy(body);
    const approval = /^POST \/api\/approvals\/(\d+)$/.exec(route);
    if (approval) {
      if (this.waiting?.id !== Number(approval[1])) throw new MockHttpError(409, "that proposal is no longer waiting for an answer");
      this.resolve((body as { approve?: boolean })?.approve ? "approved" : "rejected");
      return this.controlState();
    }

    if (route === "POST /api/sim/run") {
      this.run(Number((body as { speed?: number })?.speed ?? 60));
      return { running: this.speed > 0, speed: this.speed };
    }
    if (route === "POST /api/sim/reset") {
      this.stop();
      if (this.waiting) clearTimeout(this.waiting.timer);
      this.waiting = null;
      this.override = null; // the old day's clock; an emergency hold stays until released
      this.done = [];
      this.fastForward(1);
      this.emitControl();
      return { running: false, speed: 0 };
    }
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
