import { useState } from "react";
import { useSimNextDay, useSimReset, useSimRun, useStateSnapshot } from "@/api/hooks";
import { cn } from "@/lib/utils";

const SPEEDS = [1, 60, 600];

/**
 * Strip along the bottom edge for the hackathon demo: run the simulated day
 * fast, restart it, or move on to the next date once it is over.
 * POST /api/sim/run {speed}; speed 0 pauses. POST /api/sim/next-day {speed}.
 */
export function DemoControls() {
  const state = useStateSnapshot();
  const run = useSimRun();
  const reset = useSimReset();
  const nextDay = useSimNextDay();
  const sim = state.data?.sim ?? { running: false, speed: 0 };
  const [picked, setPicked] = useState(600);
  const speed = sim.running && sim.speed ? sim.speed : picked;
  const busy = run.isPending || reset.isPending || nextDay.isPending;
  const dayOver = !!sim.day_over && !sim.running;

  function pick(s: number) {
    setPicked(s);
    if (sim.running) run.mutate(s);
  }

  const btn = "underline-offset-2 hover:underline disabled:opacity-50";

  return (
    <div className="border-t border-rule-strong bg-wash text-sm" aria-label="Simulation controls">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 px-6 py-2.5 text-base lg:px-12">
        <span className="label">Simulation</span>
        {dayOver && sim.next_day ? (
          <button type="button" className={cn(btn, "font-semibold")} disabled={busy} onClick={() => nextDay.mutate(speed)}>
            {nextDay.isPending ? "Preparing the next day…" : "Run the next day"}
          </button>
        ) : (
          <button type="button" className={cn(btn, "font-semibold")} disabled={busy} onClick={() => run.mutate(sim.running ? 0 : speed)}>
            {sim.running ? "Pause" : dayOver ? "Run the day again" : "Run the rest of the day"}
          </button>
        )}
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
        {nextDay.isError && <span className="text-bad">{nextDay.error.message}</span>}
        <span className="ml-auto hidden text-xs text-ink-3 md:inline">
          {sim.date ? `${sim.date} · ` : ""}
          {sim.running ? `Running at ${sim.speed}×` : dayOver ? "Day finished" : "Paused"} · demo only, not shown to operators
        </span>
      </div>
    </div>
  );
}
