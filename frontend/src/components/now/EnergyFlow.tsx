import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Interval } from "@/api/types";
import { actionKind, fmtMw, fmtPrice } from "@/lib/labels";

/**
 * Single-line schematic of where the power goes in this interval:
 * solar + wind -> site, site <-> battery, site <-> grid.
 * Dashes travel along each live line in the direction of the power, faster
 * for more MW; line weight and the battery level glide between intervals.
 */

const W = 600;
const H = 420;
const EPS = 0.01;
/** Discharge efficiency, sqrt of the 90 % round trip (guard/config.py). */
const ETA_D = Math.sqrt(0.9);

type Box = { x: number; y: number; w: number; h: number };
const B = {
  solar: { x: 4, y: 28, w: 150, h: 60 },
  wind: { x: 4, y: 244, w: 150, h: 60 },
  site: { x: 226, y: 130, w: 148, h: 72 },
  grid: { x: 446, y: 130, w: 150, h: 72 },
} satisfies Record<string, Box>;

/** The battery: a battery outline with a terminal, centred under the site. */
const BATT = { cx: 300, y: 312, w: 168, h: 58 };

/** Order in which the conductors are traced on page load. */
const DRAW_ORDER: Record<string, number> = { "m-solar": 0, "m-wind": 1, "m-batt": 2, "m-grid": 3 };
/** Order in which the boxes fade in. */
const BOX_ORDER: Record<string, number> = { Solar: 0, Wind: 1, "Site load": 2, Grid: 3 };

const weight = (mw: number) => (mw > EPS ? 2 + Math.min(mw, 8) * 1.1 : 1);
/** Seconds for one dash cycle: 2.4 s at a trickle, 0.5 s at 8 MW. */
const speed = (mw: number) => `${Math.max(0.5, 2.4 - Math.min(mw, 8) * 0.24).toFixed(2)}s`;

interface LineProps {
  d: string;
  mw: number;
  color: string;
  id: string;
  label: string;
  lx: number;
  ly: number;
  anchor?: "start" | "middle" | "end";
}

function Line({ d, mw, color, id, label, lx, ly, anchor = "middle" }: LineProps) {
  const on = mw > EPS;
  const w = weight(mw);
  return (
    <g>
      <defs>
        <marker id={id} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={color} />
        </marker>
      </defs>
      {/* the conductor */}
      <path
        d={d}
        fill="none"
        className={on ? "flow-ease draw" : "flow-ease"}
        pathLength={on ? 1 : undefined}
        style={{ ["--i" as string]: DRAW_ORDER[id] ?? 0 }}
        stroke={on ? color : "var(--rule-strong)"}
        strokeOpacity={on ? 0.35 : 1}
        strokeWidth={w}
        strokeDasharray={on ? undefined : "3 4"}
        markerEnd={on ? `url(#${id})` : undefined}
        strokeLinejoin="round"
      />
      {/* the power moving along it */}
      {on && (
        <path
          d={d}
          fill="none"
          className="flow-dash flow-ease"
          stroke={color}
          strokeWidth={Math.max(2.5, w * 0.7)}
          strokeLinejoin="round"
          style={{ ["--flow-dur" as string]: speed(mw) }}
        />
      )}
      <text x={lx} y={ly} textAnchor={anchor} className="num" fontSize={14} fontWeight={500} fill={on ? "var(--ink)" : "var(--ink-3)"}>
        {label}
      </text>
    </g>
  );
}

function Frame({ b, title, value, note }: { b: Box; title: string; value: string; note?: string }) {
  return (
    <g className="enter-fade" style={{ ["--i" as string]: BOX_ORDER[title] ?? 0 }}>
      <rect x={b.x} y={b.y} width={b.w} height={b.h} fill="var(--sheet)" stroke="var(--ink)" strokeWidth={1.25} />
      <text x={b.x + 10} y={b.y + 19} fontSize={11} fontWeight={600} letterSpacing="0.08em" fill="var(--ink-3)">
        {title.toUpperCase()}
      </text>
      <text x={b.x + 10} y={b.y + 41} fontSize={17} className="num" fontWeight={500} fill="var(--ink)">
        {value}
      </text>
      {note && (
        <text x={b.x + 10} y={b.y + 59} fontSize={12} fill="var(--ink-3)">
          {note}
        </text>
      )}
    </g>
  );
}

/** "1 h 32 min" */
function duration(h: number) {
  if (!isFinite(h)) return "a long time";
  const min = Math.round(h * 60);
  return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min` : `${min} min`;
}

interface BatteryProps {
  soc: number;
  capacity: number;
  kind: "charge" | "discharge" | "hold";
  /** Site demand now, for the hover read-out. */
  demandMw: number;
}

function Battery({ soc, capacity, kind, demandMw }: BatteryProps) {
  const [hover, setHover] = useState(false);
  const pct = Math.max(0, Math.min(1, soc / capacity));
  const x = BATT.cx - BATT.w / 2;
  const inner = BATT.w - 12;
  const state = kind === "charge" ? "charging" : kind === "discharge" ? "discharging" : "idle";
  const stateColor = kind === "charge" ? "var(--charge)" : kind === "discharge" ? "var(--discharge)" : "var(--ink-3)";
  // What the stored energy means on site: how long it alone could carry the whole load.
  const runtime = duration((soc * ETA_D) / Math.max(demandMw, 0.01));

  return (
    <motion.g
      onHoverStart={() => setHover(true)}
      onHoverEnd={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
      tabIndex={0}
      aria-label={`Battery ${Math.round(pct * 100)} percent full; would run the site for ${runtime}`}
      style={{ cursor: "pointer", transformBox: "fill-box", transformOrigin: "50% 30%", outline: "none" }}
      animate={{ scale: hover ? 1.06 : 1, rotate: hover ? -2 : 0 }}
      transition={{ type: "spring", stiffness: 320, damping: 18 }}
    >
      {/* body and terminal */}
      <rect x={x} y={BATT.y} width={BATT.w} height={BATT.h} rx={10} fill="var(--sheet)" stroke="var(--ink)" strokeWidth={2.5} />
      <rect x={x + BATT.w + 3} y={BATT.y + BATT.h / 2 - 11} width={8} height={22} rx={2.5} fill="var(--ink)" />
      {/* charge level */}
      <rect x={x + 6} y={BATT.y + 6} width={inner} height={BATT.h - 12} rx={6} fill="var(--wash)" />
      <rect x={x + 6} y={BATT.y + 6} width={inner * pct} height={BATT.h - 12} rx={6} fill="var(--soc)" className="flow-ease" />
      <text
        x={BATT.cx}
        y={BATT.y + BATT.h / 2 + 7}
        textAnchor="middle"
        fontSize={20}
        fontWeight={600}
        className="num"
        fill="var(--ink)"
        stroke="var(--sheet)"
        strokeWidth={4}
        paintOrder="stroke"
      >
        {Math.round(pct * 100)}%
      </text>
      <AnimatePresence mode="wait" initial={false}>
        {hover ? (
          <motion.g key="runtime" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.18 }}>
            <text x={BATT.cx} y={BATT.y + BATT.h + 24} textAnchor="middle" fontSize={15} fontWeight={600} className="num" fill="var(--ink)">
              Runs the site for {runtime}
            </text>
            <text x={BATT.cx} y={BATT.y + BATT.h + 42} textAnchor="middle" fontSize={13} fill="var(--ink-3)">
              on its own, at the current {fmtMw(demandMw)} load
            </text>
          </motion.g>
        ) : (
          <motion.g key="level" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.18 }}>
            <text x={BATT.cx} y={BATT.y + BATT.h + 24} textAnchor="middle" fontSize={15} fontWeight={600} className="num" fill="var(--ink)">
              Battery · {soc.toFixed(2)} / {capacity.toFixed(0)} MWh
            </text>
            <text x={BATT.cx} y={BATT.y + BATT.h + 42} textAnchor="middle" fontSize={13} fill={stateColor}>
              {state}
            </text>
          </motion.g>
        )}
      </AnimatePresence>
    </motion.g>
  );
}

export function EnergyFlow({ iv, capacityMwh }: { iv: Interval; capacityMwh: number }) {
  const curtail = iv.guard.curtail_mw;
  const used = Math.max(0, iv.production_mw - curtail);
  const share = iv.production_mw > 0 ? used / iv.production_mw : 0;
  const solar = iv.solar_mw * share;
  const wind = iv.wind_mw * share;
  const sp = iv.guard.battery_setpoint_mw;
  const kind = actionKind(sp);
  const grid = iv.demand_mw + sp - used; // > 0 buying, < 0 selling
  const buying = grid > EPS;
  const selling = grid < -EPS;

  const summary = `Solar ${fmtMw(solar)} and wind ${fmtMw(wind)} feed the site, which uses ${fmtMw(iv.demand_mw)}. Battery ${
    kind === "hold" ? "idle" : kind === "charge" ? `charging ${fmtMw(sp)}` : `discharging ${fmtMw(-sp)}`
  }. Grid ${buying ? `supplying ${fmtMw(grid)}` : selling ? `taking ${fmtMw(-grid)}` : "idle"}.`;

  const sL = B.site.x;
  const siteMidY = B.site.y + B.site.h / 2;
  const siteMidX = B.site.x + B.site.w / 2;
  const siteBottom = B.site.y + B.site.h;
  const gridIn = `M ${B.grid.x} ${siteMidY} L ${sL + B.site.w} ${siteMidY}`;
  const gridOut = `M ${sL + B.site.w} ${siteMidY} L ${B.grid.x} ${siteMidY}`;
  const battDown = `M ${siteMidX} ${siteBottom} L ${siteMidX} ${BATT.y - 2}`;
  const battUp = `M ${siteMidX} ${BATT.y - 2} L ${siteMidX} ${siteBottom}`;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={summary}>
      <Line
        id="m-solar"
        d={`M ${B.solar.x + B.solar.w} ${B.solar.y + 30} H 190 V ${siteMidY - 14} H ${sL}`}
        mw={solar}
        color="var(--solar)"
        label={fmtMw(solar)}
        lx={198}
        ly={B.solar.y + 24}
        anchor="start"
      />
      <Line
        id="m-wind"
        d={`M ${B.wind.x + B.wind.w} ${B.wind.y + 30} H 190 V ${siteMidY + 14} H ${sL}`}
        mw={wind}
        color="var(--wind)"
        label={fmtMw(wind)}
        lx={198}
        ly={B.wind.y + 52}
        anchor="start"
      />
      <Line
        id="m-grid"
        d={buying ? gridIn : gridOut}
        mw={Math.abs(grid)}
        color="var(--grid-c)"
        label={buying || selling ? fmtMw(Math.abs(grid)) : ""}
        lx={(sL + B.site.w + B.grid.x) / 2}
        ly={siteMidY - 12}
      />
      <Line
        id="m-batt"
        d={kind === "discharge" ? battUp : battDown}
        mw={Math.abs(sp)}
        color={kind === "discharge" ? "var(--discharge)" : "var(--charge)"}
        label={kind === "hold" ? "" : fmtMw(Math.abs(sp))}
        lx={siteMidX + 16}
        ly={(siteBottom + BATT.y) / 2 + 5}
        anchor="start"
      />

      <Frame b={B.solar} title="Solar" value={fmtMw(iv.solar_mw)} note={curtail > EPS ? `${fmtMw(curtail)} switched off` : undefined} />
      <Frame b={B.wind} title="Wind" value={fmtMw(iv.wind_mw)} />
      <Frame b={B.site} title="Site load" value={fmtMw(iv.demand_mw)} note="measured demand" />
      <Frame b={B.grid} title="Grid" value={fmtPrice(iv.price_eur_mwh)} note={buying ? "site is buying" : selling ? "site is selling" : "no exchange"} />
      <g className="enter-fade" style={{ ["--i" as string]: 4 }}>
        <Battery soc={iv.soc_mwh} capacity={capacityMwh} kind={kind} demandMw={iv.demand_mw} />
      </g>
    </svg>
  );
}
