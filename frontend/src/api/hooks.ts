import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, subscribeStream, type StreamStatus } from "./client";
import type { AddGoalResponse, Alert, ControlState, DayResponse, Goal, Interval, StateSnapshot } from "./types";

export const qk = {
  state: ["state"] as const,
  day: ["day"] as const,
  goals: ["goals"] as const,
  metrics: ["metrics"] as const,
  alerts: ["alerts"] as const,
  upcoming: ["upcoming"] as const,
  flags: ["flags"] as const,
  audit: ["audit"] as const,
  control: ["control"] as const,
  report: ["report"] as const,
};

/** Queries that change after every interval, refreshed together with goals and metrics. */
const PER_INTERVAL = [qk.state, qk.goals, qk.metrics, qk.alerts, qk.upcoming, qk.flags, qk.audit, qk.report];

/** Fallback polling in case the stream drops; the SSE stream does the real-time work. */
const POLL_MS = 30_000;

export const useStateSnapshot = () => useQuery({ queryKey: qk.state, queryFn: api.state, refetchInterval: POLL_MS });
export const useDay = () => useQuery({ queryKey: qk.day, queryFn: api.day, refetchInterval: POLL_MS });
export const useGoals = () => useQuery({ queryKey: qk.goals, queryFn: api.goals, refetchInterval: POLL_MS });
export const useMetrics = () => useQuery({ queryKey: qk.metrics, queryFn: api.metrics, refetchInterval: POLL_MS });
export const useControl = () => useQuery({ queryKey: qk.control, queryFn: api.control, refetchInterval: POLL_MS });

/**
 * One mutation per operator command. Every command answers with the new
 * ControlState, which replaces the cached one at once.
 */
function useControlCommand<V>(fn: (v: V) => Promise<ControlState>) {
  const qc = useQueryClient();
  return useMutation<ControlState, Error, V>({
    mutationFn: fn,
    onSuccess: (c) => {
      qc.setQueryData(qk.control, c);
      // the command is in the audit log, and a standing override changes what is planned
      qc.invalidateQueries({ queryKey: qk.audit });
      qc.invalidateQueries({ queryKey: qk.upcoming });
    },
  });
}

export const useSetOverride = () => useControlCommand(api.setOverride);
export const useClearOverride = () => useControlCommand<void>(api.clearOverride);
export const useSetHold = () => useControlCommand<boolean>((on) => (on ? api.setHold() : api.releaseHold()));
export const useSetAutonomy = () => useControlCommand(api.setAutonomy);
export const useAnswerApproval = () => useControlCommand(api.answerApproval);

export const useAlerts = () => useQuery({ queryKey: qk.alerts, queryFn: api.alerts, refetchInterval: POLL_MS });
export const useUpcoming = () => useQuery({ queryKey: qk.upcoming, queryFn: () => api.upcoming(8), refetchInterval: POLL_MS });
export const useFlags = () => useQuery({ queryKey: qk.flags, queryFn: api.flags, refetchInterval: POLL_MS });
export const useAudit = () => useQuery({ queryKey: qk.audit, queryFn: () => api.audit(200), refetchInterval: POLL_MS });
export const useReport = () => useQuery({ queryKey: qk.report, queryFn: api.report, refetchInterval: POLL_MS });
/** The report's closing paragraph written by the AI (one LLM call: takes a while). */
export const useReportSummary = () => useMutation({ mutationFn: api.reportSummary });

/** A mutation that refreshes the operator desk (and anything else listed) once it is done. */
function useDeskMutation<V, R>(fn: (v: V) => Promise<R>, keys: readonly (readonly string[])[]) {
  const qc = useQueryClient();
  return useMutation<R, Error, V>({
    mutationFn: fn,
    onSettled: () => [...keys, qk.audit].forEach((k) => qc.invalidateQueries({ queryKey: k })),
  });
}

export const useIgnoreAlert = () => useDeskMutation((id: string) => api.ignoreAlert(id), [qk.alerts]);
export const useRelaxGoal = () => useDeskMutation((id: string) => api.relaxGoal(id), [qk.alerts, qk.goals, qk.upcoming]);
/** One interval set ahead of time on the Day tab ("Skip" / "Change"). */
export const useSetPlan = () =>
  useDeskMutation(
    (v: { time: string; fraction: number; note?: string }) => api.setPlan(v.time, v.fraction, v.note),
    [qk.upcoming],
  );
export const useClearPlan = () => useDeskMutation((time: string) => api.clearPlan(time), [qk.upcoming]);
export const useFlag = () => useDeskMutation((v: { time: string; reason: string }) => api.flag(v.time, v.reason), [qk.flags]);
export const useUnflag = () => useDeskMutation((time: string) => api.unflag(time), [qk.flags]);

export function useAddGoal() {
  const qc = useQueryClient();
  return useMutation<AddGoalResponse, Error, string>({
    mutationFn: api.addGoal,
    onSuccess: (res) => {
      qc.setQueryData<Goal[]>(qk.goals, res.goals);
      qc.invalidateQueries({ queryKey: qk.metrics });
      qc.invalidateQueries({ queryKey: qk.audit });
    },
  });
}

export function useDeleteGoal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.deleteGoal,
    onMutate: async (id: string) => {
      await qc.cancelQueries({ queryKey: qk.goals });
      const prev = qc.getQueryData<Goal[]>(qk.goals);
      qc.setQueryData<Goal[]>(qk.goals, (g) => g?.filter((x) => x.id !== id));
      return { prev };
    },
    onError: (_e, _id, ctx) => ctx?.prev && qc.setQueryData(qk.goals, ctx.prev),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.goals });
      qc.invalidateQueries({ queryKey: qk.metrics });
      qc.invalidateQueries({ queryKey: qk.audit });
    },
  });
}

function useInvalidateAll() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries();
}

export function useSimRun() {
  const invalidate = useInvalidateAll();
  return useMutation({ mutationFn: api.simRun, onSuccess: invalidate });
}

export function useSimNextDay() {
  const invalidate = useInvalidateAll();
  return useMutation({ mutationFn: api.simNextDay, onSuccess: invalidate });
}

export function useSimReset() {
  const invalidate = useInvalidateAll();
  return useMutation({ mutationFn: api.simReset, onSuccess: invalidate });
}

/**
 * Listen to the SSE stream: "cycle" patches the cached day/state in place and
 * then refreshes the aggregates (goals, metrics); "control" replaces the
 * cached ControlState, so a proposal waiting for approval shows at once.
 */
export function useLiveStream(): StreamStatus {
  const qc = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    const onCycle = (iv: Interval) => {
      qc.setQueryData<DayResponse>(qk.day, (day) => {
        if (!day) return day;
        const intervals = day.intervals.map((s) => (s.time === iv.time ? iv : s));
        // A cycle at 00:00 means a new day/reset: drop decisions after it.
        const startOfDay = iv.time === "00:00";
        return {
          ...day,
          now: iv.time,
          intervals: startOfDay ? intervals.map((s, i) => (i === 0 ? iv : stripDecision(s))) : intervals,
        };
      });
      qc.setQueryData<StateSnapshot>(qk.state, (s) => (s ? { ...s, current: iv } : s));
      // Coalesce refreshes when the simulation runs fast.
      if (!refreshTimer) {
        refreshTimer = setTimeout(() => {
          refreshTimer = null;
          PER_INTERVAL.forEach((k) => qc.invalidateQueries({ queryKey: k }));
        }, 400);
      }
    };
    const unsubscribe = subscribeStream({
      onCycle,
      onControl: (c) => qc.setQueryData(qk.control, c),
      // A new alert: show it at once; the list itself is refreshed with the interval.
      onAlert: (alert) =>
        qc.setQueryData<Alert[]>(qk.alerts, (list) => (list && !list.some((a) => a.id === alert.id) ? [alert, ...list] : list)),
      onStatus: setStatus,
    });
    return () => {
      unsubscribe();
      if (refreshTimer) clearTimeout(refreshTimer);
    };
  }, [qc]);

  return status;
}

function stripDecision<T extends object>(s: T) {
  if (!("decision" in s)) return s;
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { decision, guard, settlement, soc_mwh, control, ...rest } = s as Record<string, unknown>;
  return rest as T;
}
