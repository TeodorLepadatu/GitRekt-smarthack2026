import { Fragment } from "react";
import type { Goal, Interval } from "@/api/types";
import { actionWord, SourceMark } from "@/components/common/marks";
import { actionKind, actionShort, confidenceLabel, fallbackReasonText, fmtEur, fmtMw, rulesText } from "@/lib/labels";
import { cn } from "@/lib/utils";

interface Props {
  iv: Interval;
  goals: Goal[];
  open: boolean;
  onToggle: () => void;
  maxPowerMw: number;
}

export function TimelineRow({ iv, goals, open, onToggle, maxPowerMw }: Props) {
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
              {iv.decision.source === "llm" ? "AI" : "backup rules"} proposed {fmtMw(proposed)}
              {iv.guard.clipped && `, safety set ${fmtMw(sp)}`}
              {iv.guard.curtail_mw > 0 && ` · ${fmtMw(iv.guard.curtail_mw)} switched off`}
            </p>
            {iv.decision.source === "llm" ? (
              <p className="max-w-[75ch] font-serif text-lg leading-relaxed">
                <span className="label mr-2 align-middle">AI reasoning, {confidenceLabel[iv.decision.confidence].toLowerCase()}</span>
                {iv.decision.reasoning || "The AI gave no reasoning for this decision."}
              </p>
            ) : (
              <p className="max-w-[75ch] text-ink-2">
                No usable AI answer for this interval, so the backup rules decided from the price and your goals.
              </p>
            )}
            <span className="mt-1 block md:hidden">
              <SourceMark interval={iv} />
            </span>
          </td>
        </tr>
      )}
    </Fragment>
  );
}
