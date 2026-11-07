/**
 * TypeScript port of the Python settlement package (settlement/settle.py and
 * settlement/goals.py) and of the site simulator's grid rule (simulator/site.py),
 * so the mock dashboard shows the same money and goal statuses the real backend will.
 *
 *   curtailed = export limiter: if curtailment is asked for, all production that
 *               would otherwise be exported (sized on the actual surplus), else 0
 *   grid    = (production - curtailed) - demand - battery   (> 0 export, < 0 import)
 *   energy  = (export - import) x price
 *   wear    = - 2.5 EUR x |battery| x 0.25 h
 *   profit  = energy + wear
 */
import type { Goal, GoalStatus, Interval, Settlement } from "@/api/types";
import { INTERVAL_H, timeToIndex } from "@/lib/utils";
import { CAPACITY, ETA, P_MAX } from "./simulator";

export const WEAR_EUR_PER_MWH = 2.5;
const BUFFER_MIN = 30;
const TOL_MWH = 0.01;
const DAY_MIN = 24 * 60;
const STEP_MIN = INTERVAL_H * 60;

const r2 = (v: number) => Math.round(v * 100) / 100;
const r4 = (v: number) => Math.round(v * 10000) / 10000;
const mod = (v: number) => ((v % DAY_MIN) + DAY_MIN) % DAY_MIN;
const toMin = (t: string) => (t === "24:00" ? DAY_MIN : timeToIndex(t) * 15);
const hhmm = (min: number) => {
  const m = mod(Math.round(min));
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
};
const inWindow = (now: number, start: number, end: number) =>
  start < end ? now >= start && now < end : now >= start || now < end;

export function settle(price: number, demand: number, production: number, batteryMw: number, curtailMw: number): Settlement {
  const prod = Math.max(0, production);
  // Same rule as simulator/site.py: curtailment is an export limiter on production.
  const curtail = curtailMw > 0.001 ? Math.min(prod, Math.max(0, prod - demand - batteryMw)) : 0;
  const grid = prod - curtail - demand - batteryMw;
  const exp = Math.max(0, grid) * INTERVAL_H;
  const imp = Math.max(0, -grid) * INTERVAL_H;
  const energy = (exp - imp) * price;
  const wear = -WEAR_EUR_PER_MWH * Math.abs(batteryMw) * INTERVAL_H;
  const profit = energy + wear;
  const noBattery = (prod - demand) * INTERVAL_H * price;
  return {
    grid_import_mwh: r4(imp),
    grid_export_mwh: r4(exp),
    energy_cost_eur: r2(energy),
    wear_eur: r2(wear),
    profit_eur: r2(profit),
    no_battery_profit_eur: r2(noBattery),
    value_added_eur: r2(profit - noBattery),
  };
}

type GoalDef = Omit<Goal, "status">;

interface State {
  status: GoalStatus;
  reached_at: string | null;
  violated_at: string | null;
  violations: number;
  blocked: number;
  need: number | null;
  hoursLeft: number | null;
}

function reserveStep(g: GoalDef, st: State, iv: Interval) {
  const target = g.mwh!;
  const by = toMin(g.by!);
  let until = g.until ? toMin(g.until) : DAY_MIN;
  if (until === by) until = DAY_MIN; // same assumption as the guard: protect until midnight
  const start = toMin(iv.time);
  const soc = iv.soc_mwh;
  const battery = iv.guard.battery_setpoint_mw;

  if (inWindow(start, by, until)) {
    if (battery < 0 && soc < target - TOL_MWH) {
      if (st.status !== "violated") st.violated_at = iv.time;
      st.status = "violated";
      st.violations += 1;
    }
    if (st.status !== "missed" && st.status !== "violated" && soc >= target - TOL_MWH) st.status = "met";
    st.need = Math.max(0, target - soc);
    st.hoursLeft = 0;
    return;
  }

  const toDeadline = mod(by - start);
  const deadlineNow = toDeadline <= STEP_MIN;
  const minutesLeft = Math.max(0, toDeadline - STEP_MIN);
  const need = Math.max(0, target - soc);
  st.need = need;
  st.hoursLeft = minutesLeft / 60;
  if (st.status === "missed" || st.status === "violated") return;
  if (need <= TOL_MWH) {
    st.reached_at ??= hhmm(start + STEP_MIN);
    st.status = "met";
    return;
  }
  st.reached_at = null; // dropped back below before the deadline
  if (deadlineNow) {
    st.status = "missed";
    return;
  }
  const fullPowerMin = (need / (ETA * P_MAX)) * 60;
  if (target > CAPACITY + TOL_MWH || fullPowerMin > minutesLeft + 1e-6) st.status = "unreachable";
  else if (fullPowerMin + BUFFER_MIN > minutesLeft) st.status = "behind";
  else st.status = "on_track";
}

function minSellStep(g: GoalDef, st: State, iv: Interval) {
  if (iv.price_eur_mwh >= g.eur_per_mwh!) return;
  if (iv.settlement.grid_export_mwh > 1e-6) {
    if (st.status !== "violated") st.violated_at = iv.time;
    st.status = "violated";
    st.violations += 1;
  } else st.blocked += 1;
}

function message(g: GoalDef, st: State): string {
  if (g.type === "min_sell_price") {
    return st.status === "violated"
      ? `Sold below €${g.eur_per_mwh}/MWh ${st.violations}× (first at ${st.violated_at})`
      : `Never sold below €${g.eur_per_mwh}/MWh (${st.blocked} intervals held back)`;
  }
  switch (st.status) {
    case "violated":
      return `Reserve broken at ${st.violated_at}: discharged below ${g.mwh} MWh`;
    case "missed":
      return `${g.mwh} MWh not reached by ${g.by}`;
    case "met":
      return st.reached_at ? `${g.mwh} MWh reached at ${st.reached_at}` : `Holding ${g.mwh} MWh`;
    case "unreachable":
      return `Can no longer reach ${g.mwh} MWh by ${g.by}`;
    default:
      return `Needs +${(st.need ?? 0).toFixed(2)} MWh in ${(st.hoursLeft ?? 0).toFixed(2)} h`;
  }
}

/**
 * Replay the settled intervals and judge every goal, like settlement.GoalTracker.
 * `dayOver` = all 96 intervals ran (an unbroken min-sell goal then counts as met).
 */
export function trackGoals(goals: GoalDef[], done: Interval[], dayOver: boolean, startSoc: number): Goal[] {
  const soc = done.length ? done[done.length - 1].soc_mwh : startSoc;
  return goals.map((g) => {
    if (g.type === "max_profit") {
      const profit = done.reduce((s, iv) => s + iv.settlement.profit_eur, 0);
      return { ...g, status: profit >= 0 ? (dayOver ? "met" : "on_track") : "behind" };
    }
    const st: State = { status: "on_track", reached_at: null, violated_at: null, violations: 0, blocked: 0, need: null, hoursLeft: null };
    for (const iv of done) {
      if (g.type === "reserve" && g.mwh != null && g.by) reserveStep(g, st, iv);
      else if (g.type === "min_sell_price" && g.eur_per_mwh != null) minSellStep(g, st, iv);
    }
    if (dayOver && g.type === "min_sell_price" && st.status === "on_track") st.status = "met";
    if (g.type === "reserve" && g.mwh != null && st.need == null) {
      // nothing settled yet: progress from the start SoC
      st.need = Math.max(0, g.mwh - soc);
      st.hoursLeft = g.by ? Math.max(0, toMin(g.by) - done.length * 15) / 60 : null;
    }
    const out: Goal = { ...g, status: st.status, message: message(g, st), reached_at: st.reached_at, violated_at: st.violated_at };
    if (g.type === "reserve") {
      out.stored_mwh = r2(soc);
      out.need_mwh = r2(st.need ?? 0);
      out.hours_left = r2(st.hoursLeft ?? 0);
    }
    return out;
  });
}
