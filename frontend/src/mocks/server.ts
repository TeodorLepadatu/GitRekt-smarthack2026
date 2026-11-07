/**
 * In-browser mock of the FastAPI backend. Implements the same routes and
 * shapes as the real API (see src/api/types.ts). Enabled with VITE_USE_MOCKS=true.
 */
import type {
  AddGoalResponse,
  DayResponse,
  Goal,
  GoalStatus,
  Interval,
  Metrics,
  StateSnapshot,
} from "@/api/types";
import { INTERVAL_H, indexToTime, timeToIndex } from "@/lib/utils";
import { buildForecast } from "./forecast";
import { parseGoal } from "./goalParser";
import { CAPACITY, ETA, P_MAX, runCycle } from "./simulator";

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

  private goalStatus(g: Omit<Goal, "status">): Goal {
    const soc = this.soc();
    const nowMin = this.done.length * 15; // end of the latest interval
    if (g.type === "max_profit") {
      return { ...g, status: this.profit() >= 0 ? "on_track" : "behind" };
    }
    if (g.type === "min_sell_price") {
      const broken = this.done.some(
        (iv) => iv.price_eur_mwh < (g.eur_per_mwh ?? 0) && iv.settlement.grid_export_mwh > 0.001,
      );
      const status: GoalStatus = broken ? "behind" : this.done.length >= 96 ? "met" : "on_track";
      return { ...g, status };
    }
    const mwh = g.mwh ?? 0;
    const byMin = timeToIndex(g.by ?? "24:00") * 15;
    const hoursLeft = Math.max(0, (byMin - nowMin) / 60);
    let status: GoalStatus;
    if (nowMin >= byMin) {
      // deadline passed: judge on the energy stored at the deadline
      const at = this.done[Math.min(this.done.length, byMin / 15) - 1];
      const reached = (at?.soc_mwh ?? soc) >= mwh - 0.01;
      status = reached ? (soc >= mwh - 0.01 ? "met" : "behind") : "unreachable";
    } else if (soc >= mwh - 0.01) {
      status = "met";
    } else {
      const possible = ETA * P_MAX * hoursLeft;
      const need = mwh - soc;
      status = need > possible ? "unreachable" : need > possible * 0.6 ? "behind" : "on_track";
    }
    return {
      ...g,
      status,
      stored_mwh: Math.round(soc * 100) / 100,
      need_mwh: Math.round(Math.max(0, mwh - soc) * 100) / 100,
      hours_left: Math.round(hoursLeft * 100) / 100,
    };
  }

  private listGoals(): Goal[] {
    return this.goals.map((g) => this.goalStatus(g));
  }

  // ---- metrics ---------------------------------------------------------------

  private profit() {
    return this.done.reduce((s, iv) => s + iv.settlement.profit_eur, 0);
  }

  private metrics(): Metrics {
    const goals = this.listGoals().filter((g) => g.type !== "max_profit");
    const sum = (f: (iv: Interval) => number) => this.done.reduce((s, iv) => s + f(iv), 0);
    return {
      profit_today_eur: Math.round(this.profit() * 100) / 100,
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
