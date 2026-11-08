import { useMemo, useState } from "react";
import { useDay, useFlags, useGoals, useStateSnapshot } from "@/api/hooks";
import { isDecided, type DecisionFlag, type Interval } from "@/api/types";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { cn } from "@/lib/utils";
import { TimelineRow } from "./TimelineRow";

type Filter = "all" | "backup" | "operator" | "safety" | "flagged";

const filters: { id: Filter; label: string; test: (iv: Interval, flags: Map<string, DecisionFlag>) => boolean }[] = [
  { id: "all", label: "All", test: () => true },
  { id: "backup", label: "Backup rules", test: (iv) => iv.decision.source === "fallback" },
  { id: "operator", label: "Operator involved", test: (iv) => iv.control.action !== "auto" },
  { id: "safety", label: "Changed by safety", test: (iv) => iv.guard.clipped },
  { id: "flagged", label: "Flagged", test: (iv, flags) => flags.has(iv.time) },
];

const PAGE = 24;

export function DecisionTimeline() {
  const day = useDay();
  const goals = useGoals();
  const maxPowerMw = useStateSnapshot().data?.max_power_mw ?? 3;
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE);

  const flagList = useFlags().data;
  const flags = useMemo(() => new Map((flagList ?? []).map((f) => [f.time, f])), [flagList]);
  const decided = useMemo(() => (day.data?.intervals ?? []).filter(isDecided).reverse(), [day.data]);
  const counts = useMemo(
    () => Object.fromEntries(filters.map((f) => [f.id, decided.filter((iv) => f.test(iv, flags)).length])) as Record<Filter, number>,
    [decided, flags],
  );
  const test = filters.find((f) => f.id === filter)!.test;
  const rows = decided.filter((iv) => test(iv, flags));

  return (
    <div className="flex flex-col gap-3">
      <div role="tablist" aria-label="Show" className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm">
        <span className="label">Show</span>
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
              "border-b-2 pb-0.5",
              filter === f.id ? "border-ink font-semibold text-ink" : "border-transparent text-ink-3 hover:text-ink",
            )}
          >
            {f.label} <span className="num text-ink-3">{counts[f.id]}</span>
          </button>
        ))}
      </div>

      {day.isPending ? (
        <PanelLoading rows={8} />
      ) : day.isError && !day.data ? (
        <PanelError what="the decision log" onRetry={() => day.refetch()} />
      ) : rows.length === 0 ? (
        <p className="border-t border-rule-strong py-6 font-serif text-lg text-ink-2">
          {decided.length === 0 ? "No decisions yet today." : "None today."}
        </p>
      ) : (
        <>
          <table className="w-full border-b border-rule-strong text-left">
            <thead>
              <tr className="border-b border-rule-strong align-bottom">
                <th className="label pr-4 pb-2 font-semibold">Time</th>
                <th className="label pr-4 pb-2 font-semibold">Battery</th>
                <th className="label pr-4 pb-2 font-semibold">Reason</th>
                <th className="label hidden pr-4 pb-2 font-semibold md:table-cell">Decided by</th>
                <th className="label pb-2 text-right font-semibold">Profit</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, limit).map((iv) => (
                <TimelineRow
                  key={iv.time}
                  iv={iv}
                  goals={goals.data ?? []}
                  maxPowerMw={maxPowerMw}
                  flag={flags.get(iv.time) ?? null}
                  open={open === iv.time}
                  onToggle={() => setOpen((o) => (o === iv.time ? null : iv.time))}
                />
              ))}
            </tbody>
          </table>
          {rows.length > limit && (
            <button
              type="button"
              onClick={() => setLimit((l) => l + PAGE)}
              className="w-fit text-sm font-medium underline underline-offset-2"
            >
              Show {Math.min(PAGE, rows.length - limit)} earlier ({rows.length - limit} not shown)
            </button>
          )}
        </>
      )}
    </div>
  );
}
