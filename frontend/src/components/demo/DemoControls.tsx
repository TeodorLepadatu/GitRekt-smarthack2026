import { useState } from "react";
import { useLiveDemo, useLiveFetch, useLiveReset, useSimReset, useSimRun, useStateSnapshot } from "@/api/hooks";
import type { LiveDemoStatus } from "@/api/types";
import { cn } from "@/lib/utils";

const SPEEDS = [1, 60, 600];

/**
 * Strip along the bottom edge for the hackathon demo: run the simulated day
 * fast, or restart it. POST /api/sim/run {speed}; speed 0 pauses.
 */
export function DemoControls() {
  const state = useStateSnapshot();
  const run = useSimRun();
  const reset = useSimReset();
  const sim = state.data?.sim ?? { running: false, speed: 0 };
  const [picked, setPicked] = useState(600);
  const speed = sim.running && sim.speed ? sim.speed : picked;
  const busy = run.isPending || reset.isPending;

  function pick(s: number) {
    setPicked(s);
    if (sim.running) run.mutate(s);
  }

  const btn = "underline-offset-2 hover:underline disabled:opacity-50";

  return (
    <div className="fixed inset-x-0 bottom-0 z-30 border-t border-rule-strong bg-wash text-sm" aria-label="Simulation controls">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 px-6 py-2.5 text-base lg:px-12">
        <span className="label">Simulation</span>
        <button type="button" className={cn(btn, "font-semibold")} disabled={busy} onClick={() => run.mutate(sim.running ? 0 : speed)}>
          {sim.running ? "Pause" : "Run the rest of the day"}
        </button>
        <span className="flex items-center gap-2" role="radiogroup" aria-label="Speed">
          <span className="text-ink-3">speed</span>
          {SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              role="radio"
              aria-checked={speed === s}
              disabled={busy}
              onClick={() => pick(s)}
              title={s === 1 ? "Real time: one decision every 15 minutes" : `One decision every ${(900 / s).toFixed(1)} s`}
              className={cn("num px-1", speed === s ? "bg-ink text-paper" : "text-ink-2 hover:text-ink")}
            >
              {s}×
            </button>
          ))}
        </span>
        <button type="button" className={cn(btn, "text-ink-2")} disabled={busy} onClick={() => reset.mutate()}>
          Restart at 00:00
        </button>
        <LiveData />
        <span className="ml-auto hidden text-xs text-ink-3 md:inline">
          {sim.running ? `Running at ${sim.speed}×` : "Paused"} · demo only, not shown to operators
        </span>
      </div>
    </div>
  );
}

const PHASE: Record<LiveDemoStatus["phase"], string> = {
  starting: "Preparing the day's data…",
  idle: "",
  scraping: "Scraping the next hour from Transelectrica and Open-Meteo…",
  training: "The models are learning from the new data…",
  forecasting: "Forecasting the rest of the day again…",
};

/**
 * The live-data demo (backend --live-demo): the day's measured data starts cut
 * off in the morning; each press scrapes the next hour for real, the models
 * learn from it and the rest of the day is forecast again. Hidden when the
 * backend runs without the demo.
 */
function LiveData() {
  const live = useLiveDemo();
  const fetchNext = useLiveFetch();
  const reset = useLiveReset();
  const d = live.data;
  if (!d) return null;

  const busy = d.busy || fetchNext.isPending || reset.isPending;
  const btn = "underline-offset-2 hover:underline disabled:opacity-50";
  const last = d.last;
  const message = d.error
    ? `Failed: ${d.error}`
    : d.busy
      ? PHASE[d.phase]
      : last
        ? `+${last.added} intervals from ${last.source === "transelectrica" ? "Transelectrica" : "ENTSO-E"} (${last.from}–${last.to}) · models updated in ${last.train_seconds} s`
        : "";

  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1" aria-label="Live data demo" aria-live="polite">
      <span className="label">Live data</span>
      <span className="text-ink-2">
        measured until <span className="num font-semibold text-ink">{d.data_until}</span>
      </span>
      <button
        type="button"
        className={cn(btn, "font-semibold")}
        disabled={busy || !d.can_fetch}
        onClick={() => fetchNext.mutate(1)}
        title="Scrape the next hour of this day, update the models with it and forecast the rest of the day again"
      >
        {d.can_fetch ? "Fetch the next hour" : "Day complete"}
      </button>
      {d.steps > 0 && (
        <button type="button" className={cn(btn, "text-ink-2")} disabled={busy} onClick={() => reset.mutate()}>
          Back to the start
        </button>
      )}
      {message && <span className={cn("text-xs", d.error ? "text-bad" : "text-ink-3")}>{message}</span>}
    </span>
  );
}
