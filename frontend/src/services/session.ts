import { api, wsUrl } from "@/services/api";
import { createStore } from "@/services/store";
import { toast } from "@/services/toast";
import type { EventView, SessionInfo, Snapshot, StreamMessage } from "@/types/api";

export interface RatePoint {
  t: number;
  rate: number;
  events: number;
}

export interface SessionState {
  activeId: string | null;
  info: SessionInfo | null;
  snapshot: Snapshot | null;
  events: EventView[];
  connection: "idle" | "connecting" | "live" | "final" | "error";
  error: string | null;
  rate: RatePoint[];
  droppedUpdates: number;
  skippedEvents: number;
  updatedAt: number;
}

const LIVE_EVENT_CAP = 1500;
const STORAGE_KEY = "threadlens.activeSession";

function loadActive(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export const sessionStore = createStore<SessionState>({
  activeId: null,
  info: null,
  snapshot: null,
  events: [],
  connection: "idle",
  error: null,
  rate: [],
  droppedUpdates: 0,
  skippedEvents: 0,
  updatedAt: 0,
});

let ws: WebSocket | null = null;
let generation = 0;
let retryTimer: number | null = null;
const seenAlerts = new Set<string>();

function close() {
  if (retryTimer !== null) window.clearTimeout(retryTimer);
  retryTimer = null;
  if (ws) {
    ws.onclose = null;
    ws.onmessage = null;
    ws.close();
  }
  ws = null;
}

/** Make `id` the active session and start streaming it. */
export function openSession(id: string | null) {
  close();
  generation += 1;
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable: fine */
  }
  sessionStore.set({
    activeId: id,
    info: null,
    snapshot: null,
    events: [],
    connection: id ? "connecting" : "idle",
    error: null,
    rate: [],
    droppedUpdates: 0,
    skippedEvents: 0,
  });
  if (id) connect(id, generation, 0);
}

function connect(id: string, gen: number, attempt: number) {
  const sock = new WebSocket(wsUrl(`/ws/sessions/${id}`));
  ws = sock;
  sock.onmessage = (m) => {
    if (gen !== generation) return;
    let msg: StreamMessage;
    try {
      msg = JSON.parse(m.data);
    } catch {
      return;
    }
    handle(msg);
  };
  sock.onclose = () => {
    if (gen !== generation) return;
    const st = sessionStore.get();
    if (st.connection === "final") return;
    // the stream ended without a final message: fall back to REST and retry
    void refreshFromRest(id, gen);
    if (attempt < 6 && st.info?.status !== "completed") {
      retryTimer = window.setTimeout(() => connect(id, gen, attempt + 1), Math.min(4000, 400 * 2 ** attempt));
    }
  };
}

async function refreshFromRest(id: string, gen: number) {
  try {
    const [info, snapshot] = await Promise.all([api.session(id), api.state(id).catch(() => null)]);
    if (gen !== generation) return;
    const done = !["running", "starting"].includes(info.status);
    sessionStore.set({
      info,
      snapshot: snapshot ?? sessionStore.get().snapshot,
      connection: done ? "final" : "connecting",
      updatedAt: Date.now(),
    });
    if (done && sessionStore.get().events.length === 0) void loadTailEvents(id, gen);
  } catch (e) {
    if (gen !== generation) return;
    sessionStore.set({ connection: "error", error: (e as Error).message });
  }
}

async function loadTailEvents(id: string, gen: number) {
  try {
    const page = await api.events(id, { order: "desc", limit: 300 });
    if (gen !== generation) return;
    sessionStore.set({ events: page.events.slice().reverse() });
  } catch {
    /* events are optional here */
  }
}

function handle(msg: StreamMessage) {
  const st = sessionStore.get();
  const gen = generation;
  switch (msg.type) {
    case "hello":
    case "update":
    case "end": {
      const events = msg.events.length
        ? [...st.events, ...msg.events].slice(-LIVE_EVENT_CAP)
        : st.events;
      const metrics = msg.session.metrics;
      const rate = metrics
        ? [...st.rate, { t: Date.now(), rate: metrics.events_per_sec, events: metrics.events }].slice(-180)
        : st.rate;
      sessionStore.set({
        info: msg.session,
        snapshot: msg.snapshot,
        events,
        rate,
        connection: msg.type === "end" ? "final" : "live",
        droppedUpdates: msg.dropped_updates ?? st.droppedUpdates,
        skippedEvents: st.skippedEvents + (msg.skipped ?? 0),
        updatedAt: Date.now(),
      });
      notifyAlerts(msg.session);
      if (msg.type === "end") {
        // the stored session has the final metrics (wall time, latency)
        if (msg.session.id) void refreshFromRest(msg.session.id, gen);
      }
      break;
    }
    case "status":
      sessionStore.set({ info: msg.session, updatedAt: Date.now() });
      notifyAlerts(msg.session);
      break;
    case "final":
      sessionStore.set({ info: msg.session, snapshot: msg.snapshot, connection: "final", updatedAt: Date.now() });
      if (msg.session?.id) {
        void loadTailEvents(msg.session.id, gen);
        void refreshFromRest(msg.session.id, gen);
      }
      break;
    case "error":
      sessionStore.set({ connection: "error", error: msg.error });
      break;
  }
}

function notifyAlerts(info: SessionInfo) {
  for (const a of info.alerts ?? []) {
    const key = `${info.id}:${a.finding ?? a.text}:${a.status ?? ""}`;
    if (seenAlerts.has(key)) continue;
    seenAlerts.add(key);
    if (a.finding) {
      const confirmed = a.status === "confirmed";
      toast.push({
        level: a.level === "critical" ? "critical" : a.level === "warning" ? "warning" : "info",
        title: confirmed ? "Deadlock confirmed" : a.title ?? "New finding",
        body: confirmed ? a.title : a.classification?.replace(/_/g, " "),
        action: { label: "View diagnosis", href: `/app/diagnostics?f=${a.finding}` },
      });
    } else if (a.text) {
      toast.push({ level: a.level === "error" ? "critical" : "warning", title: "Collector", body: a.text });
    }
  }
}

/** Select the session from a `?session=<id>` deep link, else the last one used in this browser. */
export function restoreActiveSession() {
  const fromUrl = new URLSearchParams(window.location.search).get("session");
  if (fromUrl && /^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/.test(fromUrl)) {
    openSession(fromUrl);
    return;
  }
  const id = loadActive();
  if (id && !sessionStore.get().activeId) openSession(id);
}
