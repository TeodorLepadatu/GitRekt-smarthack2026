import { Factory, Sun, UtilityPole, Wind, type LucideIcon } from "lucide-react";
import type { Interval } from "@/api/types";
import { actionKind, fmtMw } from "@/lib/labels";

/**
 * Live picture of where the power goes in this interval:
 * Solar + Wind -> Site, Site <-> Battery, Site <-> Grid.
 * Line thickness and dash speed follow the MW on each line.
 */

const W = 640;
const H = 476;
const EPS = 0.01;

type Pt = { x: number; y: number };
const P = {
  solar: { x: 86, y: 92 },
  wind: { x: 86, y: 316 },
  site: { x: 318, y: 206 },
  grid: { x: 556, y: 206 },
  battery: { x: 318, y: 406 },
};
const R = 36;

interface FlowProps {
  from: Pt;
  to: Pt;
  mw: number;
  color: string;
  curve?: boolean;
  /** Where to put the MW label, defaults to the middle. */
  labelAt?: Pt;
}

function path(a: Pt, b: Pt, curve: boolean) {
  if (!curve) return `M ${a.x} ${a.y} L ${b.x} ${b.y}`;
  const mx = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`;
}

function Flow({ from, to, mw, color, curve = false, labelAt }: FlowProps) {
  const active = mw > EPS;
  const d = path(from, to, curve);
  const width = active ? 3 + Math.min(mw, 8) * 1.3 : 3;
  const dur = `${Math.max(0.45, 2.2 - mw * 0.35)}s`;
  const mid = labelAt ?? { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
  const label = active ? fmtMw(mw) : "0 MW";
  const labelW = label.length * 8.4 + 18;
  return (
    <g>
      <path d={d} fill="none" stroke="var(--border)" strokeWidth={width + 6} strokeLinecap="round" />
      {active && (
        <>
          <path d={d} fill="none" stroke={color} strokeOpacity={0.28} strokeWidth={width} strokeLinecap="round" />
          <path
            d={d}
            fill="none"
            stroke={color}
            strokeWidth={Math.max(3, width * 0.55)}
            className="flow-line"
            style={{ ["--flow-dur" as string]: dur }}
          />
        </>
      )}
      <g transform={`translate(${mid.x - labelW / 2} ${mid.y - 13})`}>
        <rect width={labelW} height={26} rx={13} fill="var(--card)" stroke={active ? color : "var(--border)"} strokeWidth={1.5} />
        <text
          x={labelW / 2}
          y={17.5}
          textAnchor="middle"
          className="num"
          fontSize={14}
          fontWeight={600}
          fill={active ? "var(--foreground)" : "var(--muted-foreground)"}
        >
          {label}
        </text>
      </g>
    </g>
  );
}

type Icon = LucideIcon;

interface NodeProps {
  at: Pt;
  icon: Icon;
  color: string;
  title: string;
  sub?: string;
  pulse?: boolean;
  /** Put the caption above the node (keeps the line below it clear). */
  above?: boolean;
}

function Node({ at, icon: I, color, title, sub, pulse, above }: NodeProps) {
  const titleY = above ? at.y - R - (sub ? 30 : 12) : at.y + R + 23;
  return (
    <g>
      {pulse && <circle cx={at.x} cy={at.y} r={R} fill="none" stroke={color} strokeWidth={3} className="pulse-ring" />}
      <circle cx={at.x} cy={at.y} r={R} fill="var(--card)" stroke={color} strokeWidth={3} />
      <I x={at.x - 16} y={at.y - 16} width={32} height={32} stroke={color} strokeWidth={2} />
      <text x={at.x} y={titleY} textAnchor="middle" fontSize={17} fontWeight={650} fill="var(--foreground)" className="num">
        {title}
      </text>
      {sub && (
        <text x={at.x} y={titleY + 19} textAnchor="middle" fontSize={14} fill="var(--muted-foreground)">
          {sub}
        </text>
      )}
    </g>
  );
}

function BatteryNode({ at, soc, capacity, color, pulse }: { at: Pt; soc: number; capacity: number; color: string; pulse: boolean }) {
  const w = 132;
  const h = 52;
  const pct = Math.max(0, Math.min(1, soc / capacity));
  const x = at.x - w / 2;
  const y = at.y - h / 2;
  return (
    <g>
      {pulse && <rect x={x} y={y} width={w} height={h} rx={12} fill="none" stroke={color} strokeWidth={3} className="pulse-ring" />}
      <rect x={x} y={y} width={w} height={h} rx={12} fill="var(--card)" stroke="var(--foreground)" strokeOpacity={0.7} strokeWidth={3} />
      <rect x={x + w + 2} y={at.y - 10} width={7} height={20} rx={2.5} fill="var(--foreground)" fillOpacity={0.7} />
      <rect x={x + 6} y={y + 6} width={(w - 12) * pct} height={h - 12} rx={7} fill="var(--soc)" style={{ transition: "width 700ms" }} />
      <text x={at.x} y={at.y + 7} textAnchor="middle" fontSize={20} fontWeight={700} className="num" fill="var(--foreground)">
        {Math.round(pct * 100)}%
      </text>
      <text x={at.x} y={y + h + 26} textAnchor="middle" fontSize={17} fontWeight={650} className="num" fill="var(--foreground)">
        Battery · {soc.toFixed(2)} / {capacity.toFixed(0)} MWh
      </text>
    </g>
  );
}

export function EnergyFlow({ iv, capacityMwh }: { iv: Interval; capacityMwh: number }) {
  const curtail = iv.guard.curtail_mw;
  const used = Math.max(0, iv.production_mw - curtail);
  // split curtailment over solar and wind in proportion to their output
  const share = iv.production_mw > 0 ? used / iv.production_mw : 0;
  const solar = iv.solar_mw * share;
  const wind = iv.wind_mw * share;
  const sp = iv.guard.battery_setpoint_mw;
  const kind = actionKind(sp);
  const grid = iv.demand_mw + sp - used; // > 0 buying, < 0 selling
  const gridMw = Math.abs(grid);
  const buying = grid > EPS;
  const selling = grid < -EPS;

  const battColor = kind === "discharge" ? "var(--discharge)" : "var(--charge)";
  const gridSub = buying ? `Buying · €${iv.price_eur_mwh.toFixed(2)}/MWh` : selling ? `Selling · €${iv.price_eur_mwh.toFixed(2)}/MWh` : `Idle · €${iv.price_eur_mwh.toFixed(2)}/MWh`;

  const summary = `Solar ${fmtMw(solar)} and wind ${fmtMw(wind)} supply the site, which uses ${fmtMw(iv.demand_mw)}. Battery ${
    kind === "hold" ? "holding" : kind === "charge" ? `charging ${fmtMw(sp)}` : `discharging ${fmtMw(-sp)}`
  }. Grid: ${buying ? `buying ${fmtMw(gridMw)}` : selling ? `selling ${fmtMw(gridMw)}` : "no exchange"}.`;

  const edge = (a: Pt, b: Pt, dx: number, dy = 0) => ({ from: { x: a.x + dx, y: a.y + dy }, to: b });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={summary}>
      <Flow
        {...edge(P.solar, { x: P.site.x - R - 4, y: P.site.y - 14 }, R + 4)}
        mw={solar}
        color="var(--solar)"
        curve
      />
      <Flow
        {...edge(P.wind, { x: P.site.x - R - 4, y: P.site.y + 14 }, R + 4)}
        mw={wind}
        color="var(--wind)"
        curve
      />
      {/* site <-> grid: dashes run toward whoever receives the power */}
      <Flow
        from={buying ? { x: P.grid.x - R - 4, y: P.grid.y } : { x: P.site.x + R + 4, y: P.site.y }}
        to={buying ? { x: P.site.x + R + 4, y: P.site.y } : { x: P.grid.x - R - 4, y: P.grid.y }}
        mw={gridMw}
        color="var(--grid-c)"
      />
      {/* site <-> battery */}
      <Flow
        from={kind === "discharge" ? { x: P.battery.x, y: P.battery.y - 30 } : { x: P.site.x, y: P.site.y + R + 4 }}
        to={kind === "discharge" ? { x: P.site.x, y: P.site.y + R + 4 } : { x: P.battery.x, y: P.battery.y - 30 }}
        mw={Math.abs(sp)}
        color={battColor}
      />

      <Node at={P.solar} icon={Sun} color="var(--solar)" title="Solar" sub={curtail > EPS ? `${fmtMw(iv.solar_mw)} · ${fmtMw(curtail)} off` : fmtMw(iv.solar_mw)} />
      <Node at={P.wind} icon={Wind} color="var(--wind)" title="Wind" sub={fmtMw(iv.wind_mw)} />
      <Node at={P.site} icon={Factory} color="var(--foreground)" title="Your site" sub={`uses ${fmtMw(iv.demand_mw)}`} above />
      <Node at={P.grid} icon={UtilityPole} color="var(--grid-c)" title="Grid" sub={gridSub} pulse={buying || selling} />
      <BatteryNode at={P.battery} soc={iv.soc_mwh} capacity={capacityMwh} color={battColor} pulse={kind !== "hold"} />
    </svg>
  );
}
