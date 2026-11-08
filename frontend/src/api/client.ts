import type {
  AddGoalResponse,
  Alert,
  AuditEntry,
  Autonomy,
  ControlState,
  DayReport,
  DayResponse,
  DecisionFlag,
  Goal,
  IntervalPlan,
  Interval,
  Metrics,
  RelaxGoalResponse,
  ReportSummary,
  StateSnapshot,
  UpcomingResponse,
} from "./types";

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
  simNextDay: (speed: number) => request<unknown>("POST", "/api/sim/next-day", { speed }),

  control: () => request<ControlState>("GET", "/api/control"),
  setOverride: (body: { setpoint_mw: number; until: string }) => request<ControlState>("POST", "/api/override", body),
  clearOverride: () => request<ControlState>("DELETE", "/api/override"),
  setHold: () => request<ControlState>("POST", "/api/hold"),
  releaseHold: () => request<ControlState>("DELETE", "/api/hold"),
  setAutonomy: (body: Partial<Autonomy>) => request<ControlState>("PUT", "/api/autonomy", body),
  answerApproval: ({ id, approve }: { id: number; approve: boolean }) =>
    request<ControlState>("POST", `/api/approvals/${id}`, { approve }),

  // operator desk
  alerts: () => request<Alert[]>("GET", "/api/alerts"),
  ignoreAlert: (id: string) => request<unknown>("POST", `/api/alerts/${encodeURIComponent(id)}/ignore`),
  report: () => request<DayReport>("GET", "/api/report"),
  reportSummary: () => request<ReportSummary>("POST", "/api/report/summary"),
  relaxGoal: (id: string) => request<RelaxGoalResponse>("POST", `/api/goals/${encodeURIComponent(id)}/relax`),
  upcoming: (n = 8) => request<UpcomingResponse>("GET", `/api/upcoming?n=${n}`),
  setPlan: (time: string, battery_fraction: number, note = "") =>
    request<IntervalPlan>("PUT", `/api/plan/${encodeURIComponent(time)}`, { battery_fraction, note }),
  clearPlan: (time: string) => request<unknown>("DELETE", `/api/plan/${encodeURIComponent(time)}`),
  flags: () => request<DecisionFlag[]>("GET", "/api/flags"),
  flag: (time: string, reason: string) => request<DecisionFlag>("POST", `/api/decisions/${encodeURIComponent(time)}/flag`, { reason }),
  unflag: (time: string) => request<unknown>("DELETE", `/api/decisions/${encodeURIComponent(time)}/flag`),
  audit: (limit = 200) => request<AuditEntry[]>("GET", `/api/audit?limit=${limit}`),
};

export type StreamStatus = "connecting" | "live" | "down";

export interface StreamHandlers {
  /** A new interval was decided ("cycle" event). */
  onCycle: (iv: Interval) => void;
  /** The operator control changed: an override, the hold, autonomy or a pending approval ("control" event). */
  onControl: (c: ControlState) => void;
  /** The operator desk raised an alert ("alert" event). */
  onAlert?: (a: Alert) => void;
  onStatus: (s: StreamStatus) => void;
}

/**
 * Subscribe to the SSE stream. Returns an unsubscribe function.
 * EventSource reconnects by itself; onStatus reports the connection state.
 */
export function subscribeStream({ onCycle, onControl, onAlert, onStatus }: StreamHandlers): () => void {
  if (USE_MOCKS) {
    let unsub: (() => void) | null = null;
    let cancelled = false;
    import("@/mocks/server").then(({ mockBackend }) => {
      if (cancelled) return;
      onStatus("live");
      const offCycle = mockBackend.subscribe((iv) => onCycle(structuredClone(iv)));
      const offControl = mockBackend.subscribeControl((c) => onControl(structuredClone(c)));
      const offAlert = mockBackend.subscribeAlerts((a) => onAlert?.(structuredClone(a)));
      unsub = () => {
        offCycle();
        offControl();
        offAlert();
      };
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
  const on = <T,>(event: string, fn: (data: T) => void) =>
    es.addEventListener(event, (ev) => {
      try {
        fn(JSON.parse((ev as MessageEvent).data) as T);
      } catch {
        /* ignore malformed event */
      }
    });
  on("cycle", onCycle);
  on("control", onControl);
  on<Alert>("alert", (a) => onAlert?.(a));
  return () => es.close();
}
