import { useAudit } from "@/api/hooks";
import type { AuditAction } from "@/api/types";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { clockTime } from "@/lib/labels";
import { cn } from "@/lib/utils";

const kind: Record<AuditAction, { label: string; tone: string }> = {
  goal_added: { label: "Goal", tone: "text-ok" },
  goal_removed: { label: "Goal", tone: "text-bad" },
  goal_relaxed: { label: "Goal", tone: "text-warn" },
  override_set: { label: "Override", tone: "text-ai" },
  override_cleared: { label: "Override", tone: "text-ink-2" },
  alert_ignored: { label: "Alert", tone: "text-ink-2" },
  decision_flagged: { label: "Flag", tone: "text-bad" },
  decision_unflagged: { label: "Flag", tone: "text-ink-2" },
  sim_speed: { label: "Settings", tone: "text-ink-2" },
  sim_reset: { label: "Settings", tone: "text-ink-2" },
};

/** Every operator action, latest first: who stepped in, when, and what changed. */
export function AuditLog() {
  const audit = useAudit();

  if (audit.isPending) return <PanelLoading rows={8} />;
  if (audit.isError && !audit.data) return <PanelError what="the audit log" onRetry={() => audit.refetch()} />;
  if (audit.data.length === 0)
    return <p className="border-t border-rule-strong py-6 font-serif text-lg text-ink-2">No operator actions yet. Overrides, goal changes, flags and ignored alerts show up here.</p>;

  return (
    <table className="w-full border-b border-rule-strong text-left">
      <thead>
        <tr className="border-b border-rule-strong align-bottom">
          <th className="label pr-4 pb-2 font-semibold">When</th>
          <th className="label hidden pr-4 pb-2 font-semibold sm:table-cell">Site time</th>
          <th className="label pr-4 pb-2 font-semibold">Kind</th>
          <th className="label pb-2 font-semibold">What</th>
        </tr>
      </thead>
      <tbody>
        {audit.data.map((e) => {
          const k = kind[e.action] ?? { label: e.action, tone: "text-ink-2" };
          return (
            <tr key={e.id} className="border-t border-rule align-baseline">
              <td className="num py-3 pr-4 whitespace-nowrap" title={e.at}>
                {clockTime(e.at)}
                <span className="block text-xs text-ink-3">{e.date}</span>
              </td>
              <td className="num hidden py-3 pr-4 text-ink-2 sm:table-cell">{e.sim_time ?? "before 00:00"}</td>
              <td className={cn("label py-3 pr-4", k.tone)}>{k.label}</td>
              <td className="py-3">{e.summary}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
