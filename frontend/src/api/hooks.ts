import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, subscribeCycles, type StreamStatus } from "./client";
import type { AddGoalResponse, DayResponse, Goal, StateSnapshot } from "./types";

export const qk = {
  state: ["state"] as const,
  day: ["day"] as const,
  goals: ["goals"] as const,
  metrics: ["metrics"] as const,
};

/** Fallback polling in case the stream drops; the SSE stream does the real-time work. */
const POLL_MS = 30_000;

export const useStateSnapshot = () => useQuery({ queryKey: qk.state, queryFn: api.state, refetchInterval: POLL_MS });
export const useDay = () => useQuery({ queryKey: qk.day, queryFn: api.day, refetchInterval: POLL_MS });
export const useGoals = () => useQuery({ queryKey: qk.goals, queryFn: api.goals, refetchInterval: POLL_MS });
export const useMetrics = () => useQuery({ queryKey: qk.metrics, queryFn: api.metrics, refetchInterval: POLL_MS });

export function useAddGoal() {
  const qc = useQueryClient();
  return useMutation<AddGoalResponse, Error, string>({
    mutationFn: api.addGoal,
    onSuccess: (res) => {
      qc.setQueryData<Goal[]>(qk.goals, res.goals);
      qc.invalidateQueries({ queryKey: qk.metrics });
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
          qc.invalidateQueries({ queryKey: qk.state });
          qc.invalidateQueries({ queryKey: qk.goals });
          qc.invalidateQueries({ queryKey: qk.metrics });
        }, 400);
      }
    }, setStatus);
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
