import { useState } from "react";
import { useAlerts, useIgnoreAlert, useRelaxGoal } from "@/api/hooks";
import type { Alert, AlertAction, AlertSeverity } from "@/api/types";
import type { TabId } from "@/components/layout/tabs";
import { alertActionLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";

const tone: Record<AlertSeverity, string> = { critical: "text-bad", warning: "text-warn", info: "text-info" };
const glyph: Record<AlertSeverity, string> = { critical: "■", warning: "▲", info: "●" };
const word: Record<AlertSeverity, string> = { critical: "Needs action", warning: "Warning", info: "Heads-up" };

/**
 * What needs the operator's attention right now. Raised by the backend after
 * every interval (and pushed over the SSE stream), gone by itself once the
 * condition clears. Shown on every tab, under the top bar.
 */
export function AlertsPanel({ go }: { go: (t: TabId) => void }) {
  const alerts = useAlerts();
  const ignore = useIgnoreAlert();
  const relax = useRelaxGoal();
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const list = alerts.data ?? [];

  if (list.length === 0 && !result) return null;

  async function act(a: Alert, action: AlertAction) {
    setResult(null);
    if (action === "override") {
      go("day");
      // the Day tab mounts first, then the panel can be scrolled to
      setTimeout(() => document.getElementById("upcoming")?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
      return;
    }
    try {
      if (action === "ignore") await ignore.mutateAsync(a.id);
      if (action === "relax_goal" && a.goal_id) {
        const r = await relax.mutateAsync(a.goal_id);
        setResult({ ok: true, text: `Goal relaxed. ${r.message}` });
      }
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : "That did not work. Try again in a moment." });
    }
  }

  const busy = ignore.isPending || relax.isPending;

  return (
    <section aria-label="Alerts" aria-live="polite" className="border-b border-rule-strong bg-sheet px-6 py-3 lg:px-12">
      <ul className="flex flex-col divide-y divide-rule">
        {list.map((a) => (
          <li key={a.id} className="flex flex-wrap items-baseline gap-x-6 gap-y-1.5 py-2">
            <span className={cn("label inline-flex w-28 shrink-0 items-baseline gap-1.5", tone[a.severity])}>
              <span aria-hidden className="text-[0.8em]">
                {glyph[a.severity]}
              </span>
              {word[a.severity]}
            </span>
            <div className="min-w-0 flex-1 basis-80">
              <p className="font-semibold">
                {a.title}
                {a.time && <span className="num ml-2 text-sm font-normal text-ink-3">since {a.time}</span>}
              </p>
              <p className="text-sm text-ink-2">{a.message}</p>
            </div>
            <div className="flex shrink-0 flex-wrap gap-x-4 gap-y-1 text-sm">
              {a.actions.map((action) => (
                <button
                  key={action}
                  type="button"
                  disabled={busy}
                  onClick={() => void act(a, action)}
                  className={cn(
                    "underline-offset-2 hover:underline disabled:opacity-50",
                    action === "ignore" ? "text-ink-3" : "font-semibold text-ink",
                  )}
                >
                  {alertActionLabel[action]}
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>
      {result && (
        <p className={cn("py-1 text-sm", result.ok ? "text-ok" : "text-bad")} role="status">
          {result.text}{" "}
          <button type="button" className="text-ink-3 underline underline-offset-2" onClick={() => setResult(null)}>
            Dismiss
          </button>
        </p>
      )}
    </section>
  );
}
