import type { ReactNode } from "react";
import { useReport, useReportSummary } from "@/api/hooks";
import type { DayReport, ReportDecision } from "@/api/types";
import { GoalStatusWord } from "@/components/common/marks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { Reveal } from "@/components/common/Reveal";
import { actionKind, fmtEur, fmtMw } from "@/lib/labels";
import { cn } from "@/lib/utils";

const WHO: Record<ReportDecision["source"], string> = { llm: "AI", fallback: "backup rules", operator: "operator" };

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Reveal>
      <section className="flex flex-col gap-4 border-t border-rule-strong pt-6 break-inside-avoid">
        <h2 className="font-serif text-3xl font-semibold tracking-tight">{title}</h2>
        {children}
      </section>
    </Reveal>
  );
}

function Money({ v, className }: { v: number; className?: string }) {
  return <span className={cn("num", v > 0.5 ? "text-ok" : v < -0.5 ? "text-bad" : "text-ink-3", className)}>{fmtEur(v)}</span>;
}

function DecisionItem({ d }: { d: ReportDecision }) {
  const k = actionKind(d.setpoint_mw);
  return (
    <li className="grid grid-cols-[4.5rem_1fr_auto] gap-x-4 gap-y-1 border-t border-rule py-3 first:border-t-0">
      <span className="num font-medium">{d.time}</span>
      <div className="flex flex-col gap-1">
        <p className="leading-snug">{d.summary}</p>
        {d.facts.length > 0 && <p className="num text-sm text-ink-3">{d.facts.join(" · ")}</p>}
        {d.safety && <p className="text-sm text-warn">{d.safety}</p>}
        <p className="text-sm text-ink-3">
          Decided by the {WHO[d.source]}
          {k !== "hold" && ` · ${k} ${fmtMw(Math.abs(d.setpoint_mw))}`}
          {d.ai_note && <> · AI's note: “{d.ai_note}”</>}
        </p>
      </div>
      <Money v={d.value_eur} className="text-right" />
    </li>
  );
}

function AiParagraph() {
  const ai = useReportSummary();
  return (
    <div className="flex flex-col gap-2 print:hidden">
      {ai.data ? (
        <div className="flex flex-col gap-1">
          <span className="label">{ai.data.source === "ai" ? "Written by the AI from the figures below" : "The AI's paragraph was not used"}</span>
          {ai.data.source === "ai" ? (
            <p className="max-w-[70ch] font-serif text-xl leading-relaxed">{ai.data.summary}</p>
          ) : (
            <p className="text-sm text-ink-3">Reason: {ai.data.note}. The summary above is built from the numbers.</p>
          )}
        </div>
      ) : (
        <button
          type="button"
          disabled={ai.isPending}
          onClick={() => ai.mutate()}
          className="w-fit text-sm text-ink-2 underline underline-offset-2 hover:text-ink disabled:opacity-50"
        >
          {ai.isPending ? "The AI is writing a summary… (this can take half a minute)" : "Let the AI write the summary"}
        </button>
      )}
      {ai.isError && <p className="text-sm text-bad">The AI summary failed: {ai.error.message}</p>}
    </div>
  );
}

function Headline({ r }: { r: DayReport }) {
  const e = r.energy;
  return (
    <div className="flex flex-col gap-4">
      <p className="label">
        {r.date} · {r.complete ? "whole day" : r.through ? `so far, 00:00–${r.through} (${r.intervals} of ${r.total_intervals} intervals)` : "not started"}
      </p>
      <p className="font-serif text-6xl leading-none font-semibold tracking-tight xl:text-7xl">
        Result <Money v={r.money.profit_eur} />
      </p>
      {e.avg_charge_price != null && e.avg_discharge_price != null && (
        <p className="max-w-[62ch] text-lg text-ink-2">
          Energy went into the battery at <span className="num">€{e.avg_charge_price.toFixed(2)}/MWh</span> on average and came out at{" "}
          <span className="num">€{e.avg_discharge_price.toFixed(2)}/MWh</span>. The battery's trades came to <Money v={r.money.battery_trades_eur} />.
        </p>
      )}
      <p className="max-w-[70ch] font-serif text-xl leading-relaxed">{r.summary}</p>
      <AiParagraph />
    </div>
  );
}

export function ReportPage() {
  const report = useReport();
  if (report.isPending) return <PanelLoading rows={8} />;
  if (report.isError && !report.data) return <PanelError what="the day report" onRetry={() => report.refetch()} />;
  const r = report.data;
  if (r.intervals === 0) return <p className="font-serif text-2xl text-ink-2">No decisions yet today. Run the simulation and the report fills in.</p>;

  const d = r.decisions;
  return (
    <div className="flex flex-col gap-12">
      <div className="flex flex-wrap items-start justify-between gap-6">
        <Headline r={r} />
        <button
          type="button"
          onClick={() => window.print()}
          className="border border-ink px-4 py-2 text-sm font-semibold hover:bg-ink hover:text-paper print:hidden"
        >
          Print or save as PDF
        </button>
      </div>

      <Section title="How the day went">
        {r.phases.length === 0 ? (
          <p className="text-ink-2">The battery barely moved.</p>
        ) : (
          <ul className="flex flex-col">
            {r.phases.map((p) => (
              <li key={p.start} className="flex flex-wrap items-baseline justify-between gap-x-6 border-t border-rule py-2.5 first:border-t-0">
                <span className={cn("text-lg", p.kind === "charge" ? "text-charge" : "text-discharge")}>{p.text}</span>
                <Money v={p.value_eur} />
              </li>
            ))}
          </ul>
        )}
        {r.energy.avg_charge_price != null && r.energy.avg_discharge_price != null && (
          <p className="text-ink-2">
            On average energy went in at <span className="num">€{r.energy.avg_charge_price.toFixed(2)}/MWh</span> and came out at{" "}
            <span className="num">€{r.energy.avg_discharge_price.toFixed(2)}/MWh</span>. The site sold{" "}
            <span className="num">{r.energy.sold_mwh.toFixed(1)} MWh</span> and bought <span className="num">{r.energy.bought_mwh.toFixed(1)} MWh</span>.
          </p>
        )}
      </Section>

      <Section title="The decisions that mattered most">
        {r.key_decisions.length === 0 ? (
          <p className="text-ink-2">No single decision made a real difference yet.</p>
        ) : (
          <ol>
            {r.key_decisions.map((k) => (
              <DecisionItem key={k.time} d={k} />
            ))}
          </ol>
        )}
      </Section>

      {r.goals.length > 0 && (
        <Section title="Goals">
          <ul>
            {r.goals.map((g) => (
              <li key={g.id} className="flex flex-wrap items-baseline justify-between gap-x-6 border-t border-rule py-2.5 first:border-t-0">
                <span className="font-serif text-xl">{g.text}</span>
                <span className="flex items-baseline gap-3 text-sm text-ink-3">
                  {g.message}
                  <GoalStatusWord status={g.status} />
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Who decided, and where someone stepped in">
        <p className="text-lg text-ink-2">
          Of {d.total} decisions, the AI made <span className="num">{d.ai}</span>, the backup rules <span className="num">{d.backup}</span> and the
          operator <span className="num">{d.operator}</span>. The safety layer adjusted <span className="num">{d.safety_adjusted}</span>.
          {d.ai_notes_dropped > 0 && <> {d.ai_notes_dropped} of the AI's own sentences were hidden because they contradicted its action.</>}
        </p>
        {Object.keys(d.backup_reasons).length > 0 && (
          <p className="text-ink-2">
            Backup rules took over because{" "}
            {Object.entries(d.backup_reasons)
              .map(([why, n]) => `${why} (${n}×)`)
              .join(", ")}
            .
          </p>
        )}
        {r.safety.length > 0 && (
          <div>
            <h3 className="label mb-1">Safety corrections{d.safety_adjusted > r.safety.length && `, the first ${r.safety.length}`}</h3>
            <ul className="text-sm">
              {r.safety.map((s) => (
                <li key={s.time} className="border-t border-rule py-1.5 first:border-t-0">
                  <span className="num mr-3 font-medium">{s.time}</span>
                  {s.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div>
          <h3 className="label mb-1">Operator actions</h3>
          {r.people.length === 0 ? (
            <p className="text-sm text-ink-3">Nobody had to step in.</p>
          ) : (
            <ul className="text-sm">
              {r.people.map((p, i) => (
                <li key={i} className="border-t border-rule py-1.5 first:border-t-0">
                  <span className="num mr-3 font-medium">{p.sim_time ?? "before 00:00"}</span>
                  {p.summary}
                </li>
              ))}
            </ul>
          )}
        </div>
        {r.flags.length > 0 && (
          <div>
            <h3 className="label mb-1">Decisions flagged as wrong</h3>
            <ul className="text-sm">
              {r.flags.map((f) => (
                <li key={f.time} className="border-t border-rule py-1.5 first:border-t-0">
                  <span className="num mr-3 font-medium">{f.time}</span>
                  {f.reason || "no reason given"}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      {(r.forecast.demand_mae_mw != null || r.forecast.production_mae_mw != null) && (
        <Section title="Forecast accuracy">
          <p className="text-lg text-ink-2">
            {r.forecast.demand_mae_mw != null && (
              <>
                Demand was off by <span className="num">{fmtMw(r.forecast.demand_mae_mw)}</span> on average
              </>
            )}
            {r.forecast.demand_mae_mw != null && r.forecast.production_mae_mw != null && ", "}
            {r.forecast.production_mae_mw != null && (
              <>
                solar and wind by <span className="num">{fmtMw(r.forecast.production_mae_mw)}</span>
              </>
            )}
            .{Math.abs(r.money.imbalance_eur) >= 0.5 && <> Not delivering what was promised cost <Money v={r.money.imbalance_eur} />.</>}
          </p>
        </Section>
      )}
    </div>
  );
}
