// Types mirroring the backend JSON (app/detection/engine.py snapshot(),
// app/services/eventview.py, app/services/session.py info()).

export type ThreadState = "running" | "blocked_lock" | "waiting_cond" | "waiting_sem" | "blocked_join" | "barrier" | "exited";
/** "x" = mutex (exclusive), "r" / "w" = rwlock held or requested for reading / writing. */
export type LockMode = "x" | "r" | "w";
export type Severity = "critical" | "warning" | "info";
export type Classification =
  | "confirmed_deadlock"
  | "potential_deadlock"
  | "excessive_contention"
  | "api_misuse"
  | "stall"
  | "inconclusive"
  | "no_issue";
export type SessionStatus = "starting" | "running" | "completed" | "failed" | "killed" | "error" | "analyzing";

export interface WaitInfo {
  kind: "lock" | "rwlock" | "join" | "cond" | "sem" | "barrier";
  since_seq: number;
  since_ts: number;
  timed: boolean;
  lock: number | null;
  cond: string | null;
  target: number | null;
  signaled: string | null;
  /** rwlock waits: requested mode */
  mode: "r" | "w" | null;
  /** semaphore waits: address and label */
  sem: string | null;
  sem_label: string | null;
}

export interface ThreadView {
  index: number;
  tid: number;
  handle: number;
  name: string | null;
  label: string;
  state: ThreadState;
  parent: number | null;
  wait: WaitInfo | null;
  held: { lock: number; since_seq: number; since_ts: number; mode: LockMode }[];
  started_seq: number;
  started_ts: number;
  ended_seq: number | null;
  ended_ts: number | null;
  exit_kind: number | null;
  late: boolean;
  detached: boolean;
  terminated_by_process_exit: boolean;
  last_seq: number;
  last_ts: number;
  last_type: string | null;
  events: number;
  acquisitions: number;
  wait_ns_total: number;
  in_cycle: boolean;
}

export interface LockView {
  id: number;
  label: string;
  annotation: string | null;
  symbol: string | null;
  stable_key: string | null;
  addr: string;
  kind: string;
  primitive: "mutex" | "rwlock";
  /** mutex owner, or the writer of an rwlock */
  owner: number | null;
  count: number;
  owner_since_seq: number;
  owner_since_ts: number;
  /** rwlocks: threads holding a read lock (count > 1 for recursive read locks) */
  readers: { thread: number; count: number; since_seq: number; since_ts: number }[];
  waiters: number[];
  destroyed: boolean;
  first_seq: number;
  acquisitions: number;
  contended: number;
  try_failures: number;
  timeouts: number;
  wait_ns_total: number;
  wait_ns_max: number;
  hold_ns_total: number;
  hold_ns_max: number;
  holds: number;
}

export interface WaitEdge {
  from: number;
  to: number;
  kind: "lock" | "rwlock" | "join" | "cond_reacquire";
  lock: number | null;
  since_seq: number;
  since_ts: number;
  timed: boolean;
  inferred: boolean;
  /** rwlock edges: mode requested by `from` (a writer has one edge per reader) */
  mode: "r" | "w" | null;
}

export interface LockOrderEdge {
  from: number;
  to: number;
  count: number;
  threads: number[];
  first_thread: number;
  first_seq_src: number;
  first_seq: number;
  first_ts: number;
  last_seq: number;
  guards: number[];
  timed_only: boolean;
  via_cond: boolean;
  src_modes: LockMode[];
  dst_modes: LockMode[];
}

export interface SemaphoreView {
  addr: string;
  label: string;
  annotation: string | null;
  symbol: string | null;
  /** initial value + posts - successful waits; null when the initial value is unknown */
  value: number | null;
  init_value: number | null;
  pshared: boolean;
  posts: number;
  waits: number;
  posts_without_waiters: number;
  try_failures: number;
  timeouts: number;
  interrupted: number;
  waiters: number[];
  destroyed: boolean;
  first_seq: number;
  wait_ns_total: number;
  wait_ns_max: number;
}

export interface Evidence {
  seq: number;
  ts: number;
  thread: number | null;
  type: string;
  description: string;
  role: "cause" | "context" | "trigger";
  lock: number | null;
  caller: string | null;
  site: string | null;
}

export interface Finding {
  id: string;
  kind: "deadlock" | "lock_order" | "contention" | "misuse" | "stall" | "abandoned";
  classification: Classification;
  classification_label: string;
  severity: Severity;
  title: string;
  summary: string;
  status: "suspected" | "confirmed" | "active" | "resolved" | "filtered";
  threads: number[];
  locks: number[];
  cycle: {
    type: "wait_for" | "lock_order";
    nodes: { kind: "thread" | "lock"; id: number }[];
    edges: (WaitEdge | LockOrderEdge)[];
    path: string;
  } | null;
  evidence: Evidence[];
  explanation: string[];
  remedies: string[];
  limitations: string[];
  detected_seq: number;
  detected_ts: number;
  confirmed_seq: number | null;
  confirmed_ts: number | null;
  resolved_seq: number | null;
  updated_seq: number;
  occurrences: number;
  metrics: Record<string, unknown>;
  filter_reason: string | null;
}

export interface Coverage {
  complete: boolean;
  lost_events: number;
  seq_gaps: number;
  missing_events: number;
  duplicates: number;
  reordered: number;
  anomalies: number;
  anomaly_examples: { kind: string; seq: number; detail: string }[];
  late_threads: number;
  heartbeats: number;
  last_heartbeat_ts: number;
  ring_depth: number;
  ring_capacity: number;
  owner_checks: number;
  owner_mismatches: number;
  clean_exit: boolean;
  stream_ended: boolean;
}

export interface Verdict {
  classification: Classification;
  label: string;
  findings: number;
  notes: string[];
}

export interface Snapshot {
  seq: number;
  first_ts: number;
  now_ts: number;
  ended: boolean;
  threads: ThreadView[];
  locks: LockView[];
  semaphores: SemaphoreView[];
  wait_for: { edges: WaitEdge[]; cycles: number[][] };
  lock_order: { edges: LockOrderEdge[]; cycles: number[][] };
  findings: Finding[];
  coverage: Coverage;
  counts: Record<string, number>;
  marks: { seq: number; ts: number; thread: number | null; text: string }[];
  process: Record<string, number | string>;
  stats: {
    threads_total: number;
    threads_live: number;
    threads_blocked: number;
    threads_waiting: number;
    locks_total: number;
    locks_held: number;
    rwlocks_total: number;
    semaphores_total: number;
    events: number;
    lock_acquisitions: number;
    contended_acquisitions: number;
    total_wait_ns: number;
  };
  verdict: Verdict;
  storage?: { stored_events: number; truncated: boolean };
  replay?: { seq: number; last_seq: number; events: number };
  window?: EventView[];
}

export interface EventView {
  session_id: string;
  event_id: string;
  seq: number;
  ts: number;
  rel_ns: number;
  tid: number;
  thread: number | null;
  thread_label: string | null;
  type: string;
  lock: number | null;
  lock_label: string | null;
  obj: string | null;
  target_thread: number | null;
  correlation: number | null;
  result: number | null;
  result_name: string | null;
  flags: string[];
  caller: string | null;
  text: string | null;
  metadata: Record<string, unknown>;
  description: string;
}

export interface SessionMetrics {
  events: number;
  stored_events?: number;
  events_per_sec: number;
  wall_ms: number;
  process_ms: number;
  lost_events: number;
  heartbeats?: number;
  heartbeat_age_ms?: number | null;
  ring_depth?: number;
  ring_capacity?: number;
  collector_connected?: boolean;
  detection?: Record<string, { finding: string; latency_ms: number; confirmation_delay_ms?: number }>;
}

export interface SessionInfo {
  id: string;
  source: "live" | "import";
  status: SessionStatus;
  scenario_id: string | null;
  label: string | null;
  executable: string | null;
  args: string[];
  pid: number | null;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  end_reason: string | null;
  exit_code: number | null;
  signal: number | null;
  timeout_s?: number;
  event_count: number;
  lost_events: number;
  verdict: Classification | null;
  result?: Record<string, unknown> | null;
  metrics?: SessionMetrics | null;
  alerts?: { level: string; title?: string; text?: string; finding?: string; classification?: string; status?: string }[];
  wall_ms?: number | null;
  detection_latency_ms?: number | null;
  stdout?: string | null;
  stderr?: string | null;
  coverage?: Coverage;
  stats?: Snapshot["stats"];
  verdict_detail?: Verdict;
}

export interface Scenario {
  id: string;
  title: string;
  category: string;
  language: string;
  binary: string;
  sources: string[];
  args: string[];
  timeout_s: number;
  pair?: string;
  description: string;
  teaches: string;
  expect: { verdict: Classification; must: string[]; must_not: string[]; completes: boolean; cycle_length?: number };
  built: boolean;
}

export interface TimelineLane {
  thread: number;
  label: string;
  tid: number;
  start_ns: number;
  end_ns: number | null;
  segments: [number, number, string, number | null][];
}

export interface Timeline {
  duration_ns: number;
  lanes: TimelineLane[];
  downsampled: boolean;
  resolution_ns: number;
}

export interface CompareRow {
  key: string;
  label: string;
  a: unknown;
  b: unknown;
  delta: number | null;
  change: "improved" | "regressed" | null;
  note?: string;
}

export interface CompareSide {
  id: string;
  label: string | null;
  scenario_id: string | null;
  status: string;
  completed: boolean;
  verdict: Classification;
  verdict_label: string;
  confirmed_deadlocks: number;
  potential_deadlocks: number;
  stalls: number;
  events: number;
  snapshot: Pick<Snapshot, "threads" | "locks" | "wait_for" | "lock_order">;
  findings_list: Pick<Finding, "id" | "title" | "classification" | "severity" | "status" | "cycle">[];
  [k: string]: unknown;
}

export interface Comparison {
  a: CompareSide;
  b: CompareSide;
  rows: CompareRow[];
  observations: string[];
  disclaimer: string;
}

export interface MergedLockOrder {
  nodes: { key: string; label: string; stable: boolean; sessions: string[] }[];
  edges: { from: string; to: string; count: number; sessions: string[] }[];
  cycles: { nodes: string[]; sessions: string[]; cross_session: boolean; explanation: string; filter_reason: string | null }[];
  sessions: string[];
}

export interface Health {
  status: string;
  version: string;
  platform: string;
  python: string;
  collector_built: boolean;
  collector_path: string;
  scenarios_total: number;
  scenarios_built: number;
  running_sessions: number;
  max_sessions: number;
  supported_primitives: string[];
  unsupported_primitives: string[];
}

export interface AnalysisSettings {
  contention_min_acquisitions: number;
  contention_ratio: number;
  contention_total_wait_ms: number;
  long_wait_ms: number;
  stall_ms: number;
  confirm_requires_heartbeat: boolean;
}

export type StreamMessage =
  | { type: "hello" | "update" | "end"; session: SessionInfo; snapshot: Snapshot; events: EventView[]; skipped?: number; dropped_updates?: number }
  | { type: "status"; session: SessionInfo }
  | { type: "final"; session: SessionInfo; snapshot: Snapshot | null }
  | { type: "error"; error: string }
  | { type: "pong" };
