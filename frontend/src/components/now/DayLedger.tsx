import { useMemo } from "react";
import { useDay, useGoals, useMetrics } from "@/api/hooks";
import { isDecided, type Goal } from "@/api/types";
import { GoalStatusWord, ProgressLine } from "@/components/common/marks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { fallbackReasonText, fmtEur, fmtMwh, goalProgressText, rulesText } from "@/lib/labels";
import { Reveal } from "@/components/common/Reveal";
import { cn } from "@/lib/utils";
import type { TabId } from "@/components/layout/tabs";

function MoreLink({ onClick, children }: { onClick: () => void; children: string }) {
  return (
    <button type="button" onClick={onClick} className="mt-auto w-fit pt-3 text-base font-medium underline underline-offset-2 hover:text-ink">
      {children}
    </button>
  );
}

function goalsSentence(goals: Goal[]) {
  const failed = goals.filter((g) => g.status === "missed" || g.status === "violated").length;
  const unreachable = goals.filter((g) => g.status === "unreachable").length;
  const behind = goals.filter((g) => g.status === "behind").length;
  if (goals.length === 0) return { text: "No goals set. The system only tries to make money.", tone: "text-ink-2" };
  if (failed) return { text: `${failed} of ${goals.length} goals were not kept.`, tone: "text-bad" };
  if (unreachable) return { text: `${unreachable} of ${goals.length} goals can no longer be met.`, tone: "text-bad" };
  if (behind) return { text: `${behind} of ${goals.length} goals are behind schedule.`, tone: "text-warn" };
  return { text: `All ${goals.length} goals are on schedule.`, tone: "text-ink" };
}

function GoalsColumn({ go }: { go: (t: TabId) => void }) {
  const goals = useGoals();
  if (goals.isPending) return <PanelLoading rows={4} />;
  if (goals.isError && !goals.data) return <PanelError what="your goals" onRetry={() => goals.refetch()} />;
  const s = goalsSentence(goals.data);
  return (
    <>
      <p className={cn("font-serif text-2xl leading-snug", s.tone)}>{s.text}</p>
      <ul className="mt-4 flex flex-col">
        {goals.data.map((g) => {
          const progress = goalProgressText(g);
          const pct = g.type === "reserve" && g.mwh ? ((g.stored_mwh ?? 0) / g.mwh) * 100 : null;
          return (
            <li key={g.id} className="flex flex-col gap-1.5 border-t border-rule py-2.5 first:border-t-0 first:pt-0">
              <div className="flex items-baseline justify-between gap-3 text-base">
                <span>{g.source_text}</span>
                <GoalStatusWord status={g.status} />
              </div>
              {pct != null && progress && (
                <>
                  <ProgressLine value={pct} status={g.status} label={progress} />
                  <span className="num text-sm text-ink-3">{progress}</span>
                </>
              )}
            </li>
          );
        })}
      </ul>
      <MoreLink onClick={() => go("goals")}>Change goals</MoreLink>
    </>
  );
}

function MoneyColumn({ go }: { go: (t: TabId) => void }) {
  const m = useMetrics();
  if (m.isPending) return <PanelLoading rows={4} />;
  if (m.isError && !m.data) return <PanelError what="today's money" onRetry={() => m.refetch()} />;
  const d = m.data;
  const up = d.profit_today_eur >= 0;
  const added = d.value_added_eur;
  return (
    <>
      <p className={cn("num text-4xl font-medium whitespace-nowrap 2xl:text-5xl", up ? "text-ok" : "text-bad")}>
        {up ? "+" : ""}
        {fmtEur(d.profit_today_eur)}
      </p>
      <p className="mt-1 text-base text-ink-2">
        {up ? "earned" : "spent"} on the grid since 00:00, after {fmtEur(Math.abs(d.wear_eur))} of battery wear.
      </p>
      <p className="mt-4 font-serif text-xl leading-snug">
        The battery made{" "}
        <span className={cn("num font-medium", added >= 0 ? "text-ok" : "text-bad")}>{fmtEur(Math.abs(added))}</span>{" "}
        {added >= 0 ? "more" : "less"} than the site would have without it.
      </p>
      <dl className="num mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-base">
        <dt className="text-ink-3">sold</dt>
        <dd className="text-right">{fmtMwh(d.energy_sold_mwh)}</dd>
        <dt className="text-ink-3">bought</dt>
        <dd className="text-right">{fmtMwh(d.energy_bought_mwh)}</dd>
        <dt className="text-ink-3">switched off</dt>
        <dd className="text-right">{fmtMwh(d.curtailed_mwh)}</dd>
      </dl>
      <MoreLink onClick={() => go("day")}>Prices and power over the day</MoreLink>
    </>
  );
}

function DecisionsColumn({ go }: { go: (t: TabId) => void }) {
  const m = useMetrics();
  const day = useDay();
  const goals = useGoals();

  const exceptions = useMemo(() => {
    const out: { time: string; text: string; who: "backup" | "safety" }[] = [];
    for (const iv of (day.data?.intervals ?? []).filter(isDecided).reverse()) {
      const fb = fallbackReasonText(iv.decision);
      if (fb) out.push({ time: iv.time, text: fb, who: "backup" });
      else if (iv.guard.clipped)
        out.push({ time: iv.time, text: rulesText(iv.guard.applied_rules, goals.data ?? [])[0] ?? "Safety layer adjusted the decision", who: "safety" });
      if (out.length >= 4) break;
    }
    return out;
  }, [day.data, goals.data]);

  if (m.isPending) return <PanelLoading rows={4} />;
  if (m.isError && !m.data) return <PanelError what="the decision record" onRetry={() => m.refetch()} />;
  const d = m.data;
  return (
    <>
      <p className="font-serif text-2xl leading-snug">
        The AI made <span className="num font-medium">{d.decisions_llm}</span> of today's{" "}
        <span className="num font-medium">{d.decisions_total}</span> decisions.
      </p>
      <p className="mt-1 text-base text-ink-2">
        Backup rules took over {d.decisions_fallback} {d.decisions_fallback === 1 ? "time" : "times"}; the safety layer changed{" "}
        {d.decisions_clipped} {d.decisions_clipped === 1 ? "decision" : "decisions"}.
      </p>
      {exceptions.length > 0 && (
        <table className="mt-4 w-full text-base">
          <caption className="label mb-1 text-left">Latest exceptions</caption>
          <tbody>
            {exceptions.map((e) => (
              <tr key={e.time} className="border-t border-rule align-baseline">
                <td className="num w-14 py-1.5 pr-2 text-ink-3">{e.time}</td>
                <td className={cn("py-1.5", e.who === "backup" ? "text-warn" : "text-info")}>{e.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <MoreLink onClick={() => go("decisions")}>All decisions</MoreLink>
    </>
  );
}

/** The day so far, as three ruled columns. */
export function DayLedger({ go }: { go: (t: TabId) => void }) {
  // Uneven columns; the money column is a raised sheet that overlaps the rule above it.
  return (
    <section className="grid items-start gap-y-2 md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_minmax(0,1.05fr)]">
      <Reveal from="left" className="flex flex-col py-6 md:pr-10">
        <span className="label mb-3">Goals</span>
        <GoalsColumn go={go} />
      </Reveal>
      <Reveal delay={0.1} className="relative z-10 flex flex-col border border-rule-strong bg-sheet px-6 py-6 md:-mt-14 md:px-8">
        <span className="label mb-3">Money today</span>
        <MoneyColumn go={go} />
      </Reveal>
      <Reveal from="right" delay={0.2} className="flex flex-col border-t border-rule py-6 md:mt-10 md:border-t-0 md:pl-10">
        <span className="label mb-3">Who decided</span>
        <DecisionsColumn go={go} />
      </Reveal>
    </section>
  );
}
