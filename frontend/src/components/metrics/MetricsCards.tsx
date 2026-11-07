import type { ReactNode } from "react";
import { useMetrics } from "@/api/hooks";
import type { Metrics } from "@/api/types";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { fmtEur, fmtMwh, fmtPct } from "@/lib/labels";
import { cn } from "@/lib/utils";

type Figure = { label: string; value: ReactNode; note?: string; tone?: string };

function energyFigures(d: Metrics): Figure[] {
  const up = d.profit_today_eur >= 0;
  return [
    {
      label: "Profit since 00:00",
      value: `${up ? "+" : ""}${fmtEur(d.profit_today_eur)}`,
      tone: up ? "text-ok" : "text-bad",
      note: `after ${fmtEur(Math.abs(d.wear_eur))} battery wear`,
    },
    {
      label: "Battery's share",
      value: `${d.value_added_eur >= 0 ? "+" : ""}${fmtEur(d.value_added_eur)}`,
      tone: d.value_added_eur >= 0 ? "text-ok" : "text-bad",
      note: "compared with no battery",
    },
    { label: "Sold", value: fmtMwh(d.energy_sold_mwh), note: "exported to the grid" },
    { label: "Bought", value: fmtMwh(d.energy_bought_mwh), note: "imported from the grid" },
    { label: "Switched off", value: fmtMwh(d.curtailed_mwh), note: "solar/wind not used" },
  ];
}

function trustFigures(d: Metrics): Figure[] {
  const total = Math.max(1, d.decisions_total);
  return [
    { label: "Decisions", value: d.decisions_total, note: "since 00:00" },
    { label: "By the AI", value: fmtPct((d.decisions_llm / total) * 100), note: `${d.decisions_llm} decisions` },
    {
      label: "By backup rules",
      value: d.decisions_fallback,
      tone: d.decisions_fallback ? "text-warn" : undefined,
      note: "AI answer missing or invalid",
    },
    { label: "Changed by safety", value: d.decisions_clipped, tone: d.decisions_clipped ? "text-info" : undefined, note: "limits or hard goals" },
    { label: "Goals met", value: `${d.goals_met} / ${d.goals_total}`, note: "with a target" },
  ];
}

/** One ruled row of figures (a ledger line, not a grid of cards). */
export function MetricsCards({ kind }: { kind: "energy" | "trust" }) {
  const m = useMetrics();
  if (m.isPending) return <PanelLoading rows={2} />;
  if (m.isError && !m.data) return <PanelError what="today's figures" onRetry={() => m.refetch()} />;
  const figures = kind === "energy" ? energyFigures(m.data) : trustFigures(m.data);
  return (
    <dl className="grid grid-cols-2 border-y border-rule-strong sm:grid-cols-3 lg:grid-cols-5">
      {figures.map((f, i) => (
        <div key={f.label} className={cn("flex flex-col gap-1 py-5 pr-6", i > 0 && "lg:border-l lg:pl-6")}>
          <dt className="label">{f.label}</dt>
          <dd className={cn("num text-3xl font-medium", f.tone)}>{f.value}</dd>
          {f.note && <dd className="text-sm text-ink-3">{f.note}</dd>}
        </div>
      ))}
    </dl>
  );
}
