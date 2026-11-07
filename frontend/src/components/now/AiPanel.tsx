import type { Goal, Interval } from "@/api/types";
import { USE_MOCKS } from "@/api/client";
import { actionKind, confidenceLabel, fallbackReasonText, fmtMw, fmtMwh, fmtPrice } from "@/lib/labels";
import { cn } from "@/lib/utils";

interface Props {
  iv: Interval;
  /** Energy stored when the interval started (end of the previous one). */
  socBefore: number | null;
  goals: Goal[];
  maxPowerMw: number;
}

function Row({ k, v, tone }: { k: string; v: string; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-t border-rule py-2 first:border-t-0">
      <dt className="text-ink-2">{k}</dt>
      <dd className={cn("num text-right", tone)}>{v}</dd>
    </div>
  );
}

/**
 * The AI's turn in this interval: what it was given, what it answered,
 * how sure it was. Shown in words and figures, never as raw JSON.
 */
export function AiPanel({ iv, socBefore, goals, maxPowerMw }: Props) {
  const llm = iv.decision.source === "llm";
  const proposed = iv.decision.battery_fraction * maxPowerMw;
  const kind = actionKind(proposed);
  const activeGoals = goals.filter((g) => g.type !== "max_profit");
  const objective = goals.some((g) => g.type === "max_profit") ? "make the most money" : "make money";

  return (
    <section className="grid gap-x-12 gap-y-8 border-t border-rule-strong pt-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)] 2xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.4fr)_minmax(0,0.8fr)]">
      <div>
        <h3 className="label mb-3">What the AI was given</h3>
        <dl className="text-base">
          <Row k="Price now" v={fmtPrice(iv.price_eur_mwh)} />
          <Row k="Break-even price" v={fmtPrice(iv.break_even)} />
          <Row k="Solar + wind forecast" v={fmtMw(iv.production_mw)} />
          <Row k="Site demand forecast" v={fmtMw(iv.demand_mw)} />
          {socBefore != null && <Row k="Stored at start" v={fmtMwh(socBefore)} />}
        </dl>
        <p className="mt-3 text-sm text-ink-3">
          Plus your {activeGoals.length} {activeGoals.length === 1 ? "goal" : "goals"} and the instruction to {objective}.
        </p>
      </div>

      <div>
        <h3 className="label mb-3">{llm ? "What it answered" : "What happened"}</h3>
        {llm ? (
          <>
            <blockquote className="font-serif text-2xl leading-snug">
              “{iv.decision.reasoning || iv.decision.explanation_for_operator}”
            </blockquote>
            <p className="mt-4 text-base text-ink-2">
              It proposed to{" "}
              <span className={cn("font-semibold", kind === "charge" ? "text-charge" : kind === "discharge" ? "text-discharge" : "text-ink")}>
                {kind === "hold" ? "hold" : `${kind} ${fmtMw(Math.abs(proposed))}`}
              </span>
              {iv.decision.curtail ? " and switch off surplus output" : ""}, with {confidenceLabel[iv.decision.confidence].toLowerCase()}.
            </p>
          </>
        ) : (
          <>
            <p className="font-serif text-2xl leading-snug text-warn">{fallbackReasonText(iv.decision)}.</p>
            <p className="mt-4 text-base text-ink-2">
              Fixed rules took over for this quarter-hour: they charge when the price is below zero, sell above break-even, and otherwise match
              the site's surplus, while keeping hard goals on pace.
            </p>
          </>
        )}
      </div>

      <div className="lg:col-span-2 lg:max-w-[70ch] 2xl:col-span-1">
        <h3 className="label mb-3">About the AI</h3>
        <p className="text-base text-ink-2">
          A small language model running on the site's own computer. Every 15 minutes it gets the figures on the left and answers with a battery
          setting, a short reason and how sure it is.
        </p>
        <p className="mt-3 text-base text-ink-2">
          It never controls the battery directly: its answer is checked, and the safety layer has the last word.
        </p>
        {USE_MOCKS && (
          <p className="mt-3 text-sm text-ink-3">In this simulated day the answers are produced by the demo data, not by the model.</p>
        )}
      </div>
    </section>
  );
}
