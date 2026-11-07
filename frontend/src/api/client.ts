import type { AddGoalResponse, DayResponse, Goal, Interval, Metrics, StateSnapshot } from "./types";

export const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === "true";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (USE_MOCKS) {
    const { mockBackend, MockHttpError } = await import("@/mocks/server");
    await new Promise((r) => setTimeout(r, 120)); // feel like a network call
    try {
      // structuredClone: callers must never share objects with the mock's state
      return structuredClone(mockBackend.handle(method, path, body)) as T;
    } catch (e) {
      if (e instanceof MockHttpError) throw new ApiError(e.status, e.message);
      throw e;
    }
  }
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the energy system");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j?.detail === "string" ? j.detail : detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  state: () => request<StateSnapshot>("GET", "/api/state"),
  day: () => request<DayResponse>("GET", "/api/day"),
  goals: () => request<Goal[]>("GET", "/api/goals"),
  addGoal: (text: string) => request<AddGoalResponse>("POST", "/api/goals", { text }),
  deleteGoal: (id: string) => request<unknown>("DELETE", `/api/goals/${encodeURIComponent(id)}`),
  metrics: () => request<Metrics>("GET", "/api/metrics"),
  simRun: (speed: number) => request<unknown>("POST", "/api/sim/run", { speed }),
  simReset: () => request<unknown>("POST", "/api/sim/reset"),
};

export type StreamStatus = "connecting" | "live" | "down";

/**
 * Subscribe to the "cycle" SSE stream. Returns an unsubscribe function.
 * EventSource reconnects by itself; onStatus reports the connection state.
 */
export function subscribeCycles(onCycle: (iv: Interval) => void, onStatus: (s: StreamStatus) => void): () => void {
  if (USE_MOCKS) {
    let unsub: (() => void) | null = null;
    let cancelled = false;
    import("@/mocks/server").then(({ mockBackend }) => {
      if (cancelled) return;
      onStatus("live");
      const off = mockBackend.subscribe((iv) => onCycle(structuredClone(iv)));
      unsub = () => void off();
    });
    return () => {
      cancelled = true;
      unsub?.();
    };
  }
  onStatus("connecting");
  const es = new EventSource("/api/stream");
  es.onopen = () => onStatus("live");
  es.onerror = () => onStatus(es.readyState === EventSource.CLOSED ? "down" : "connecting");
  es.addEventListener("cycle", (ev) => {
    try {
      onCycle(JSON.parse((ev as MessageEvent).data) as Interval);
    } catch {
      /* ignore malformed event */
    }
  });
  return () => es.close();
}
