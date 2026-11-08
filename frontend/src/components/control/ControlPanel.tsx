import { useEffect, useState } from "react";
import { useClearOverride, useControl, useSetAutonomy, useSetOverride, useStateSnapshot } from "@/api/hooks";
import type { AutonomyMode, ControlState } from "@/api/types";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { actionPhrase, autonomyHint, autonomyLabel, fmtMw } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { DURATIONS, nextStart, roundUpToSlot, slotOf, untilAfter } from "./time";

type Kind = "charge" | "hold" | "discharge";
const KINDS: { id: Kind; label: string }[] = [
  { id: "charge", label: "Charge" },
  { id: "hold", label: "Hold" },
  { id: "discharge", label: "Discharge" },
];
const MODES: AutonomyMode[] = ["autonomous", "supervised", "advisory"];
const TIMEOUTS = [30, 60, 120, 300];

const choice = (on: boolean) => cn("px-2.5 py-1", on ? "bg-ink text-paper" : "text-ink-2 hover:text-ink");
const primary = "self-start bg-ink px-5 py-2 text-sm font-semibold text-paper disabled:opacity-40";

/** How long taking control lasts until the operator says otherwise. */
const FIRST_DURATION = "1h";

/**
 * The AI is in control: one button. Pressing it puts the operator in control at
 * once, holding the battery (the safe default) for an hour, and opens the controls.
 */
function TakeControl({ start }: { start: string }) {
  const set = useSetOverride();
  const dayOver = start === "24:00";
  const until = untilAfter(start, DURATIONS.find((d) => d.id === FIRST_DURATION)!.intervals);
  return (
    <div className="flex flex-col gap-3">
      <h3 className="label">Take control</h3>
      <p className="text-base text-ink-2">
        The AI is in control. Take over to set the battery yourself: it holds until you choose what it should do.
      </p>
      <button type="button" className={primary} disabled={dayOver || set.isPending} onClick={() => set.mutate({ setpoint_mw: 0, until })}>
        {set.isPending ? "Taking control…" : "Take control"}
      </button>
      {dayOver && <p className="text-sm text-ink-3">The day is over.</p>}
      {set.isError && <p className="text-sm text-bad">{set.error.message}</p>}
    </div>
  );
}

/**
 * The operator is in control: every change applies at once (after a short pause
 * while the slider moves). The safety layer still checks every quarter-hour.
 */
function InControl({ override, maxMw, start }: { override: NonNullable<ControlState["override"]>; maxMw: number; start: string }) {
  const set = useSetOverride();
  const clear = useClearOverride();
  const sp = override.setpoint_mw;
  const [kind, setKind] = useState<Kind>(sp > 0.005 ? "charge" : sp < -0.005 ? "discharge" : "hold");
  const [mw, setMw] = useState(Math.abs(sp) > 0.005 ? Math.abs(sp) : Math.round(maxMw * 5) / 10);
  // The end is kept as a clock time: a duration counted from "now" would move every quarter-hour.
  const [until, setUntil] = useState(override.until);
  const [picked, setPicked] = useState<string | null>(null);

  const setpoint = kind === "hold" ? 0 : kind === "charge" ? mw : -mw;
  const valid = slotOf(until) > slotOf(start);

  useEffect(() => {
    if (!valid || (Math.abs(setpoint - override.setpoint_mw) < 1e-6 && until === override.until)) return;
    const t = setTimeout(() => set.mutate({ setpoint_mw: setpoint, until }), 400);
    return () => clearTimeout(t);
    // `set` is left out on purpose: it is a new object every render and would restart the pause.
  }, [setpoint, until, valid, override.setpoint_mw, override.until]);

  return (
    <div className="flex flex-col gap-4">
      <h3 className="label">You are in control</h3>
      <p className="font-serif text-2xl leading-snug">
        {actionPhrase(override.setpoint_mw).replace(/^./, (ch) => ch.toUpperCase())} until <span className="num">{override.until}</span>.
      </p>
      <div role="radiogroup" aria-label="Action" className="flex gap-1 text-base">
        {KINDS.map((k) => (
          <button key={k.id} type="button" role="radio" aria-checked={kind === k.id} className={choice(kind === k.id)} onClick={() => setKind(k.id)}>
            {k.label}
          </button>
        ))}
      </div>
      {kind !== "hold" && (
        <label className="flex flex-col gap-1">
          <span className="flex justify-between text-sm text-ink-2">
            Power <span className="num text-ink">{fmtMw(mw)}</span>
          </span>
          <input type="range" min={0.1} max={maxMw} step={0.1} value={mw} onChange={(e) => setMw(Number(e.target.value))} className="accent-ink" />
        </label>
      )}
      <div className="flex flex-col gap-1">
        <span className="text-sm text-ink-2">For</span>
        <div role="radiogroup" aria-label="Duration" className="flex flex-wrap items-center gap-1 text-sm">
          {DURATIONS.map((d) => (
            <button
              key={d.id}
              type="button"
              role="radio"
              aria-checked={picked === d.id}
              className={choice(picked === d.id)}
              onClick={() => {
                setPicked(d.id);
                setUntil(untilAfter(start, d.intervals));
              }}
            >
              {d.label}
            </button>
          ))}
          <button type="button" role="radio" aria-checked={picked === "custom"} className={choice(picked === "custom")} onClick={() => setPicked("custom")}>
            Until…
          </button>
          {picked === "custom" && (
            <input
              type="time"
              step={900}
              value={until === "24:00" ? "23:45" : until}
              onChange={(e) => e.target.value && setUntil(roundUpToSlot(e.target.value))}
              aria-label="Until"
              className="num border border-rule-strong bg-sheet px-2 py-0.5"
            />
          )}
        </div>
      </div>
      <p className="text-sm text-ink-3">
        {valid
          ? "The AI is not asked meanwhile. Battery limits and hard goals can still trim the setting."
          : `Pick a time after ${start}.`}
      </p>
      <button type="button" className={primary} disabled={clear.isPending} onClick={() => clear.mutate()}>
        Hand back to the AI
      </button>
      {(set.isError || clear.isError) && <p className="text-sm text-bad">{(set.error ?? clear.error)?.message}</p>}
    </div>
  );
}

/** How far the AI is trusted. */
function AutonomyPicker({ c }: { c: ControlState }) {
  const set = useSetAutonomy();
  const a = c.autonomy;
  const [mwDraft, setMwDraft] = useState(String(a.max_auto_mw));

  function commitMw() {
    const v = Number(mwDraft);
    if (Number.isFinite(v) && v >= 0 && v !== a.max_auto_mw) set.mutate({ max_auto_mw: v });
    else setMwDraft(String(a.max_auto_mw));
  }

  return (
    <div className="flex flex-col gap-4">
      <h3 className="label">How much the AI may do</h3>
      <div role="radiogroup" aria-label="Autonomy" className="flex flex-col gap-3">
        {MODES.map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={a.mode === m}
            disabled={set.isPending}
            onClick={() => a.mode !== m && set.mutate({ mode: m })}
            className={cn("flex flex-col border-l-2 pl-3 text-left", a.mode === m ? "border-ink" : "border-rule hover:border-ink-3")}
          >
            <span className={cn("text-base font-semibold", a.mode !== m && "text-ink-2")}>{autonomyLabel[m]}</span>
            <span className="text-sm text-ink-3">{autonomyHint[m]}</span>
          </button>
        ))}
      </div>
      {a.mode === "supervised" && (
        <div className="flex flex-col gap-2 text-sm">
          <label className="flex items-center gap-2">
            Ask before moves above
            <input
              type="number"
              min={0}
              step={0.5}
              value={mwDraft}
              onChange={(e) => setMwDraft(e.target.value)}
              onBlur={commitMw}
              onKeyDown={(e) => e.key === "Enter" && commitMw()}
              className="num w-16 border border-rule-strong bg-sheet px-2 py-0.5"
            />
            MW
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={a.ask_on_low_confidence}
              onChange={(e) => set.mutate({ ask_on_low_confidence: e.target.checked })}
              className="accent-ink"
            />
            Also ask when the AI is unsure or the backup rules decide
          </label>
        </div>
      )}
      {a.mode !== "autonomous" && (
        <label className="flex flex-wrap items-center gap-2 text-sm">
          If you don't answer within
          <select
            value={a.timeout_s}
            onChange={(e) => set.mutate({ timeout_s: Number(e.target.value) })}
            className="num border border-rule-strong bg-sheet px-1 py-0.5"
          >
            {TIMEOUTS.map((s) => (
              <option key={s} value={s}>
                {s < 60 ? `${s} s` : `${s / 60} min`}
              </option>
            ))}
          </select>
          the battery holds.
        </label>
      )}
      {set.isError && <p className="text-sm text-bad">{set.error.message}</p>}
    </div>
  );
}

/** The operator's controls on the Now page. Emergency hold and approvals live in the strip at the bottom. */
export function ControlPanel() {
  const control = useControl();
  const state = useStateSnapshot();
  if (control.isPending) return <PanelLoading rows={4} />;
  if (control.isError && !control.data) return <PanelError what="the operator controls" onRetry={() => control.refetch()} />;

  const start = nextStart(state.data?.current?.time ?? null);
  const maxMw = state.data?.max_power_mw ?? 3;
  return (
    <div className="grid gap-x-14 gap-y-10 lg:grid-cols-2">
      {control.data.override ? (
        <InControl override={control.data.override} maxMw={maxMw} start={start} />
      ) : (
        <TakeControl start={start} />
      )}
      <AutonomyPicker c={control.data} />
    </div>
  );
}
