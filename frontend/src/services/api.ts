import type {
  AnalysisSettings,
  Comparison,
  EventView,
  Health,
  MergedLockOrder,
  Scenario,
  SessionInfo,
  Snapshot,
  Timeline,
} from "@/types/api";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  } catch {
    throw new ApiError(0, "Backend unreachable — is scripts/run_backend.sh running?");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const qs = (params: Record<string, string | number | boolean | undefined | null>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export interface EventPage {
  events: EventView[];
  total: number | null;
  has_more: boolean;
  next_cursor: number | null;
  type_names: string[];
}

export interface EventQuery {
  after_seq?: number;
  before_seq?: number;
  limit?: number;
  types?: string;
  threads?: string;
  locks?: string;
  from_ns?: number;
  to_ns?: number;
  q?: string;
  order?: "asc" | "desc";
}

export const api = {
  health: () => request<Health>("/api/health"),
  scenarios: () => request<Scenario[]>("/api/scenarios"),
  scenarioSource: (id: string) =>
    request<{ scenario: string; files: { path: string; name: string; content: string }[] }>(
      `/api/scenarios/${encodeURIComponent(id)}/source`,
    ),
  sessions: (limit = 100) => request<SessionInfo[]>(`/api/sessions${qs({ limit })}`),
  session: (id: string) => request<SessionInfo>(`/api/sessions/${id}`),
  createSession: (body: {
    scenario_id?: string;
    executable?: string;
    args?: string[];
    timeout_s?: number;
    label?: string;
    kill_on_deadlock?: boolean;
    ring_capacity?: number;
    block_us?: number;
  }) => request<SessionInfo>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  stopSession: (id: string) => request<SessionInfo>(`/api/sessions/${id}/stop`, { method: "POST" }),
  deleteSession: (id: string) => request<void>(`/api/sessions/${id}`, { method: "DELETE" }),
  state: (id: string) => request<Snapshot>(`/api/sessions/${id}/state`),
  events: (id: string, q: EventQuery) => request<EventPage>(`/api/sessions/${id}/events${qs({ ...q })}`),
  event: (id: string, seq: number) =>
    request<{
      event: EventView;
      related: (EventView & { relation: string })[];
      site: { function: string | null; file: string | null; line: number | null; path: string | null } | null;
      thread_prev: EventView | null;
      thread_next: EventView | null;
    }>(`/api/sessions/${id}/events/${seq}`),
  replay: (id: string, seq: number) => request<Snapshot>(`/api/sessions/${id}/replay${qs({ seq })}`),
  timeline: (id: string, max_segments = 800) => request<Timeline>(`/api/sessions/${id}/timeline${qs({ max_segments })}`),
  compare: (a: string, b: string) => request<Comparison>(`/api/compare${qs({ a, b })}`),
  lockOrder: (ids: string[]) => request<MergedLockOrder>(`/api/analysis/lock-order${qs({ sessions: ids.join(",") })}`),
  settings: () => request<{ analysis: AnalysisSettings; retention: { max_sessions: number } }>("/api/settings"),
  saveSettings: (body: { analysis: AnalysisSettings; retention: { max_sessions: number } }) =>
    request<{ analysis: AnalysisSettings; retention: { max_sessions: number } }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  importTrace: async (file: File, label?: string) => {
    const res = await fetch(`/api/sessions/import${qs({ label: label ?? file.name })}`, {
      method: "POST",
      body: await file.arrayBuffer(),
      headers: { "Content-Type": "application/octet-stream" },
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new ApiError(res.status, typeof body.detail === "string" ? body.detail : "import failed");
    }
    return (await res.json()) as { id: string };
  },
  exportUrl: (id: string, format: "json" | "ndjson" | "csv" | "raw" | "report") =>
    `/api/sessions/${id}/export${qs({ format })}`,
};

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}
