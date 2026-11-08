import { useEffect, useState } from "react";
import { useAnswerApproval, useClearOverride, useControl, useSetHold } from "@/api/hooks";
import type { PendingApproval } from "@/api/types";
import { actionKind, actionPhrase, confidenceLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";

const btn = "px-3 py-1 text-sm font-semibold disabled:opacity-50";

/** Seconds left on a proposal, counted down from what the server said when it was sent. */
function Countdown({ seconds }: { seconds: number }) {
  const [deadline] = useState(() => Date.now() + seconds * 1000);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, []);
  return <span className="num">{Math.max(0, Math.ceil((deadline - now) / 1000))} s</span>;
}

function Approval({ p }: { p: PendingApproval }) {
  const answer = useAnswerApproval();
  const kind = actionKind(p.setpoint_mw);
  return (
    <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2 border-t-2 border-warn bg-sheet px-6 py-3 lg:px-12" role="alert">
      <span className="label text-warn">Waiting for you</span>
      <span className="text-base">
        {p.source === "llm" ? "The AI wants" : "The backup rules want"} to{" "}
        <span className={cn("font-semibold", kind === "charge" ? "text-charge" : kind === "discharge" ? "text-discharge" : "")}>
          {actionPhrase(p.setpoint_mw)}
        </span>{" "}
        at <span className="num">{p.time}</span>
        <span className="text-ink-3"> · {confidenceLabel[p.confidence].toLowerCase()}</span>
      </span>
      {p.explanation_for_operator && <span className="max-w-[60ch] text-sm text-ink-2">“{p.explanation_for_operator}”</span>}
      <span className="ml-auto flex items-baseline gap-3">
        <button
          type="button"
          className={cn(btn, "bg-ink text-paper")}
          disabled={answer.isPending}
          onClick={() => answer.mutate({ id: p.id, approve: true })}
        >
          Approve
        </button>
        <button
          type="button"
          className={cn(btn, "border border-ink")}
          disabled={answer.isPending}
          onClick={() => answer.mutate({ id: p.id, approve: false })}
        >
          Reject
        </button>
        <span className="text-sm text-ink-3">
          holds in <Countdown key={p.id} seconds={p.expires_in_s} />
        </span>
      </span>
    </div>
  );
}

/**
 * Along the bottom of every tab: who is in control, the emergency hold
 * button, and any AI proposal waiting for the operator's click.
 */
export function OperatorStrip() {
  const { data: c } = useControl();
  const hold = useSetHold();
  const clear = useClearOverride();
  if (!c) return null;

  const status = c.hold
    ? "Emergency hold is on: the battery is idle and nothing is exported."
    : c.override
      ? `Manual override: ${actionPhrase(c.override.setpoint_mw)} until ${c.override.until}.`
      : c.autonomy.mode === "autonomous"
        ? "The AI is in control."
        : c.autonomy.mode === "supervised"
          ? `The AI is in control; moves above ${c.autonomy.max_auto_mw} MW${c.autonomy.ask_on_low_confidence ? " or unsure ones" : ""} wait for you.`
          : "Advisory only: every move waits for your approval.";

  return (
    <div aria-label="Operator control">
      {c.pending && <Approval p={c.pending} />}
      <div
        className={cn(
          "flex flex-wrap items-center gap-x-5 gap-y-1 border-t px-6 py-2 lg:px-12",
          c.hold ? "border-bad bg-bad text-paper" : "border-rule-strong bg-sheet",
        )}
      >
        <span className={cn("label", !c.hold && "text-ink-3")}>Operator</span>
        <span className="text-base">{status}</span>
        {c.override && !c.hold && (
          <button type="button" className="text-sm underline underline-offset-2" disabled={clear.isPending} onClick={() => clear.mutate()}>
            Hand back to the AI
          </button>
        )}
        <button
          type="button"
          disabled={hold.isPending}
          onClick={() => hold.mutate(!c.hold)}
          className={cn(
            "ml-auto px-4 py-1.5 text-sm font-semibold tracking-wide uppercase disabled:opacity-50",
            c.hold ? "border border-paper" : "bg-bad text-paper",
          )}
          title={c.hold ? "Hand the battery back" : "Battery idle and no export from the next quarter-hour, until released"}
        >
          {c.hold ? "Release hold" : "Emergency hold"}
        </button>
      </div>
    </div>
  );
}
