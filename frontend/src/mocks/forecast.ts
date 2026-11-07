import type { ForecastInterval } from "@/api/types";
import { indexToTime } from "@/lib/utils";

/** Small seeded PRNG so the mock day looks the same on every reload. */
function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const gauss = (x: number, mu: number, sigma: number) => Math.exp(-((x - mu) ** 2) / (2 * sigma ** 2));
const r2 = (v: number) => Math.round(v * 100) / 100;

export const BREAK_EVEN = 148;

/** A realistic spring day: negative prices at noon, a strong evening peak. */
export function buildForecast(seed = 7): ForecastInterval[] {
  const rnd = mulberry32(seed);
  const noise = (amp: number) => (rnd() * 2 - 1) * amp;
  const out: ForecastInterval[] = [];
  for (let i = 0; i < 96; i++) {
    const h = i / 4 + 0.125;
    const price =
      82 +
      78 * gauss(h, 8, 1.1) -
      22 * gauss(h, 3.5, 2) -
      118 * gauss(h, 12.75, 1.15) +
      125 * gauss(h, 19.5, 1.5) +
      noise(5);
    const sunArc = Math.max(0, Math.sin((Math.PI * (h - 6.25)) / 13.25));
    const cloud = 0.85 + 0.15 * Math.sin(h * 2.1) + noise(0.05);
    const solar = Math.max(0, 6.8 * sunArc ** 1.4 * cloud);
    const wind = Math.max(0, 2.6 + 0.9 * Math.sin((h / 24) * 2 * Math.PI + 1.2) + noise(0.3));
    const demand =
      3.1 + 0.9 * gauss(h, 8.5, 1.5) + 1.4 * gauss(h, 19, 2) + 0.5 * gauss(h, 13, 3) + noise(0.12);
    out.push({
      time: indexToTime(i),
      price_eur_mwh: Math.round(price * 10) / 10,
      break_even: BREAK_EVEN,
      demand_mw: r2(demand),
      production_mw: r2(solar + wind),
      solar_mw: r2(solar),
      wind_mw: r2(wind),
    });
  }
  return out;
}
