import type { ReactNode } from "react";
import type { TooltipProps } from "recharts";
import { actionColor, ActionIcon } from "@/components/common/ActionIcon";
import { actionKind, actionLabel, fmtMw, fmtMwh, fmtPrice } from "@/lib/labels";
import { cn, indexToTime } from "@/lib/utils";
import type { ChartRow } from "./chartData";

type Props = TooltipProps<number, string>;

function rowOf(props: Props): ChartRow | null {
  const p = props.payload?.[0]?.payload as ChartRow | undefined;
  return props.active && p ? p : null;
}

function Shell({ children, time }: { children: ReactNode; time: string }) {
  return (
    <div className="max-w-72 rounded-lg border bg-card p-3 text-sm text-card-foreground shadow-lg">
      <div className="mb-1.5 font-semibold">{time}</div>
      {children}
    </div>
  );
}

const range = (x: number) => `${indexToTime(x)}–${x + 1 === 96 ? "24:00" : indexToTime(x + 1)}`;

/** Full tooltip for the price chart: price, battery level, action, sentence. */
export function IntervalTooltip(props: Props) {
  const r = rowOf(props);
  if (!r || r.x >= 96) return null;
  const kind = r.setpoint != null ? actionKind(r.setpoint) : null;
  return (
    <Shell time={range(r.x)}>
      <dl className="tabular grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <dt className="text-muted-foreground">Price</dt>
        <dd className={cn("font-medium", r.price != null && r.price < 0 && "text-ok")}>
          {r.price != null ? fmtPrice(r.price) : "–"}
        </dd>
        <dt className="text-muted-foreground">Battery</dt>
        <dd className="font-medium">{r.soc != null ? fmtMwh(r.soc) : "not yet"}</dd>
      </dl>
      {kind && r.setpoint != null ? (
        <>
          <div className={cn("mt-2 flex items-center gap-1.5 font-semibold", actionColor[kind])}>
            <ActionIcon kind={kind} className="size-4" />
            {actionLabel(r.setpoint)}
          </div>
          {r.explanation && <p className="mt-1 leading-snug text-muted-foreground">{r.explanation}</p>}
        </>
      ) : (
        <p className="mt-2 text-muted-foreground">Forecast only — not decided yet.</p>
      )}
    </Shell>
  );
}

export function PowerTooltip(props: Props) {
  const r = rowOf(props);
  if (!r || r.x >= 96) return null;
  const items: [string, number | null, string][] = [
    ["Solar", r.solar, "bg-solar"],
    ["Wind", r.wind, "bg-wind"],
    ["Demand", r.demand, "bg-demand"],
  ];
  return (
    <Shell time={range(r.x)}>
      <dl className="tabular grid grid-cols-[auto_auto_1fr] items-center gap-x-2 gap-y-0.5">
        {items.map(([label, v, color]) => (
          <div key={label} className="contents">
            <span className={cn("size-2.5 rounded-sm", color)} />
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right font-medium">{v != null ? fmtMw(v) : "–"}</dd>
          </div>
        ))}
      </dl>
    </Shell>
  );
}
