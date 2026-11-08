/**
 * A rough TypeScript stand-in for the backend loop
 * (forecast -> "LLM" proposal -> validator/fallback -> guard -> battery),
 * only good enough to produce believable data for the dashboard.
 */
import type { Confidence, Decision, ForecastInterval, Goal, GuardResult, Interval } from "@/api/types";
import { indexToTime, INTERVAL_H, timeToIndex } from "@/lib/utils";
import { settle } from "./settlement";

export const CAPACITY = 10;
export const P_MAX = 3;
export const ETA = Math.sqrt(0.9);
const BUFFER_MIN = 30;
const EPS = 1e-6;
/** Below this price, storing surplus beats exporting it. */
const CHEAP = 60;

const r2 = (v: number) => Math.round(v * 100) / 100;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export const maxCharge = (soc: number) => Math.min(P_MAX, Math.max(0, CAPACITY - soc) / (ETA * INTERVAL_H));
export const maxDischarge = (soc: number, floor = 0) =>
  Math.min(P_MAX, (Math.max(0, soc - floor) * ETA) / INTERVAL_H);
export const socAfter = (soc: number, sp: number) =>
  sp >= 0 ? soc + ETA * sp * INTERVAL_H : soc + (sp * INTERVAL_H) / ETA;

const toMin = (t: string) => timeToIndex(t) * 15;
function inWindow(now: number, start: number, end: number) {
  return start <= end ? now >= start && now < end : now >= start || now < end;
}

/** Interval indices where the mock "LLM" fails, with the validator's reason. */
const LLM_FAILURES: Record<number, string> = {
  9: "timeout",
  27: "bad_json",
  41: "timeout",
  58: "missing_field",
  59: "llm_error",
  60: "llm_error", // three in a row: the operator desk raises an alert
  74: "timeout",
  89: "out_of_range",
};

interface Proposal {
  fraction: number;
  curtail: boolean;
  explanation: string;
  reasoning: string;
  confidence: Confidence;
}

function reserveGoals(goals: Goal[]) {
  return goals.filter((g) => g.type === "reserve" && g.mwh != null && g.by);
}

/** Open reserve goal (deadline still ahead) that needs energy, the most urgent first. */
function openReserve(goals: Goal[], soc: number, startMin: number) {
  let best: { goal: Goal; need: number; hoursLeft: number } | null = null;
  for (const g of reserveGoals(goals)) {
    const by = toMin(g.by!);
    if (by <= startMin) continue;
    const need = g.mwh! - soc;
    if (need <= 0.01) continue;
    const hoursLeft = (by - startMin) / 60;
    if (!best || hoursLeft < best.hoursLeft) best = { goal: g, need, hoursLeft };
  }
  return best;
}

function proposeLlm(f: ForecastInterval, soc: number, goals: Goal[], startMin: number): Proposal {
  const net = f.production_mw - f.demand_mw;
  const p = f.price_eur_mwh;
  const gap = Math.abs(f.break_even - p);
  const confidence: Confidence = gap > 60 ? "high" : gap > 20 ? "medium" : "low";
  const open = openReserve(goals, soc, startMin);
  const surplusTxt = net >= 0 ? `a surplus of ${net.toFixed(2)} MW` : `a deficit of ${(-net).toFixed(2)} MW`;

  if (p < 0) {
    return {
      fraction: 1,
      curtail: net > P_MAX,
      confidence: "high",
      explanation: `Charging at full power: the grid pays us to take power (${p.toFixed(0)} €/MWh).`,
      reasoning: `The price is negative (€${p.toFixed(1)}/MWh), so every MWh stored earns money. The site has ${surplusTxt}. Charging at the 3 MW maximum; any surplus above that should be switched off rather than exported at a loss.`,
    };
  }
  if (p >= f.break_even) {
    return {
      fraction: -1,
      curtail: false,
      confidence,
      explanation: `Selling at full power while the price is above break-even (${p.toFixed(0)} €/MWh).`,
      reasoning: `The price is €${(p - f.break_even).toFixed(1)} above the break-even of €${f.break_even.toFixed(1)}/MWh, so discharging is profitable. The site has ${surplusTxt}. Discharging at full power; the safety layer keeps any reserve goals intact.`,
    };
  }
  // Plan to reach reserve goals at half power, so cheap hours are used first.
  if (open && open.hoursLeft <= open.need / (0.5 * P_MAX * ETA) + 0.25) {
    const pace = open.need / (open.hoursLeft * ETA);
    const mw = clamp(Math.max(pace, net), 0, P_MAX);
    return {
      fraction: mw / P_MAX,
      curtail: false,
      confidence,
      explanation: `Charging ${mw.toFixed(2)} MW to reach the ${open.goal.mwh!.toFixed(0)} MWh goal by ${open.goal.by}.`,
      reasoning: `The price is €${(f.break_even - p).toFixed(1)} below break-even, so selling now is not worth it. Goal "${open.goal.source_text}" still needs ${open.need.toFixed(2)} MWh in ${open.hoursLeft.toFixed(2)} h; an even pace of ${pace.toFixed(2)} MW keeps it on track without buying too much at once.`,
    };
  }
  if (net > 0.05 && p < CHEAP) {
    const mw = Math.min(net, P_MAX);
    return {
      fraction: mw / P_MAX,
      curtail: false,
      confidence,
      explanation: `Storing ${mw.toFixed(2)} MW of solar/wind surplus for the evening peak.`,
      reasoning: `The site has ${surplusTxt} and the price (€${p.toFixed(1)}/MWh) is well below break-even. Storing the surplus is worth more than exporting it now, because the evening price forecast is higher.`,
    };
  }
  return {
    fraction: 0,
    curtail: false,
    confidence,
    explanation:
      net > 0.05
        ? "Holding: selling the surplus directly pays more than storing it now."
        : "Holding: the price is not good enough to buy or to sell.",
    reasoning: `The price is €${p.toFixed(1)}/MWh, below break-even (€${f.break_even.toFixed(1)}) but not cheap enough to charge from the grid. The site has ${surplusTxt}, ${net > 0 ? "which is exported directly" : "covered by the grid"}. Keeping the stored energy for the evening peak.`,
  };
}

/** Same simple rules as validator/fallback.py. */
function proposeFallback(f: ForecastInterval, goals: Goal[], soc: number, startMin: number): Proposal {
  const net = f.production_mw - f.demand_mw;
  let fraction: number;
  let explanation: string;
  if (f.price_eur_mwh < 0) {
    fraction = 1;
    explanation = "Backup rules: charging at full power because the price is negative.";
  } else if (f.price_eur_mwh >= f.break_even) {
    fraction = -1;
    explanation = "Backup rules: selling at full power because the price is above break-even.";
  } else {
    fraction = clamp(net / P_MAX, -1, 1);
    explanation =
      fraction > 0
        ? "Backup rules: storing the site's surplus."
        : fraction < 0
          ? "Backup rules: covering the site's own demand from the battery."
          : "Backup rules: holding.";
  }
  const open = openReserve(goals, soc, startMin);
  if (open && open.goal.hard) {
    const even = open.need / (open.hoursLeft * ETA) / P_MAX;
    if (fraction < even) {
      fraction = clamp(even, -1, 1);
      explanation = `Backup rules: charging ${(fraction * P_MAX).toFixed(2)} MW to keep "${open.goal.source_text}" on track.`;
    }
  }
  return { fraction, curtail: f.price_eur_mwh < 0 && net > P_MAX, confidence: "low", explanation, reasoning: "" };
}

/** Mirrors guard/guard.py closely enough for realistic applied_rules. */
function runGuard(dec: Decision, f: ForecastInterval, soc: number, goals: Goal[], startMin: number) {
  const rules: string[] = [];
  const proposed = dec.battery_fraction * P_MAX;
  let hi = maxCharge(soc);
  let lo = -maxDischarge(soc);
  let hiRule = "soc_max_limit";
  let loRule = "soc_min_limit";
  let forceCharge = false;
  let forceRule = "";
  let blockExport = false;
  let blockExportRule = "";
  const endMin = startMin + 15;
  const hard = goals.filter((g) => g.hard);

  for (const g of hard) {
    if (g.type === "reserve" && g.mwh != null && g.by) {
      const by = toMin(g.by);
      let until = g.until ? toMin(g.until) : 24 * 60;
      if (until === by) until = 24 * 60;
      if (inWindow(startMin, by, until)) {
        const floorLo = -maxDischarge(soc, g.mwh);
        if (floorLo > lo + EPS) {
          lo = floorLo;
          loRule = `goal_${g.id}_blocked_discharge`;
        } else rules.push(`goal_${g.id}_ok`);
      } else if (by > startMin) {
        // latest-start rule: after this interval, does full power + buffer still fit?
        const afterProposed = socAfter(soc, clamp(proposed, lo, hi));
        const needAfter = g.mwh - afterProposed;
        const minutesLeftAfter = by - endMin;
        const needMin = (needAfter / (ETA * P_MAX)) * 60;
        if (needAfter > EPS && minutesLeftAfter < needMin + BUFFER_MIN) {
          forceCharge = true;
          forceRule = `goal_${g.id}_forced_charge`;
          const best = socAfter(soc, hi) + (ETA * P_MAX * Math.max(0, minutesLeftAfter)) / 60;
          if (best < g.mwh - EPS) rules.push(`goal_${g.id}_unreachable`);
        } else {
          rules.push(`goal_${g.id}_ok`);
          if (needAfter > EPS && proposed < 0) {
            // discharging would make the goal tighter than the latest-start rule allows
            const maxD = Math.max(0, ((by - endMin) / 60 - BUFFER_MIN / 60) * ETA * P_MAX - (g.mwh - soc));
            const allowed = -Math.min(maxDischarge(soc), (maxD * ETA) / INTERVAL_H);
            if (allowed > lo) {
              lo = allowed;
              loRule = `goal_${g.id}_blocked_discharge`;
            }
          }
        }
      }
    } else if (g.type === "min_sell_price" && g.eur_per_mwh != null) {
      if (f.price_eur_mwh < g.eur_per_mwh) {
        blockExport = true;
        blockExportRule = `goal_${g.id}_blocked_export`;
      } else rules.push(`goal_${g.id}_ok`);
    }
  }

  let sp = proposed;
  if (forceCharge) {
    sp = hi;
    rules.push(forceRule);
  }
  if (sp > hi + EPS) {
    sp = hi;
    if (!forceCharge) rules.push(hiRule);
  } else if (sp < lo - EPS) {
    sp = lo;
    rules.push(loRule);
  } else if (!rules.some((r) => r.startsWith("soc_"))) {
    rules.unshift("soc_limits_ok");
  }

  // Curtailment and "never sell below" handling.
  const surplus = f.production_mw - f.demand_mw;
  let curtail = 0;
  if (blockExport) {
    // no export: discharge at most the deficit, store the surplus, curtail the rest
    if (sp < 0) sp = Math.max(sp, Math.min(0, surplus));
    const exportLeft = surplus - sp;
    if (exportLeft > EPS) {
      const extra = Math.min(hi - sp, exportLeft);
      if (extra > EPS) sp += extra;
      const rest = surplus - sp;
      if (rest > EPS) {
        curtail = rest;
        rules.push("curtail_forced_min_sell");
      }
      rules.push(blockExportRule);
    }
  } else if (dec.curtail) {
    const rest = surplus - Math.max(sp, 0);
    if (f.price_eur_mwh >= 0) rules.push("curtail_blocked_price_not_negative");
    else if (rest <= EPS) rules.push("curtail_blocked_no_surplus");
    else {
      curtail = rest;
      rules.push("curtail_applied");
      if (sp < 0) {
        sp = 0;
        rules.push("no_discharge_while_curtailing");
      }
    }
  }
  curtail = Math.min(curtail, Math.max(0, f.production_mw));

  const guard: GuardResult = {
    battery_setpoint_mw: r2(sp),
    curtail_mw: r2(curtail),
    clipped: Math.abs(sp - proposed) > 0.01 || (curtail > EPS) !== dec.curtail,
    applied_rules: rules,
  };
  return guard;
}

/** An operator override for one interval (see backend/desk.py). */
export interface MockOverride {
  battery_fraction: number;
  note: string;
}

function operatorDecision(o: MockOverride): Decision {
  const f = clamp(o.battery_fraction, -1, 1);
  const action =
    f > 0 ? `charge at ${Math.round(f * 100)}% of max power` : f < 0 ? `discharge at ${Math.round(-f * 100)}% of max power` : "hold the battery";
  return {
    battery_fraction: r2(f),
    curtail: false,
    source: "operator",
    confidence: "high",
    reasoning: `The operator set this interval ahead of time: ${action}.${o.note ? ` Note: ${o.note}` : ""}`,
    explanation_for_operator: `Operator override: ${action}.`,
    fallback_reason: null,
  };
}

/** Run one 15-minute cycle starting with `soc` stored. An override skips the "LLM", never the guard. */
export function runCycle(i: number, f: ForecastInterval, soc: number, goals: Goal[], override?: MockOverride | null): Interval {
  const startMin = i * 15;
  const failure = LLM_FAILURES[i];
  if (override) {
    // an interval set ahead of time on the Day tab: the AI is not asked, the guard still checks it
    const decision = operatorDecision(override);
    return finish(i, f, soc, decision, runGuard(decision, f, soc, goals, startMin));
  }
  const p = failure ? proposeFallback(f, goals, soc, startMin) : proposeLlm(f, soc, goals, startMin);
  const decision: Decision = {
    battery_fraction: r2(p.fraction),
    curtail: p.curtail,
    source: failure ? "fallback" : "llm",
    confidence: p.confidence,
    reasoning: p.reasoning,
    explanation_for_operator: p.explanation,
    fallback_reason: failure ?? null,
  };
  return finish(i, f, soc, decision, runGuard(decision, f, soc, goals, startMin));
}

/**
 * What the operator can make an interval do instead of the AI (loop/day.py):
 * override  their setpoint, through the guard
 * hold      emergency hold: idle, the surplus switched off, the guard skipped
 * reject    the AI's proposal was rejected: hold, through the guard
 * timeout   nobody answered the proposal: hold, through the guard
 */
export type MockCommand =
  | { kind: "override"; setpoint_mw: number; until: string }
  | { kind: "hold" }
  | { kind: "reject"; proposed_mw: number }
  | { kind: "timeout"; proposed_mw: number };

const verb = (mw: number) => (mw > 0.005 ? `charge ${mw.toFixed(2)} MW` : mw < -0.005 ? `discharge ${(-mw).toFixed(2)} MW` : "hold");

export function runCommand(i: number, f: ForecastInterval, soc: number, goals: Goal[], cmd: MockCommand): Interval {
  const operator = (fraction: number, explanation: string): Decision => ({
    battery_fraction: fraction,
    curtail: false,
    source: "operator",
    confidence: "high",
    reasoning: "",
    explanation_for_operator: explanation,
    fallback_reason: null,
  });
  if (cmd.kind === "hold") {
    const decision = operator(0, "Emergency hold: the battery is idle and nothing is exported until the operator releases it.");
    const guard: GuardResult = {
      battery_setpoint_mw: 0,
      curtail_mw: r2(Math.max(0, f.production_mw - f.demand_mw)),
      clipped: false,
      applied_rules: ["emergency_hold"],
    };
    return finish(i, f, soc, decision, guard);
  }
  let decision: Decision;
  if (cmd.kind === "override") {
    decision = operator(clamp(cmd.setpoint_mw / P_MAX, -1, 1), `Operator override: ${verb(cmd.setpoint_mw)} until ${cmd.until}.`);
  } else if (cmd.kind === "reject") {
    decision = operator(0, `The operator rejected the AI's proposal to ${verb(cmd.proposed_mw)}: battery held.`);
  } else {
    decision = {
      ...operator(0, `No answer to the AI's proposal to ${verb(cmd.proposed_mw)} in time: battery held.`),
      source: "fallback",
      confidence: "low",
      fallback_reason: "approval_timeout",
    };
  }
  return finish(i, f, soc, decision, runGuard(decision, f, soc, goals, i * 15));
}

/** The interval once the battery has run the guard's setpoint. `control` is filled in by the caller. */
function finish(i: number, f: ForecastInterval, soc: number, decision: Decision, guard: GuardResult): Interval {
  const sp = guard.battery_setpoint_mw;
  return {
    ...f,
    time: indexToTime(i),
    soc_mwh: r2(clamp(socAfter(soc, sp), 0, CAPACITY)),
    decision,
    guard,
    // Same math as settlement/settle.py (forecast = actual in the mock).
    settlement: settle(f.price_eur_mwh, f.demand_mw, f.production_mw, sp, guard.curtail_mw),
    control: { action: "auto", mode: "autonomous", proposed_mw: null },
  };
}

/** The backup rules' plan for an interval that has not run yet, as the Day tab previews it. */
export function previewCycle(i: number, f: ForecastInterval, soc: number, goals: Goal[], override?: MockOverride | null) {
  const startMin = i * 15;
  const p = proposeFallback(f, goals, soc, startMin);
  const planned: Decision = {
    battery_fraction: r2(p.fraction),
    curtail: p.curtail,
    source: "fallback",
    confidence: "low",
    reasoning: "",
    explanation_for_operator: p.explanation.replace(/^Backup rules: /, ""),
    fallback_reason: "preview",
  };
  const plannedGuard = runGuard(planned, f, soc, goals, startMin);
  const overGuard = override ? runGuard(operatorDecision(override), f, soc, goals, startMin) : null;
  const sp = (overGuard ?? plannedGuard).battery_setpoint_mw;
  return {
    planned: {
      battery_fraction: planned.battery_fraction,
      setpoint_mw: plannedGuard.battery_setpoint_mw,
      explanation: planned.explanation_for_operator.charAt(0).toUpperCase() + planned.explanation_for_operator.slice(1),
    },
    override: overGuard ? { setpoint_mw: overGuard.battery_setpoint_mw, limited_by_safety: overGuard.clipped } : null,
    socAfter: clamp(socAfter(soc, sp), 0, CAPACITY),
  };
}
