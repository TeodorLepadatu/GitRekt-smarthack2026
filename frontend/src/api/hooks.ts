import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, subscribeCycles, type StreamStatus } from "./client";
import type { AddGoalResponse, Alert, DayResponse, Goal, StateSnapshot } from "./types";

export const qk = {
  state: ["state"] as const,
  day: ["day"] as const,
  goals: ["goals"] as const,
  metrics: ["metrics"] as const,
  alerts: ["alerts"] as const,
  upcoming: ["upcoming"] as const,
  flags: ["flags"] as const,
  audit: ["audit"] as const,
};

/** Queries that change after every interval, refreshed together with goals and metrics. */
const PER_INTERVAL = [qk.state, qk.goals, qk.metrics, qk.alerts, qk.upcoming, qk.flags, qk.audit];

/** Fallback polling in case the stream drops; the SSE stream does the real-time work. */
const POLL_MS = 30_000;

export const useStateSnapshot = () => useQuery({ queryKey: qk.state, queryFn: api.state, refetchInterval: POLL_MS });
export const useDay = () => useQuery({ queryKey: qk.day, queryFn: api.day, refetchInterval: POLL_MS });
export const useGoals = () => useQuery({ queryKey: qk.goals, queryFn: api.goals, refetchInterval: POLL_MS });
export const useMetrics = () => useQuery({ queryKey: qk.metrics, queryFn: api.metrics, refetchInterval: POLL_MS });

export const useAlerts = () => useQuery({ queryKey: qk.alerts, queryFn: api.alerts, refetchInterval: POLL_MS });
export const useUpcoming = () => useQuery({ queryKey: qk.upcoming, queryFn: () => api.upcoming(8), refetchInterval: POLL_MS });
export const useFlags = () => useQuery({ queryKey: qk.flags, queryFn: api.flags, refetchInterval: POLL_MS });
export const useAudit = () => useQuery({ queryKey: qk.audit, queryFn: () => api.audit(200), refetchInterval: POLL_MS });

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
export const useSetOverride = () =>
  useDeskMutation(
    (v: { time: string; fraction: number; note?: string }) => api.setOverride(v.time, v.fraction, v.note),
    [qk.upcoming],
  );
export const useClearOverride = () => useDeskMutation((time: string) => api.clearOverride(time), [qk.upcoming]);
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

export function useSimReset() {
  const invalidate = useInvalidateAll();
  return useMutation({ mutationFn: api.simReset, onSuccess: invalidate });
}

/**
 * Listen to the SSE "cycle" stream and patch the cached day/state in place,
 * then refresh the aggregates (goals, metrics) from the API.
 */
export function useLiveStream(): StreamStatus {
  const qc = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = subscribeCycles((iv) => {
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
    }, setStatus, (alert) => {
      // A new alert: show it at once; the list itself is refreshed with the interval.
      qc.setQueryData<Alert[]>(qk.alerts, (list) => (list && !list.some((a) => a.id === alert.id) ? [alert, ...list] : list));
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
  const { decision, guard, settlement, soc_mwh, ...rest } = s as Record<string, unknown>;
  return rest as T;
}
