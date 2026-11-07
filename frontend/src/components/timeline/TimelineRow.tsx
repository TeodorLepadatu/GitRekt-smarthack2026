import { ChevronDown, ShieldAlert, TriangleAlert } from "lucide-react";
import type { Goal, Interval } from "@/api/types";
import { actionColor, ActionIcon } from "@/components/common/ActionIcon";
import { SourceBadge } from "@/components/common/SourceBadge";
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
  const proposed = iv.decision.battery_fraction;

  return (
    <li className="border-b last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="grid w-full grid-cols-[3.5rem_1fr_auto] items-start gap-x-3 gap-y-1 px-3 py-2.5 text-left transition-colors hover:bg-accent/60 md:grid-cols-[3.5rem_11rem_1fr_auto_6.5rem_1.25rem]"
      >
        <span className="tabular pt-0.5 font-semibold">{iv.time}</span>

        <span className={cn("tabular flex items-center gap-1.5 pt-0.5 font-semibold whitespace-nowrap", actionColor[kind])}>
          <ActionIcon kind={kind} className="size-4" />
          {actionShort[kind]}
          {kind !== "hold" && <span className="font-medium">{fmtMw(Math.abs(sp))}</span>}
        </span>

        <span className="col-span-3 flex min-w-0 flex-col gap-1 md:col-span-1">
          <span className="leading-snug">{iv.decision.explanation_for_operator}</span>
          {(fb || notes.length > 0) && (
            <span className="flex flex-col gap-0.5 text-sm text-muted-foreground">
              {fb && (
                <span className="flex items-start gap-1">
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" />
                  {fb}
                </span>
              )}
              {notes.map((n) => (
                <span key={n} className="flex items-start gap-1">
                  <ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-info" />
                  {n}
                </span>
              ))}
            </span>
          )}
        </span>

        <span className="col-start-3 row-start-1 md:col-start-auto md:row-start-auto">
          <SourceBadge interval={iv} />
        </span>

        <span
          className={cn(
            "tabular hidden text-right font-semibold md:block",
            profit > 0 ? "text-ok" : profit < 0 ? "text-bad" : "text-muted-foreground",
          )}
        >
          {fmtEur(profit)}
        </span>

        <ChevronDown className={cn("hidden size-5 text-muted-foreground transition-transform md:block", open && "rotate-180")} />
      </button>

      {open && (
        <div className="flex flex-col gap-2 bg-muted/50 px-3 py-3 text-sm md:pl-[4.25rem]">
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-muted-foreground">
            <span className="md:hidden">
              Profit: <b className={profit >= 0 ? "text-ok" : "text-bad"}>{fmtEur(profit)}</b>
            </span>
            <span>Price: €{iv.price_eur_mwh.toFixed(2)}/MWh</span>
            <span>Battery after: {iv.soc_mwh.toFixed(2)} MWh</span>
            <span>
              {iv.decision.source === "llm" ? "AI proposed" : "Backup rules proposed"}: {fmtMw(proposed * maxPowerMw)}
              {iv.guard.clipped && ` → safety set ${fmtMw(sp)}`}
            </span>
            {iv.guard.curtail_mw > 0 && <span>Switched off: {fmtMw(iv.guard.curtail_mw)}</span>}
          </div>
          {iv.decision.source === "llm" ? (
            <div className="flex flex-col gap-1">
              <span className="font-semibold">
                AI reasoning <span className="font-normal text-muted-foreground">· {confidenceLabel[iv.decision.confidence]}</span>
              </span>
              <p className="leading-relaxed">{iv.decision.reasoning || "The AI gave no reasoning for this decision."}</p>
            </div>
          ) : (
            <p className="leading-relaxed">
              The AI's answer could not be used for this interval, so the backup rules decided using simple price
              thresholds and your goals.
            </p>
          )}
        </div>
      )}
    </li>
  );
}
