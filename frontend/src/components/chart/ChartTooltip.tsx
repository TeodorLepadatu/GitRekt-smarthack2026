import type { ReactNode } from "react";
import type { TooltipProps } from "recharts";
import { actionWord } from "@/components/common/marks";
import { actionLabel, fmtMw, fmtMwh, fmtPrice } from "@/lib/labels";
import { cn, indexToTime } from "@/lib/utils";
import type { ChartRow } from "./chartData";

type Props = TooltipProps<number, string>;

function rowOf(props: Props): ChartRow | null {
  const p = props.payload?.[0]?.payload as ChartRow | undefined;
  return props.active && p ? p : null;
}

function Shell({ children, time }: { children: ReactNode; time: string }) {
  return (
    <div className="max-w-72 border border-ink bg-sheet px-3 py-2.5 text-sm text-ink">
      <div className="num mb-1.5 text-xs text-ink-3">{time}</div>
      {children}
    </div>
  );
}

const range = (x: number) => `${indexToTime(x)}–${x + 1 === 96 ? "24:00" : indexToTime(x + 1)}`;

/** Price chart tooltip: price, stored energy, what the battery did and why. */
export function IntervalTooltip(props: Props) {
  const r = rowOf(props);
  if (!r || r.x >= 96) return null;
  return (
    <Shell time={range(r.x)}>
      <dl className="num grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <dt className="text-ink-3">price</dt>
        <dd className={cn("text-right", r.price != null && r.price < 0 && "text-ok")}>{r.price != null ? fmtPrice(r.price) : "–"}</dd>
        <dt className="text-ink-3">stored</dt>
        <dd className="text-right">{r.soc != null ? fmtMwh(r.soc) : "not yet"}</dd>
      </dl>
      {r.setpoint != null ? (
        <>
          <div className={cn("mt-2 font-semibold", actionWord(r.setpoint))}>{actionLabel(r.setpoint)}</div>
          {r.explanation && <p className="mt-0.5 leading-snug text-ink-2">{r.explanation}</p>}
        </>
      ) : (
        <p className="mt-2 text-ink-3">Forecast only, not decided yet.</p>
      )}
    </Shell>
  );
}

export function PowerTooltip(props: Props) {
  const r = rowOf(props);
  if (!r || r.x >= 96) return null;
  const items: [string, number | null][] = [
    ["solar", r.solar],
    ["wind", r.wind],
    ["site load", r.demand],
  ];
  return (
    <Shell time={range(r.x)}>
      <dl className="num grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        {items.map(([label, v]) => (
          <div key={label} className="contents">
            <dt className="text-ink-3">{label}</dt>
            <dd className="text-right">{v != null ? fmtMw(v) : "–"}</dd>
          </div>
        ))}
      </dl>
    </Shell>
  );
}
