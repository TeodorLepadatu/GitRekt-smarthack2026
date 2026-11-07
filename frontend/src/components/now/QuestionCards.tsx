import { useMemo, type ReactNode } from "react";
import { ArrowRight, CheckCircle2, Clock, ShieldAlert, TrendingUp, TriangleAlert, XCircle } from "lucide-react";
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, YAxis } from "recharts";
import { useDay, useGoals, useMetrics } from "@/api/hooks";
import { isDecided, type Goal, type GoalStatus } from "@/api/types";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { Card, Eyebrow } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { fallbackReasonText, fmtEur, fmtMwh, fmtPct, goalProgressText, goalStatusLabel, rulesText } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type { TabId } from "@/components/layout/tabs";

function QuestionCard({
  question,
  children,
  action,
  onAction,
}: {
  question: string;
  children: ReactNode;
  action: string;
  onAction: () => void;
}) {
  return (
    <Card className="flex flex-col gap-3 p-5 sm:p-6">
      <Eyebrow>{question}</Eyebrow>
      <div className="flex flex-1 flex-col gap-3">{children}</div>
      <button
        type="button"
        onClick={onAction}
        className="group flex w-fit items-center gap-1 text-sm font-semibold text-foreground/80 hover:text-foreground"
      >
        {action}
        <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5" />
      </button>
    </Card>
  );
}

const statusColor: Record<GoalStatus, string> = {
  on_track: "text-ok",
  behind: "text-warn",
  met: "text-info",
  unreachable: "text-bad",
  missed: "text-bad",
  violated: "text-bad",
};
const statusBar: Record<GoalStatus, string> = {
  on_track: "bg-ok",
  behind: "bg-warn",
  met: "bg-info",
  unreachable: "bg-bad",
  missed: "bg-bad",
  violated: "bg-bad",
};
const StatusIcon = {
  on_track: TrendingUp,
  behind: Clock,
  met: CheckCircle2,
  unreachable: XCircle,
  missed: XCircle,
  violated: ShieldAlert,
};

function goalsHeadline(goals: Goal[]): { text: string; className: string } {
  const failed = goals.filter((g) => g.status === "missed" || g.status === "violated").length;
  const unreachable = goals.filter((g) => g.status === "unreachable").length;
  const behind = goals.filter((g) => g.status === "behind").length;
  if (goals.length === 0) return { text: "No goals set", className: "text-muted-foreground" };
  if (failed) return { text: `${failed} goal${failed > 1 ? "s" : ""} not kept`, className: "text-bad" };
  if (unreachable) return { text: `${unreachable} goal${unreachable > 1 ? "s" : ""} can't be met`, className: "text-bad" };
  if (behind) return { text: `${behind} goal${behind > 1 ? "s" : ""} behind`, className: "text-warn" };
  return { text: "Yes, all goals on track", className: "text-ok" };
}

export function OnTrackCard({ go }: { go: (t: TabId) => void }) {
  const goals = useGoals();
  return (
    <QuestionCard question="Am I on track?" action="Manage goals" onAction={() => go("goals")}>
      {goals.isPending ? (
        <PanelLoading rows={3} />
      ) : goals.isError && !goals.data ? (
        <PanelError what="your goals" onRetry={() => goals.refetch()} />
      ) : (
        <>
          <span className={cn("num text-2xl font-bold", goalsHeadline(goals.data).className)}>
            {goalsHeadline(goals.data).text}
          </span>
          <ul className="flex flex-col gap-3">
            {goals.data.map((g) => {
              const I = StatusIcon[g.status];
              const progress = goalProgressText(g);
              const pct = g.type === "reserve" && g.mwh ? ((g.stored_mwh ?? 0) / g.mwh) * 100 : null;
              return (
                <li key={g.id} className="flex flex-col gap-1.5">
                  <div className="flex items-start gap-2">
                    <I className={cn("mt-0.5 size-4 shrink-0", statusColor[g.status])} />
                    <span className="flex-1 text-sm leading-snug font-medium">{g.source_text}</span>
                    <span className={cn("text-xs font-semibold whitespace-nowrap", statusColor[g.status])}>
                      {goalStatusLabel[g.status]}
                    </span>
                  </div>
                  {pct != null && progress && (
                    <div className="flex items-center gap-2 pl-6">
                      <Progress value={pct} className="h-1.5" indicatorClassName={statusBar[g.status]} label={progress} />
                      <span className="tabular text-xs whitespace-nowrap text-muted-foreground">{progress}</span>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </QuestionCard>
  );
}

export function MoneyCard({ go }: { go: (t: TabId) => void }) {
  const metrics = useMetrics();
  const day = useDay();
  const series = useMemo(() => {
    let sum = 0;
    return (day.data?.intervals ?? []).filter(isDecided).map((iv, i) => {
      sum += iv.settlement.profit_eur;
      return { i, sum };
    });
  }, [day.data]);

  return (
    <QuestionCard question="Am I making money?" action="See today's chart" onAction={() => go("today")}>
      {metrics.isPending ? (
        <PanelLoading rows={3} />
      ) : metrics.isError && !metrics.data ? (
        <PanelError what="today's profit" onRetry={() => metrics.refetch()} />
      ) : (
        <>
          <div className="flex flex-col">
            <span className={cn("num text-4xl font-bold", metrics.data.profit_today_eur >= 0 ? "text-ok" : "text-bad")}>
              {metrics.data.profit_today_eur >= 0 ? "+" : ""}
              {fmtEur(metrics.data.profit_today_eur)}
            </span>
            <span className="text-sm text-muted-foreground">
              {metrics.data.profit_today_eur >= 0 ? "Yes — earned so far today" : "Not yet — spent so far today"}
            </span>
          </div>
          {series.length > 1 && (
            <div className="h-16" aria-hidden>
              <ResponsiveContainer>
                <AreaChart data={series} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
                  <YAxis hide domain={["dataMin", "dataMax"]} />
                  <ReferenceLine y={0} stroke="var(--border)" />
                  <Area
                    dataKey="sum"
                    type="monotone"
                    stroke={metrics.data.profit_today_eur >= 0 ? "var(--ok)" : "var(--bad)"}
                    fill={metrics.data.profit_today_eur >= 0 ? "var(--ok)" : "var(--bad)"}
                    fillOpacity={0.15}
                    strokeWidth={2}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
          <div className="grid grid-cols-2 gap-2 text-sm">
            <span className="rounded-lg bg-muted/70 px-3 py-2">
              <span className="block text-xs text-muted-foreground">Sold</span>
              <span className="num font-semibold">{fmtMwh(metrics.data.energy_sold_mwh)}</span>
            </span>
            <span className="rounded-lg bg-muted/70 px-3 py-2">
              <span className="block text-xs text-muted-foreground">Bought</span>
              <span className="num font-semibold">{fmtMwh(metrics.data.energy_bought_mwh)}</span>
            </span>
          </div>
        </>
      )}
    </QuestionCard>
  );
}

export function TrustCard({ go }: { go: (t: TabId) => void }) {
  const metrics = useMetrics();
  const day = useDay();
  const goals = useGoals();

  /** Latest moments where the AI was not in full control. */
  const events = useMemo(() => {
    const out: { time: string; text: string; kind: "backup" | "safety" }[] = [];
    const decided = (day.data?.intervals ?? []).filter(isDecided).reverse();
    for (const iv of decided) {
      const fb = fallbackReasonText(iv.decision);
      if (fb) out.push({ time: iv.time, text: fb, kind: "backup" });
      else if (iv.guard.clipped) {
        const rule = rulesText(iv.guard.applied_rules, goals.data ?? [])[0];
        out.push({ time: iv.time, text: rule ?? "Safety layer adjusted the AI's decision", kind: "safety" });
      }
      if (out.length >= 3) break;
    }
    return out;
  }, [day.data, goals.data]);

  return (
    <QuestionCard question="Can I trust it?" action="See every decision" onAction={() => go("history")}>
      {metrics.isPending ? (
        <PanelLoading rows={3} />
      ) : metrics.isError && !metrics.data ? (
        <PanelError what="the decision record" onRetry={() => metrics.refetch()} />
      ) : (
        (() => {
          const d = metrics.data;
          const total = Math.max(1, d.decisions_total);
          const ai = (d.decisions_llm / total) * 100;
          const backup = (d.decisions_fallback / total) * 100;
          return (
            <>
              <div className="flex flex-col">
                <span className="num text-4xl font-bold">
                  {fmtPct(ai)} <span className="text-lg font-semibold text-muted-foreground">by the AI</span>
                </span>
                <span className="text-sm text-muted-foreground">
                  {d.decisions_fallback} by backup rules · {d.decisions_clipped} adjusted by safety · {d.decisions_total} total
                </span>
              </div>
              <div className="flex h-2.5 w-full gap-0.5 overflow-hidden rounded-full" aria-hidden>
                <div className="bg-ai" style={{ width: `${ai}%` }} />
                <div className="bg-warn" style={{ width: `${backup}%` }} />
              </div>
              {events.length > 0 && (
                <ul className="flex flex-col gap-1.5 text-sm">
                  {events.map((e) => (
                    <li key={e.time} className="flex gap-2">
                      <span className="tabular w-11 shrink-0 font-semibold">{e.time}</span>
                      {e.kind === "backup" ? (
                        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" />
                      ) : (
                        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-info" />
                      )}
                      <span className="leading-snug text-muted-foreground">{e.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          );
        })()
      )}
    </QuestionCard>
  );
}
