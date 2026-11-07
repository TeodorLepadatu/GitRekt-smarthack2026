import { useState } from "react";
import { ChevronDown, FlaskConical, Pause, Play, RotateCcw } from "lucide-react";
import { useSimReset, useSimRun, useStateSnapshot } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const SPEEDS = [1, 60, 600];

/**
 * Floating dock with the hackathon-demo controls: run the simulated day fast,
 * or reset it. POST /api/sim/run {speed}; speed 0 pauses.
 */
export function DemoControls() {
  const state = useStateSnapshot();
  const run = useSimRun();
  const reset = useSimReset();
  const sim = state.data?.sim ?? { running: false, speed: 0 };
  const [picked, setPicked] = useState(600);
  const [open, setOpen] = useState(false);
  const speed = sim.running && sim.speed ? sim.speed : picked;
  const busy = run.isPending || reset.isPending;
  const now = state.data?.current?.time;

  function pick(s: number) {
    setPicked(s);
    if (sim.running) run.mutate(s);
  }

  return (
    <div
      className="fixed right-4 bottom-4 z-30 flex flex-col overflow-hidden rounded-2xl border bg-card/95 shadow-card backdrop-blur sm:right-6 sm:bottom-6"
      aria-label="Demo controls"
    >
      <div className="flex items-center gap-1 py-1.5 pr-1.5 pl-1">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex flex-1 items-center gap-2 rounded-lg px-3 py-1 text-left text-sm font-semibold hover:bg-accent"
        >
          <FlaskConical className="size-4 text-ai" />
          Demo
          {now && <span className="tabular font-normal text-muted-foreground">· {now}</span>}
          <ChevronDown className={cn("ml-auto size-4 text-muted-foreground transition-transform", !open && "rotate-180")} />
        </button>
        {!open && (
          <Button
            size="icon"
            className="size-8 rounded-lg"
            disabled={busy}
            onClick={() => run.mutate(sim.running ? 0 : speed)}
            aria-label={sim.running ? "Pause simulation" : "Run full day"}
          >
            {sim.running ? <Pause /> : <Play />}
          </Button>
        )}
      </div>
      {open && (
        <div className="flex flex-col gap-2 border-t px-3 pt-3 pb-3">
          <div className="flex gap-2">
            <Button size="sm" className="flex-1 rounded-lg" disabled={busy} onClick={() => run.mutate(sim.running ? 0 : speed)}>
              {sim.running ? <Pause /> : <Play />}
              {sim.running ? "Pause" : "Run full day"}
            </Button>
            <Button size="sm" variant="outline" className="rounded-lg" disabled={busy} onClick={() => reset.mutate()} title="Restart the day at 00:00">
              <RotateCcw /> Reset
            </Button>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Speed</span>
            <div className="flex flex-1 rounded-lg border p-0.5" role="radiogroup" aria-label="Simulation speed">
              {SPEEDS.map((s) => (
                <button
                  key={s}
                  role="radio"
                  aria-checked={speed === s}
                  disabled={busy}
                  onClick={() => pick(s)}
                  title={s === 1 ? "Real time: one decision every 15 minutes" : `One decision every ${(900 / s).toFixed(1)} s`}
                  className={cn(
                    "tabular flex-1 rounded-md px-2 py-1 text-xs font-semibold",
                    speed === s ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {s}×
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
