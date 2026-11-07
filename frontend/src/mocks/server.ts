/**
 * In-browser mock of the FastAPI backend. Implements the same routes and
 * shapes as the real API (see src/api/types.ts). Enabled with VITE_USE_MOCKS=true.
 */
import type {
  AddGoalResponse,
  DayResponse,
  Goal,
  Interval,
  Metrics,
  StateSnapshot,
} from "@/api/types";
import { INTERVAL_H, indexToTime } from "@/lib/utils";
import { buildForecast } from "./forecast";
import { parseGoal } from "./goalParser";
import { trackGoals } from "./settlement";
import { CAPACITY, P_MAX, runCycle } from "./simulator";

const START_SOC = 4.0;
/** Where the demo day starts on page load: just before the noon negative prices. */
const START_INDEX = 45;

const DEFAULT_GOALS: Omit<Goal, "status">[] = [
  { id: "g0", type: "max_profit", hard: false, source_text: "Maximise profit" },
  { id: "g1", type: "reserve", hard: true, mwh: 6, by: "18:00", source_text: "Store 6 MWh by 18:00" },
  { id: "g2", type: "min_sell_price", hard: true, eur_per_mwh: 50, source_text: "Never sell below €50/MWh" },
];

type Listener = (iv: Interval) => void;

class MockBackend {
  private forecast = buildForecast();
  private done: Interval[] = [];
  private goals: Omit<Goal, "status">[] = DEFAULT_GOALS.map((g) => ({ ...g }));
  private nextId = 3;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private speed = 0;
  private listeners = new Set<Listener>();

  constructor() {
    this.fastForward(START_INDEX + 1);
  }

  private soc() {
    return this.done.length ? this.done[this.done.length - 1].soc_mwh : START_SOC;
  }

  private step(): Interval | null {
    const i = this.done.length;
    if (i >= 96) return null;
    const iv = runCycle(i, this.forecast[i], this.soc(), this.goals as Goal[]);
    this.done.push(iv);
    return iv;
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
    this.stop();
    if (speed <= 0) return;
    if (this.done.length >= 96) this.done = [];
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

  subscribe(l: Listener) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
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
    if (route === "POST /api/sim/run") {
      this.run(Number((body as { speed?: number })?.speed ?? 60));
      return { running: this.timer !== null, speed: this.speed };
    }
    if (route === "POST /api/sim/reset") {
      this.stop();
      this.done = [];
      this.fastForward(1);
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
