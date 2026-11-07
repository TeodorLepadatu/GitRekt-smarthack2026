import type { ReactNode } from "react";
import { ArrowDownToLine, ArrowUpFromLine, Bot, Euro, ListChecks, ScissorsLineDashed, ShieldCheck, Target } from "lucide-react";
import { useMetrics } from "@/api/hooks";
import type { Metrics } from "@/api/types";
import { PanelError } from "@/components/common/PanelState";
import { Card, Eyebrow } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtEur, fmtMwh, fmtPct } from "@/lib/labels";
import { cn } from "@/lib/utils";

function Tile({
  icon,
  iconClass,
  label,
  value,
  sub,
  valueClass,
}: {
  icon: ReactNode;
  iconClass: string;
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  valueClass?: string;
}) {
  return (
    <Card className="flex items-start gap-4 p-5">
      <span className={cn("flex size-11 shrink-0 items-center justify-center rounded-xl [&_svg]:size-5", iconClass)}>{icon}</span>
      <div className="flex min-w-0 flex-col gap-0.5">
        <Eyebrow>{label}</Eyebrow>
        <span className={cn("num text-2xl font-bold", valueClass)}>{value}</span>
        {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
      </div>
    </Card>
  );
}

function energyTiles(d: Metrics) {
  const up = d.profit_today_eur >= 0;
  return [
    <Tile
      key="profit"
      icon={<Euro />}
      iconClass={up ? "bg-ok/12 text-ok" : "bg-bad/12 text-bad"}
      label="Profit today"
      value={`${up ? "+" : ""}${fmtEur(d.profit_today_eur)}`}
      valueClass={up ? "text-ok" : "text-bad"}
      sub={`${up ? "Earned" : "Spent"} so far, after ${fmtEur(Math.abs(d.wear_eur))} battery wear · ${fmtEur(Math.abs(d.value_added_eur))} ${d.value_added_eur >= 0 ? "more" : "less"} than with no battery`}
    />,
    <Tile key="sold" icon={<ArrowUpFromLine />} iconClass="bg-grid/12 text-grid" label="Energy sold" value={fmtMwh(d.energy_sold_mwh)} sub="Exported to the grid" />,
    <Tile key="bought" icon={<ArrowDownToLine />} iconClass="bg-grid/12 text-grid" label="Energy bought" value={fmtMwh(d.energy_bought_mwh)} sub="Imported from the grid" />,
    <Tile
      key="curtailed"
      icon={<ScissorsLineDashed />}
      iconClass="bg-solar/15 text-solar"
      label="Switched off"
      value={fmtMwh(d.curtailed_mwh)}
      sub="Solar/wind not used (curtailed)"
    />,
  ];
}

function trustTiles(d: Metrics) {
  const total = Math.max(1, d.decisions_total);
  return [
    <Tile
      key="ai"
      icon={<Bot />}
      iconClass="bg-ai/12 text-ai"
      label="Decided by the AI"
      value={fmtPct((d.decisions_llm / total) * 100)}
      sub={`${d.decisions_llm} of ${d.decisions_total} decisions`}
    />,
    <Tile
      key="backup"
      icon={<ListChecks />}
      iconClass="bg-warn/15 text-warn"
      label="Backup rules"
      value={fmtPct((d.decisions_fallback / total) * 100)}
      sub={`${d.decisions_fallback} ${d.decisions_fallback === 1 ? "time" : "times"} the AI's answer was not usable`}
    />,
    <Tile
      key="safety"
      icon={<ShieldCheck />}
      iconClass="bg-info/12 text-info"
      label="Adjusted by safety"
      value={fmtPct((d.decisions_clipped / total) * 100)}
      sub={`${d.decisions_clipped} ${d.decisions_clipped === 1 ? "decision" : "decisions"} changed to protect the battery or your goals`}
    />,
    <Tile
      key="goals"
      icon={<Target />}
      iconClass="bg-ok/12 text-ok"
      label="Goals met today"
      value={
        <>
          {d.goals_met} <span className="text-base font-medium text-muted-foreground">/ {d.goals_total}</span>
        </>
      }
      sub="Hard and soft goals with a target"
    />,
  ];
}

export function MetricsCards({ kind }: { kind: "energy" | "trust" }) {
  const m = useMetrics();
  const grid = "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4";

  if (m.isPending) {
    return (
      <div className={grid}>
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-[104px] rounded-2xl" />
        ))}
      </div>
    );
  }
  if (m.isError && !m.data) return <PanelError what="today's numbers" onRetry={() => m.refetch()} />;
  return <div className={grid}>{kind === "energy" ? energyTiles(m.data) : trustTiles(m.data)}</div>;
}
