import { useMemo, useState } from "react";
import { History } from "lucide-react";
import { useDay, useGoals, useStateSnapshot } from "@/api/hooks";
import { isDecided, type Interval } from "@/api/types";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { TimelineRow } from "./TimelineRow";

type Filter = "all" | "backup" | "safety";

const filters: { id: Filter; label: string; test: (iv: Interval) => boolean }[] = [
  { id: "all", label: "All", test: () => true },
  { id: "backup", label: "Backup rules only", test: (iv) => iv.decision.source === "fallback" },
  { id: "safety", label: "Safety adjusted only", test: (iv) => iv.guard.clipped },
];

const PAGE = 24;

export function DecisionTimeline() {
  const day = useDay();
  const goals = useGoals();
  const maxPowerMw = useStateSnapshot().data?.max_power_mw ?? 3;
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE);

  const decided = useMemo(() => (day.data?.intervals ?? []).filter(isDecided).reverse(), [day.data]);
  const counts = useMemo(
    () => Object.fromEntries(filters.map((f) => [f.id, decided.filter(f.test).length])) as Record<Filter, number>,
    [decided],
  );
  const rows = decided.filter(filters.find((f) => f.id === filter)!.test);

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-1">
          <CardTitle className="flex items-center gap-2">
            <History className="size-4" /> Decision history
          </CardTitle>
          <CardDescription>Every 15-minute decision today, newest first. Click a row to see the AI's reasoning.</CardDescription>
        </div>
        <div role="tablist" aria-label="Filter decisions" className="flex flex-wrap gap-1 rounded-lg bg-muted p-1">
          {filters.map((f) => (
            <button
              key={f.id}
              role="tab"
              aria-selected={filter === f.id}
              onClick={() => {
                setFilter(f.id);
                setLimit(PAGE);
              }}
              className={cn(
                "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                filter === f.id ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {f.label} <span className="tabular opacity-70">({counts[f.id]})</span>
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        {day.isPending ? (
          <PanelLoading rows={6} />
        ) : day.isError && !day.data ? (
          <PanelError what="the decision history" onRetry={() => day.refetch()} />
        ) : rows.length === 0 ? (
          <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            {decided.length === 0 ? "No decisions yet today." : "No decisions match this filter."}
          </p>
        ) : (
          <>
            <div className="hidden grid-cols-[3.5rem_11rem_1fr_auto_6.5rem_1.25rem] gap-x-3 border-b px-3 pb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase md:grid">
              <span>Time</span>
              <span>Action</span>
              <span>What and why</span>
              <span>Decided by</span>
              <span className="text-right">Profit</span>
              <span />
            </div>
            <ul className="overflow-hidden rounded-b-lg">
              {rows.slice(0, limit).map((iv) => (
                <TimelineRow
                  key={iv.time}
                  iv={iv}
                  goals={goals.data ?? []}
                  maxPowerMw={maxPowerMw}
                  open={open === iv.time}
                  onToggle={() => setOpen((o) => (o === iv.time ? null : iv.time))}
                />
              ))}
            </ul>
            {rows.length > limit && (
              <button
                type="button"
                onClick={() => setLimit((l) => l + PAGE)}
                className="mt-3 w-full rounded-lg border py-2 text-sm font-medium text-muted-foreground hover:bg-accent"
              >
                Show {Math.min(PAGE, rows.length - limit)} more ({rows.length - limit} hidden)
              </button>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
