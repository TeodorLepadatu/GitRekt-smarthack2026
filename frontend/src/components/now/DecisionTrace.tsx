import type { CSSProperties } from "react";
import type { Goal, Interval } from "@/api/types";
import { actionKind, confidenceLabel, fallbackReasonText, fmtMw, fmtPrice, rulesText } from "@/lib/labels";
import { Reveal } from "@/components/common/Reveal";
import { cn } from "@/lib/utils";

/** "charge 3.00 MW" / "discharge 1.20 MW" / "hold" */
function phrase(mw: number) {
  const k = actionKind(mw);
  if (k === "charge") return `charge ${fmtMw(mw)}`;
  if (k === "discharge") return `discharge ${fmtMw(-mw)}`;
  return "hold";
}

interface Step {
  name: string;
  result: string;
  detail?: string;
  /** This step changed or replaced what came before. */
  intervened?: boolean;
}

function buildSteps(iv: Interval, goals: Goal[], maxPowerMw: number): Step[] {
  const net = iv.production_mw - iv.demand_mw;
  const proposed = iv.decision.battery_fraction * maxPowerMw;
  const llm = iv.decision.source === "llm";
  const notes = rulesText(iv.guard.applied_rules, goals);
  const sp = iv.guard.battery_setpoint_mw;

  return [
    {
      name: "Forecast",
      result: fmtPrice(iv.price_eur_mwh),
      detail: `break-even €${iv.break_even.toFixed(0)} · ${net >= 0 ? `${fmtMw(net)} spare` : `${fmtMw(-net)} short`}`,
    },
    llm
      ? { name: "AI proposal", result: phrase(proposed), detail: confidenceLabel[iv.decision.confidence].toLowerCase() }
      : { name: "AI proposal", result: "no usable answer", detail: iv.decision.fallback_reason?.replace(/_/g, " ") ?? undefined, intervened: true },
    llm
      ? { name: "Check", result: "answer accepted", detail: "valid and within limits" }
      : { name: "Backup rules", result: phrase(proposed), detail: fallbackReasonText(iv.decision)?.split(",")[0] ?? undefined, intervened: true },
    iv.guard.clipped
      ? {
          name: "Safety layer",
          result: Math.abs(sp - proposed) < 0.01 ? "kept power, adjusted output" : `changed to ${phrase(sp)}`,
          detail: notes[0] ?? "kept the battery within its limits",
          intervened: true,
        }
      : { name: "Safety layer", result: "no change", detail: notes[0] ?? "limits and goals respected" },
    {
      name: "Battery",
      result: phrase(sp),
      detail: iv.guard.curtail_mw > 0 ? `${fmtMw(iv.guard.curtail_mw)} of output switched off` : `until ${endOf(iv.time)}`,
    },
  ];
}

function endOf(t: string) {
  const [h, m] = t.split(":").map(Number);
  const e = h * 60 + m + 15;
  return e >= 1440 ? "24:00" : `${String(Math.floor(e / 60)).padStart(2, "0")}:${String(e % 60).padStart(2, "0")}`;
}

/** Forecast -> AI -> check -> safety -> battery, with what each step did this interval. */
export function DecisionTrace({ iv, goals, maxPowerMw }: { iv: Interval; goals: Goal[]; maxPowerMw: number }) {
  const steps = buildSteps(iv, goals, maxPowerMw);
  return (
    // A staircase: each step sits a little lower than the one before, so the
    // eye reads forecast -> battery as a diagonal.
    <ol className="grid grid-cols-1 lg:grid-cols-5">
      {steps.map((s, i) => (
        <Reveal
          as="li"
          key={s.name}
          delay={i * 0.1}
          style={{ ["--i" as string]: i } as CSSProperties}
          className={cn(
            "relative flex flex-col gap-1 border-t-2 py-4 lg:px-5 lg:py-5 lg:first:pl-0 lg:[margin-top:calc(var(--i)*1.5rem)]",
            s.intervened ? "border-warn" : "border-ink",
          )}
        >
          <span className="num text-sm text-ink-3">
            {String(i + 1).padStart(2, "0")} · {s.name}
          </span>
          <span className={cn("num text-lg font-medium xl:text-xl", s.intervened && "text-warn")}>{s.result}</span>
          {s.detail && <span className="text-sm leading-snug text-ink-3">{s.detail}</span>}
        </Reveal>
      ))}
    </ol>
  );
}
