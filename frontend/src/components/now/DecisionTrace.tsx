import type { CSSProperties } from "react";
import type { Goal, Interval } from "@/api/types";
import { actionPhrase as phrase, confidenceLabel, fallbackReasonText, fmtMw, fmtPrice, rulesText } from "@/lib/labels";
import { Reveal } from "@/components/common/Reveal";
import { cn } from "@/lib/utils";

interface Step {
  name: string;
  result: string;
  detail?: string;
  /** This step changed or replaced what came before. */
  intervened?: boolean;
}

/** Steps 2 and 3: what the AI proposed, and what turned it into a command. */
function decisionSteps(iv: Interval, proposed: number): [Step, Step] {
  const { action, proposed_mw } = iv.control;
  const notAsked: Step = { name: "AI proposal", result: "not asked", detail: "the operator is in control", intervened: true };
  const asked: Step = proposed_mw != null ? { name: "AI proposal", result: phrase(proposed_mw), detail: "waited for the operator" } : notAsked;
  switch (action) {
    case "approved":
      return [asked, { name: "Operator", result: "approved", detail: "clicked to let it run" }];
    case "rejected":
      return [asked, { name: "Operator", result: "rejected: hold", detail: "the battery holds instead", intervened: true }];
    case "timeout":
      return [asked, { name: "Operator", result: "no answer: hold", detail: "the wait ran out", intervened: true }];
    case "override":
      return [asked, { name: "Operator", result: phrase(proposed), detail: "manual override", intervened: true }];
    case "hold":
      return [asked, { name: "Operator", result: "emergency hold", detail: "idle, nothing exported", intervened: true }];
  }
  return iv.decision.source === "llm"
    ? [
        { name: "AI proposal", result: phrase(proposed), detail: confidenceLabel[iv.decision.confidence].toLowerCase() },
        { name: "Check", result: "answer accepted", detail: "valid and within limits" },
      ]
    : [
        { name: "AI proposal", result: "no usable answer", detail: iv.decision.fallback_reason?.replace(/_/g, " ") ?? undefined, intervened: true },
        { name: "Backup rules", result: phrase(proposed), detail: fallbackReasonText(iv.decision)?.split(",")[0] ?? undefined, intervened: true },
      ];
}

function buildSteps(iv: Interval, goals: Goal[], maxPowerMw: number): Step[] {
  const net = iv.production_mw - iv.demand_mw;
  const proposed = iv.decision.battery_fraction * maxPowerMw;
  const notes = rulesText(iv.guard.applied_rules, goals);
  const sp = iv.guard.battery_setpoint_mw;

  return [
    {
      name: "Forecast",
      result: fmtPrice(iv.price_eur_mwh),
      detail: `break-even €${iv.break_even.toFixed(0)} · ${net >= 0 ? `${fmtMw(net)} spare` : `${fmtMw(-net)} short`}`,
    },
    ...decisionSteps(iv, proposed),
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

/** Forecast -> AI -> check (or the operator) -> safety -> battery, with what each step did this interval. */
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
