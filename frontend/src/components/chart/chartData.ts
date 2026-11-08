import { isDecided, type DayResponse, type Goal } from "@/api/types";
import { explanationText } from "@/lib/labels";
import { indexToTime, timeToIndex } from "@/lib/utils";

/**
 * One row per 15-minute boundary, x = 0..96 (x = 96 is 24:00).
 * Interval values (price, power) belong to the interval STARTING at x and are
 * drawn as steps; soc is the energy stored AT x (end of interval x-1).
 */
export interface ChartRow {
  x: number;
  time: string;
  price: number | null;
  breakEven: number | null;
  soc: number | null;
  solar: number | null;
  wind: number | null;
  demand: number | null;
  setpoint: number | null;
  explanation: string | null;
}

export function buildRows(day: DayResponse): ChartRow[] {
  const rows: ChartRow[] = [];
  const slots = day.intervals;
  for (let x = 0; x <= 96; x++) {
    // the 24:00 row repeats the last interval so step lines reach the edge
    const s = slots[Math.min(x, slots.length - 1)];
    const prev = x > 0 ? slots[x - 1] : undefined;
    const cur = x < slots.length ? slots[x] : undefined;
    rows.push({
      x,
      time: x === 96 ? "24:00" : indexToTime(x),
      price: s?.price_eur_mwh ?? null,
      breakEven: s?.break_even ?? null,
      soc: prev && isDecided(prev) ? prev.soc_mwh : null,
      solar: s?.solar_mw ?? null,
      wind: s?.wind_mw ?? null,
      demand: s?.demand_mw ?? null,
      setpoint: cur && isDecided(cur) ? cur.guard.battery_setpoint_mw : null,
      explanation: cur && isDecided(cur) ? explanationText(cur) : null,
    });
  }
  return rows;
}

/** Contiguous runs of negative price as [x1, x2) ranges. */
export function negativeRuns(day: DayResponse): [number, number][] {
  const runs: [number, number][] = [];
  let start: number | null = null;
  day.intervals.forEach((s, i) => {
    if (s.price_eur_mwh < 0 && start === null) start = i;
    if (s.price_eur_mwh >= 0 && start !== null) {
      runs.push([start, i]);
      start = null;
    }
  });
  if (start !== null) runs.push([start, day.intervals.length]);
  return runs;
}

/** "Now" = end of the latest decided interval. */
export function nowX(day: DayResponse): number | null {
  return day.now ? timeToIndex(day.now) + 1 : null;
}

export interface GoalMarker {
  id: string;
  x: number;
  mwh: number;
  label: string;
  status: Goal["status"];
}

export function goalMarkers(goals: Goal[]): GoalMarker[] {
  return goals
    .filter((g) => g.type === "reserve" && g.mwh != null && g.by)
    .map((g) => ({
      id: g.id,
      x: g.by === "24:00" ? 96 : timeToIndex(g.by!),
      mwh: g.mwh!,
      label: `${g.mwh} MWh by ${g.by}`,
      status: g.status,
    }));
}

export const xTicks = Array.from({ length: 13 }, (_, i) => i * 8);
export const xTickFormat = (x: number) => (x === 96 ? "24:00" : indexToTime(x));
