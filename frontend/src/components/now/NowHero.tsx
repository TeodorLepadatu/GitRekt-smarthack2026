import { CalendarClock, Clock, ShieldAlert, TriangleAlert } from "lucide-react";
import { useDay, useGoals, useStateSnapshot } from "@/api/hooks";
import { actionBg, actionColor, ActionIcon } from "@/components/common/ActionIcon";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { SourceBadge } from "@/components/common/SourceBadge";
import { Card, Eyebrow } from "@/components/ui/card";
import { actionKind, actionLabel, fallbackReasonText, rulesText, upNextText } from "@/lib/labels";
import { cn, intervalEnd } from "@/lib/utils";
import { EnergyFlow } from "./EnergyFlow";

/** "What is the battery doing right now, and why?" */
export function NowHero() {
  const state = useStateSnapshot();
  const day = useDay();
  const goals = useGoals();

  if (state.isPending) {
    return (
      <Card className="p-6">
        <PanelLoading rows={5} />
      </Card>
    );
  }
  if (state.isError && !state.data) {
    return (
      <Card className="p-6">
        <PanelError what="the live status" onRetry={() => state.refetch()} />
      </Card>
    );
  }

  const cur = state.data.current;
  if (!cur) {
    return (
      <Card className="p-8 text-center text-lg text-muted-foreground">
        Waiting for the first decision of the day. The system decides every 15 minutes.
      </Card>
    );
  }

  const sp = cur.guard.battery_setpoint_mw;
  const kind = actionKind(sp);
  const notes = rulesText(cur.guard.applied_rules, goals.data ?? []);
  const fb = fallbackReasonText(cur.decision);
  const upNext = day.data ? upNextText(day.data.intervals, day.data.now) : null;

  return (
    <Card className="relative overflow-hidden">
      {/* soft glow behind the diagram */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-32 -left-24 size-[520px] rounded-full blur-3xl"
        style={{ background: "radial-gradient(circle, var(--hero-glow), transparent 70%)" }}
      />
      <div className="relative grid items-center gap-2 p-4 sm:p-6 lg:grid-cols-[minmax(0,1.5fr)_minmax(300px,1fr)] lg:gap-8">
        <EnergyFlow iv={cur} capacityMwh={state.data.capacity_mwh} />

        <div className="flex flex-col gap-4 lg:pr-2">
          <div className="flex flex-wrap items-center gap-2">
            <Eyebrow>Right now</Eyebrow>
            <span className="flex items-center gap-1 text-sm text-muted-foreground">
              <Clock className="size-3.5" />
              {cur.time}–{intervalEnd(cur.time)}
            </span>
          </div>

          <div className={cn("flex w-fit items-center gap-3 rounded-2xl px-4 py-2.5", actionBg[kind])}>
            <ActionIcon kind={kind} className="size-8" />
            <span className={cn("num text-2xl font-bold whitespace-nowrap sm:text-3xl xl:text-4xl", actionColor[kind])}>{actionLabel(sp)}</span>
          </div>

          <p className="font-display text-xl leading-snug font-medium sm:text-2xl">{cur.decision.explanation_for_operator}</p>

          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted-foreground">Decided by</span>
            <SourceBadge interval={cur} className="px-3 py-1 text-sm" />
          </div>

          {(fb || notes.length > 0) && (
            <ul className="flex flex-col gap-1.5 rounded-xl bg-muted/70 p-3 text-sm">
              {fb && (
                <li className="flex gap-2">
                  <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" />
                  {fb}
                </li>
              )}
              {notes.map((n) => (
                <li key={n} className="flex gap-2">
                  <ShieldAlert className="mt-0.5 size-4 shrink-0 text-info" />
                  {n}
                </li>
              ))}
            </ul>
          )}

          {upNext && (
            <div className="flex gap-2 border-t pt-4 text-sm text-muted-foreground">
              <CalendarClock className="mt-0.5 size-4 shrink-0" />
              <span>
                <b className="font-semibold text-foreground">Coming up: </b>
                {upNext}
              </span>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
