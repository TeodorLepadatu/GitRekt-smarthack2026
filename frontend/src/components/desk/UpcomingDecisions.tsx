import { useState } from "react";
import { useClearPlan, useSetPlan, useUpcoming } from "@/api/hooks";
import type { UpcomingSlot } from "@/api/types";
import { actionWord, Section } from "@/components/common/marks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { fmtMw, fmtPrice, fractionText } from "@/lib/labels";
import { cn } from "@/lib/utils";

type Dir = "charge" | "hold" | "discharge";

/**
 * The next two hours, interval by interval: what is planned and the operator's
 * say in it. "Skip" holds the battery for that interval; "Change" sets another
 * action. Either way the AI is not asked for that interval, and the safety
 * layer still checks the action against the battery limits and hard goals.
 */
export function UpcomingDecisions() {
  const up = useUpcoming();
  const set = useSetPlan();
  const clear = useClearPlan();
  const [editing, setEditing] = useState<string | null>(null);
  const busy = set.isPending || clear.isPending;
  const error = set.error ?? clear.error;

  return (
    <div id="upcoming" className="scroll-mt-24">
      <Section
        label="Next two hours"
        title="Upcoming decisions"
        aside={<p className="max-w-[52ch] text-sm text-ink-3">Planned on the forecast by the backup rules; the AI decides each one when it comes, unless you set it here.</p>}
      >
        {up.isPending ? (
          <PanelLoading rows={6} />
        ) : up.isError && !up.data ? (
          <PanelError what="the upcoming decisions" onRetry={() => up.refetch()} />
        ) : up.data.slots.length === 0 ? (
          <p className="py-4 font-serif text-lg text-ink-2">The day is over. Nothing left to plan.</p>
        ) : (
          <table className="w-full border-b border-rule-strong text-left">
            <thead>
              <tr className="border-b border-rule-strong align-bottom">
                <th className="label pr-4 pb-2 font-semibold">Time</th>
                <th className="label hidden pr-4 pb-2 font-semibold sm:table-cell">Price</th>
                <th className="label pr-4 pb-2 font-semibold">Planned</th>
                <th className="label pr-4 pb-2 font-semibold">Will run</th>
                <th className="label pb-2 text-right font-semibold">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {up.data.slots.map((s) => (
                <Row
                  key={s.time}
                  s={s}
                  busy={busy}
                  editing={editing === s.time}
                  onEdit={() => setEditing((e) => (e === s.time ? null : s.time))}
                  onSkip={() => set.mutate({ time: s.time, fraction: 0, note: "skipped" })}
                  onUndo={() => clear.mutate(s.time)}
                  onSave={(fraction, note) => {
                    set.mutate({ time: s.time, fraction, note }, { onSuccess: () => setEditing(null) });
                  }}
                />
              ))}
            </tbody>
          </table>
        )}
        {error && <PanelError className="mt-2" message={`Could not change that interval: ${error.message}`} />}
      </Section>
    </div>
  );
}

interface RowProps {
  s: UpcomingSlot;
  busy: boolean;
  editing: boolean;
  onEdit: () => void;
  onSkip: () => void;
  onUndo: () => void;
  onSave: (fraction: number, note: string) => void;
}

function Row({ s, busy, editing, onEdit, onSkip, onUndo, onSave }: RowProps) {
  const o = s.override;
  const runSp = o ? o.setpoint_mw : s.planned.setpoint_mw;
  const btn = "underline-offset-2 hover:underline disabled:opacity-50";
  return (
    <>
      <tr className={cn("border-t border-rule align-baseline", o && "bg-wash")}>
        <td className="num py-3 pr-4 font-medium">{s.time}</td>
        <td className="num hidden py-3 pr-4 text-ink-2 sm:table-cell">{fmtPrice(s.price_eur_mwh)}</td>
        <td className={cn("py-3 pr-4", o && "text-ink-3 line-through decoration-ink-3/60")}>
          <span className={cn("num whitespace-nowrap", !o && actionWord(s.planned.setpoint_mw))}>{mwText(s.planned.setpoint_mw)}</span>
          <span className="mt-0.5 block text-sm text-ink-3 no-underline">{s.planned.explanation}</span>
        </td>
        <td className="py-3 pr-4">
          {o ? (
            <>
              <span className={cn("num font-semibold whitespace-nowrap", actionWord(runSp))}>{mwText(runSp)}</span>
              <span className="mt-0.5 block text-sm text-ai">
                You: {fractionText(o.battery_fraction).toLowerCase()}
                {o.note && o.note !== "skipped" ? ` · ${o.note}` : ""}
                {o.limited_by_safety && <span className="text-info"> · limited by safety</span>}
              </span>
            </>
          ) : (
            <span className="text-sm text-ink-3">AI decides</span>
          )}
        </td>
        <td className="py-3 text-right text-sm whitespace-nowrap">
          {o ? (
            <button type="button" className={cn(btn, "font-semibold")} disabled={busy} onClick={onUndo}>
              Give back to AI
            </button>
          ) : (
            <span className="inline-flex gap-4">
              <button type="button" className={cn(btn, "font-semibold")} disabled={busy} onClick={onSkip} title="Hold the battery for this interval">
                Skip
              </button>
              <button type="button" className={cn(btn, "text-ink-2")} disabled={busy} onClick={onEdit} aria-expanded={editing}>
                Change
              </button>
            </span>
          )}
        </td>
      </tr>
      {editing && !o && (
        <tr className="bg-wash">
          <td colSpan={5} className="px-2 pb-4">
            <OverrideEditor
              initial={s.planned.battery_fraction}
              busy={busy}
              onCancel={onEdit}
              onSave={onSave}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function mwText(sp: number) {
  if (Math.abs(sp) < 0.005) return "Hold";
  return `${sp > 0 ? "Charge" : "Discharge"} ${fmtMw(Math.abs(sp))}`;
}

function OverrideEditor({ initial, busy, onCancel, onSave }: { initial: number; busy: boolean; onCancel: () => void; onSave: (f: number, note: string) => void }) {
  const [dir, setDir] = useState<Dir>(initial > 0.005 ? "charge" : initial < -0.005 ? "discharge" : "hold");
  const [pct, setPct] = useState(Math.round(Math.abs(initial) * 100) || 50);
  const [note, setNote] = useState("");
  const fraction = dir === "hold" ? 0 : (dir === "charge" ? 1 : -1) * (pct / 100);

  return (
    <form
      className="flex flex-wrap items-end gap-x-6 gap-y-3 pt-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(fraction, note.trim());
      }}
    >
      <fieldset className="flex flex-col gap-1">
        <legend className="label mb-1">Action</legend>
        <div className="flex border border-ink" role="radiogroup">
          {(["charge", "hold", "discharge"] as Dir[]).map((d) => (
            <button
              key={d}
              type="button"
              role="radio"
              aria-checked={dir === d}
              onClick={() => setDir(d)}
              className={cn("px-3 py-1.5 text-sm capitalize", dir === d ? "bg-ink text-paper" : "text-ink-2 hover:text-ink")}
            >
              {d}
            </button>
          ))}
        </div>
      </fieldset>
      <label className={cn("flex flex-col gap-1", dir === "hold" && "opacity-40")}>
        <span className="label">Power</span>
        <span className="flex items-center gap-3">
          <input
            type="range"
            min={5}
            max={100}
            step={5}
            value={pct}
            disabled={dir === "hold"}
            onChange={(e) => setPct(Number(e.target.value))}
            className="w-40 accent-[var(--ink)]"
          />
          <span className="num w-12 text-sm">{pct}%</span>
        </span>
      </label>
      <label className="flex min-w-48 flex-1 flex-col gap-1">
        <span className="label">Why (optional, kept in the log)</span>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={200}
          placeholder="e.g. grid operator asked us to hold"
          className="border-b border-ink bg-transparent py-1 outline-none placeholder:text-ink-3"
        />
      </label>
      <span className="flex gap-4 text-sm">
        <button type="submit" disabled={busy} className="bg-ink px-4 py-1.5 font-semibold text-paper disabled:opacity-40">
          Set {fractionText(fraction).toLowerCase()}
        </button>
        <button type="button" onClick={onCancel} className="text-ink-3 underline-offset-2 hover:underline">
          Cancel
        </button>
      </span>
    </form>
  );
}
