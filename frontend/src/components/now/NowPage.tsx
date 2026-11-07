import { useMemo, type CSSProperties } from "react";
import { useDay, useGoals, useStateSnapshot } from "@/api/hooks";
import { isDecided } from "@/api/types";
import { actionWord } from "@/components/common/marks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { Reveal } from "@/components/common/Reveal";
import { actionKind, fallbackReasonText, fmtMw, rulesText, upNextText } from "@/lib/labels";
import { intervalEnd } from "@/lib/utils";
import type { TabId } from "@/components/layout/tabs";
import { AiPanel } from "./AiPanel";
import { DayLedger } from "./DayLedger";
import { DecisionTrace } from "./DecisionTrace";
import { EnergyFlow } from "./EnergyFlow";

function headline(sp: number) {
  const k = actionKind(sp);
  if (k === "charge") return { verb: "Charging", rest: `at ${fmtMw(sp)}.` };
  if (k === "discharge") return { verb: "Discharging", rest: `at ${fmtMw(-sp)}.` };
  return { verb: "Holding.", rest: "The battery is idle." };
}

/** Order for the CSS page-load sequence (.enter uses --i as its delay step). */
const step = (i: number) => ({ ["--i" as string]: i }) as CSSProperties;

export function NowPage({ go }: { go: (t: TabId) => void }) {
  const state = useStateSnapshot();
  const day = useDay();
  const goals = useGoals();

  const socBefore = useMemo(() => {
    const cur = state.data?.current;
    if (!cur || !day.data) return null;
    const decided = day.data.intervals.filter(isDecided);
    const i = decided.findIndex((s) => s.time === cur.time);
    return i > 0 ? decided[i - 1].soc_mwh : null;
  }, [state.data, day.data]);

  if (state.isPending) return <PanelLoading rows={6} />;
  if (state.isError && !state.data) return <PanelError what="the battery status" onRetry={() => state.refetch()} />;

  const cur = state.data.current;
  if (!cur) {
    return <p className="font-serif text-3xl">The first decision of the day has not been made yet. One is made every 15 minutes from 00:00.</p>;
  }

  const sp = cur.guard.battery_setpoint_mw;
  const h = headline(sp);
  const notes = rulesText(cur.guard.applied_rules, goals.data ?? []);
  const fb = fallbackReasonText(cur.decision);
  const upNext = day.data ? upNextText(day.data.intervals, day.data.now) : null;

  return (
    <div className="flex flex-col">
      {/* ---- hero: statement on the left, diagram sheet lower right, overlapping what follows ---- */}
      <div className="relative grid gap-x-10 gap-y-10 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
        <span aria-hidden className="ghost-text -top-10 -left-6 text-[clamp(7rem,19vw,17rem)]">
          {cur.time}
        </span>

        <div className="relative flex flex-col gap-6 pt-6 lg:pt-14">
          <p className="num enter text-base text-ink-3" style={step(0)}>
            {cur.time}–{intervalEnd(cur.time)}
          </p>
          <h2 className="font-serif text-6xl leading-[0.98] font-semibold tracking-tight xl:text-8xl">
            <span className={`enter inline-block ${actionWord(sp)}`} style={step(1)}>
              {h.verb}
            </span>
            <br />
            <span className="enter inline-block" style={step(2)}>
              {h.rest}
            </span>
          </h2>
          <p className="enter max-w-[38ch] font-serif text-2xl leading-snug text-ink-2 sm:text-[1.75rem]" style={step(3)}>
            {cur.decision.explanation_for_operator}
          </p>
          {(fb || notes.length > 0) && (
            <p className="enter max-w-[56ch] text-lg text-ink-2" style={step(4)}>
              {fb && <>{fb}. </>}
              {notes.length > 0 && <>The safety layer stepped in: {notes.map((n) => n.charAt(0).toLowerCase() + n.slice(1)).join("; ")}.</>}
            </p>
          )}
          {upNext && (
            <p className="enter max-w-[56ch] text-lg text-ink-3" style={step(5)}>
              <span className="font-semibold text-ink-2">Next:</span> {upNext}
            </p>
          )}
        </div>

        <figure
          className="enter-fade relative z-10 flex flex-col gap-3 self-start border border-rule-strong bg-sheet p-5 sm:p-7 lg:mt-28 lg:-mb-44 lg:-ml-6"
          style={step(2)}
        >
          <EnergyFlow iv={cur} capacityMwh={state.data.capacity_mwh} />
          <figcaption className="text-sm text-ink-3">
            Power flows for {cur.time}–{intervalEnd(cur.time)}. Line weight and speed follow the MW. Hover the battery.
          </figcaption>
        </figure>
      </div>

      {/* ---- how the decision was made: heading tucked under the overlapping sheet ---- */}
      <section className="mt-16 flex flex-col gap-6 lg:mt-10">
        <Reveal className="flex flex-col gap-3 border-t border-rule-strong pt-6 lg:max-w-[46%]">
          <h2 className="font-serif text-4xl font-semibold tracking-tight">How this decision was made</h2>
          <p className="text-lg text-ink-2">
            Five steps run every quarter-hour. Highlighted steps are the ones that overrode what came before.{" "}
            <button type="button" onClick={() => go("decisions")} className="underline underline-offset-4 hover:text-ink">
              Earlier decisions
            </button>
          </p>
        </Reveal>
        <div className="lg:mt-24">
          <DecisionTrace iv={cur} goals={goals.data ?? []} maxPowerMw={state.data.max_power_mw} />
        </div>
        <Reveal>
          <AiPanel iv={cur} socBefore={socBefore} goals={goals.data ?? []} maxPowerMw={state.data.max_power_mw} />
        </Reveal>
      </section>

      {/* ---- the day so far ---- */}
      <section className="mt-20 flex flex-col">
        <Reveal from="left">
          <h2 className="border-t border-rule-strong pt-6 font-serif text-4xl font-semibold tracking-tight">The day so far</h2>
        </Reveal>
        <DayLedger go={go} />
      </section>
    </div>
  );
}
