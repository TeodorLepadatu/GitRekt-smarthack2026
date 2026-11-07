import { Fragment, useState } from "react";
import { useFlag, useUnflag } from "@/api/hooks";
import type { DecisionFlag, Goal, Interval } from "@/api/types";
import { actionWord, SourceMark } from "@/components/common/marks";
import { actionKind, actionShort, confidenceLabel, fallbackReasonText, fmtEur, fmtMw, proposerText, rulesText } from "@/lib/labels";
import { cn } from "@/lib/utils";

interface Props {
  iv: Interval;
  goals: Goal[];
  open: boolean;
  onToggle: () => void;
  maxPowerMw: number;
  flag: DecisionFlag | null;
}

const FLAG_REASONS = ["Wrong direction", "Too early or too late", "Too much power", "Ignored a goal", "Explanation is wrong"];

/** Thumbs down plus a reason. Kept with the decision so it can become a prompt example later. */
function FlagControl({ time, flag }: { time: string; flag: DecisionFlag | null }) {
  const set = useFlag();
  const unset = useUnflag();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const busy = set.isPending || unset.isPending;

  if (flag) {
    return (
      <p className="mt-3 text-sm">
        <span className="font-semibold text-bad">👎 You flagged this decision</span>
        {flag.reason && <span className="text-ink-2">: {flag.reason}</span>}.{" "}
        <button type="button" disabled={busy} onClick={() => unset.mutate(time)} className="text-ink-3 underline underline-offset-2">
          Remove flag
        </button>
      </p>
    );
  }
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="mt-3 text-sm text-ink-2 underline-offset-2 hover:text-ink hover:underline">
        👎 Flag this decision as wrong
      </button>
    );
  }
  return (
    <form
      className="mt-3 flex flex-col gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        set.mutate({ time, reason: reason.trim() }, { onSuccess: () => setOpen(false) });
      }}
    >
      <span className="label">What was wrong?</span>
      <span className="flex flex-wrap gap-x-3 gap-y-1">
        {FLAG_REASONS.map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => setReason(r)}
            className={cn("border px-2 py-0.5", reason === r ? "border-ink bg-ink text-paper" : "border-rule-strong text-ink-2 hover:border-ink")}
          >
            {r}
          </button>
        ))}
      </span>
      <span className="flex flex-wrap items-end gap-4">
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          maxLength={300}
          placeholder="Or say it in your own words"
          aria-label="Reason"
          className="min-w-64 flex-1 border-b border-ink bg-transparent py-1 outline-none placeholder:text-ink-3"
        />
        <button type="submit" disabled={busy} className="bg-ink px-4 py-1.5 font-semibold text-paper disabled:opacity-40">
          Flag
        </button>
        <button type="button" onClick={() => setOpen(false)} className="text-ink-3 underline-offset-2 hover:underline">
          Cancel
        </button>
      </span>
      {set.isError && <span className="text-bad">The flag could not be saved: {set.error.message}</span>}
    </form>
  );
}

export function TimelineRow({ iv, goals, open, onToggle, maxPowerMw, flag }: Props) {
  const sp = iv.guard.battery_setpoint_mw;
  const kind = actionKind(sp);
  const notes = rulesText(iv.guard.applied_rules, goals);
  const fb = fallbackReasonText(iv.decision);
  const profit = iv.settlement.profit_eur;
  const proposed = iv.decision.battery_fraction * maxPowerMw;

  return (
    <Fragment>
      <tr
        onClick={onToggle}
        className={cn("cursor-pointer border-t border-rule align-baseline hover:bg-wash", open && "bg-wash")}
        aria-expanded={open}
      >
        <td className="num py-3.5 pr-6 font-medium">
          <button type="button" onClick={(e) => (e.stopPropagation(), onToggle())} aria-label={`${open ? "Hide" : "Show"} details for ${iv.time}`}>
            {iv.time}
          </button>
          {flag && (
            <span className="ml-1.5 text-sm" title={`Flagged${flag.reason ? `: ${flag.reason}` : ""}`} aria-label="Flagged">
              👎
            </span>
          )}
        </td>
        <td className={cn("num py-3.5 pr-4 whitespace-nowrap", actionWord(sp))}>
          {actionShort[kind]}
          {kind !== "hold" && ` ${fmtMw(Math.abs(sp))}`}
        </td>
        <td className="py-3.5 pr-4">
          {iv.decision.explanation_for_operator}
          {(fb || notes.length > 0) && (
            <span className="mt-0.5 block text-sm text-ink-3">
              {[fb, ...notes].filter(Boolean).join(". ")}.
            </span>
          )}
        </td>
        <td className="hidden py-3.5 pr-4 text-sm md:table-cell">
          <SourceMark interval={iv} />
        </td>
        <td className={cn("num py-3.5 text-right whitespace-nowrap", profit > 0 ? "text-ok" : profit < 0 ? "text-bad" : "text-ink-3")}>
          {fmtEur(profit)}
        </td>
      </tr>
      {open && (
        <tr className="bg-wash">
          <td />
          <td colSpan={4} className="pb-4 text-sm">
            <p className="num mb-2 text-sm text-ink-3">
              price €{iv.price_eur_mwh.toFixed(2)}/MWh · stored after {iv.soc_mwh.toFixed(2)} MWh ·{" "}
              {proposerText(iv.decision)} proposed {fmtMw(proposed)}
              {iv.guard.clipped && `, safety set ${fmtMw(sp)}`}
              {iv.guard.curtail_mw > 0 && ` · ${fmtMw(iv.guard.curtail_mw)} switched off`}
            </p>
            {iv.decision.source === "llm" ? (
              <p className="max-w-[75ch] font-serif text-lg leading-relaxed">
                <span className="label mr-2 align-middle">AI reasoning, {confidenceLabel[iv.decision.confidence].toLowerCase()}</span>
                {iv.decision.reasoning || "The AI gave no reasoning for this decision."}
              </p>
            ) : iv.decision.source === "operator" ? (
              <p className="max-w-[75ch] text-ink-2">{iv.decision.reasoning} The safety layer still checked it.</p>
            ) : (
              <p className="max-w-[75ch] text-ink-2">
                No usable AI answer for this interval, so the backup rules decided from the price and your goals.
              </p>
            )}
            <span className="mt-1 block md:hidden">
              <SourceMark interval={iv} />
            </span>
            <FlagControl time={iv.time} flag={flag} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}
