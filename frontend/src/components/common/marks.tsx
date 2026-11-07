/**
 * Typographic markers used instead of pill badges and icon sets:
 * a coloured word, sometimes with a plain text glyph in front.
 */
import type { ReactNode } from "react";
import type { GoalStatus, Interval } from "@/api/types";
import { actionKind, goalStatusLabel, sourceHint, sourceKind, sourceLabel, type ActionKind, type SourceKind } from "@/lib/labels";
import { cn } from "@/lib/utils";

export const actionText: Record<ActionKind, string> = {
  charge: "text-charge",
  discharge: "text-discharge",
  hold: "text-ink-3",
};

export const goalTone: Record<GoalStatus, string> = {
  on_track: "text-ok",
  behind: "text-warn",
  met: "text-ink",
  unreachable: "text-bad",
  missed: "text-bad",
  violated: "text-bad",
};

export const goalBar: Record<GoalStatus, string> = {
  on_track: "bg-ok",
  behind: "bg-warn",
  met: "bg-ink",
  unreachable: "bg-bad",
  missed: "bg-bad",
  violated: "bg-bad",
};

const sourceTone: Record<SourceKind, string> = { ai: "text-ink", backup: "text-warn", safety: "text-info" };
/** Plain glyphs, so who-decided reads even in black and white. */
const sourceGlyph: Record<SourceKind, string> = { ai: "●", backup: "▲", safety: "◆" };

export function SourceMark({ interval, className }: { interval: Pick<Interval, "decision" | "guard">; className?: string }) {
  const kind = sourceKind(interval);
  return (
    <span className={cn("inline-flex items-baseline gap-1.5 whitespace-nowrap", sourceTone[kind], className)} title={sourceHint[kind]}>
      <span aria-hidden className="text-[0.7em]">
        {sourceGlyph[kind]}
      </span>
      {sourceLabel[kind]}
    </span>
  );
}

export function GoalStatusWord({ status, className }: { status: GoalStatus; className?: string }) {
  return <span className={cn("font-medium whitespace-nowrap", goalTone[status], className)}>{goalStatusLabel[status]}</span>;
}

/** Thin progress line, no track styling beyond a rule. */
export function ProgressLine({ value, status, label }: { value: number; status: GoalStatus; label: string }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v)} className="h-[3px] w-full bg-rule">
      <div className={cn("h-full", goalBar[status])} style={{ width: `${v}%` }} />
    </div>
  );
}

/** A page section: small label on top, then content. Separated by a rule, not a card. */
export function Section({ label, title, aside, children, className }: { label?: string; title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("border-t border-rule-strong pt-4", className)}>
      {(label || title || aside) && (
        <header className="mb-4 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <div className="flex flex-col gap-0.5">
            {label && <span className="label">{label}</span>}
            {title && <h2 className="font-serif text-2xl leading-tight font-semibold">{title}</h2>}
          </div>
          {aside}
        </header>
      )}
      {children}
    </section>
  );
}

export function actionWord(sp: number) {
  return actionText[actionKind(sp)];
}
