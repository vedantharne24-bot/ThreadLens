import type { Classification, LockView, Severity, Snapshot, ThreadState, ThreadView } from "@/types/api";

export function fmtDuration(ns: number | null | undefined, digits = 1): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return "—";
  const abs = Math.abs(ns);
  if (abs >= 60e9) return `${(ns / 60e9).toFixed(1)} min`;
  if (abs >= 1e9) return `${(ns / 1e9).toFixed(2)} s`;
  if (abs >= 1e6) return `${(ns / 1e6).toFixed(digits)} ms`;
  if (abs >= 1e3) return `${(ns / 1e3).toFixed(0)} µs`;
  return `${Math.round(ns)} ns`;
}

export function fmtMs(ms: number | null | undefined, digits = 1): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  return fmtDuration(ms * 1e6, digits);
}

export function fmtRel(ns: number, unit: "ms" | "us" | "ns" = "ms"): string {
  if (unit === "ns") return `${ns.toLocaleString()} ns`;
  if (unit === "us") return `${(ns / 1e3).toFixed(1)} µs`;
  return `${(ns / 1e6).toFixed(3)} ms`;
}

export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
  return n.toLocaleString();
}

export function fmtTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function timeAgo(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const STATE_META: Record<ThreadState, { label: string; color: string; tw: string }> = {
  running: { label: "Running", color: "var(--cyan)", tw: "text-cyan" },
  blocked_lock: { label: "Blocked on lock", color: "var(--warn)", tw: "text-warn" },
  waiting_cond: { label: "Waiting (condvar)", color: "var(--violet)", tw: "text-violet" },
  blocked_join: { label: "Joining", color: "var(--info)", tw: "text-info" },
  barrier: { label: "At barrier", color: "var(--info)", tw: "text-info" },
  exited: { label: "Exited", color: "var(--fg-3)", tw: "text-fg-3" },
};

export const SEGMENT_COLORS: Record<string, string> = {
  running: "var(--cyan)",
  holding: "var(--violet)",
  blocked_lock: "var(--warn)",
  waiting_cond: "color-mix(in oklab, var(--violet) 55%, var(--fg-3))",
  blocked_join: "var(--info)",
  barrier: "var(--info)",
};

export const SEVERITY_META: Record<Severity, { label: string; color: string; tw: string; bg: string }> = {
  critical: { label: "Critical", color: "var(--crit)", tw: "text-crit", bg: "bg-crit/10" },
  warning: { label: "Warning", color: "var(--warn)", tw: "text-warn", bg: "bg-warn/10" },
  info: { label: "Info", color: "var(--info)", tw: "text-info", bg: "bg-info/10" },
};

export const VERDICT_META: Record<Classification, { short: string; color: string; tone: "crit" | "warn" | "ok" | "info" }> = {
  confirmed_deadlock: { short: "Confirmed deadlock", color: "var(--crit)", tone: "crit" },
  stall: { short: "Stall", color: "var(--crit)", tone: "crit" },
  potential_deadlock: { short: "Potential deadlock", color: "var(--warn)", tone: "warn" },
  excessive_contention: { short: "Contention", color: "var(--warn)", tone: "warn" },
  api_misuse: { short: "API misuse", color: "var(--warn)", tone: "warn" },
  inconclusive: { short: "Inconclusive", color: "var(--info)", tone: "info" },
  no_issue: { short: "No issue detected", color: "var(--ok)", tone: "ok" },
};

export const TYPE_GROUPS: Record<string, string[]> = {
  Locks: ["LOCK_REQUEST", "LOCK_ACQUIRED", "LOCK_FAILED", "LOCK_RELEASE", "UNLOCK_FAILED", "MUTEX_INIT", "MUTEX_DESTROY"],
  "Cond vars": ["COND_WAIT_BEGIN", "COND_WAIT_END", "COND_SIGNAL", "COND_BROADCAST"],
  Threads: ["THREAD_CREATE", "THREAD_START", "THREAD_EXIT", "JOIN_BEGIN", "JOIN_END", "THREAD_DETACH", "CANCEL_REQUEST"],
  Other: ["BARRIER_WAIT_BEGIN", "BARRIER_WAIT_END", "THREAD_NAME", "LOCK_NAME", "USER_MARK", "PROCESS_START", "PROCESS_EXIT"],
};

export function typeColor(type: string): string {
  if (type === "LOCK_REQUEST") return "var(--warn)";
  if (type === "LOCK_ACQUIRED") return "var(--cyan)";
  if (type === "LOCK_RELEASE") return "var(--fg-3)";
  if (type === "LOCK_FAILED" || type === "UNLOCK_FAILED") return "var(--crit)";
  if (type.startsWith("COND")) return "var(--violet)";
  if (type.startsWith("THREAD") || type.startsWith("JOIN") || type === "CANCEL_REQUEST") return "var(--info)";
  return "var(--fg-2)";
}

export function threadMap(s: Snapshot | null): Map<number, ThreadView> {
  return new Map((s?.threads ?? []).map((t) => [t.index, t]));
}

export function lockMap(s: Snapshot | null): Map<number, LockView> {
  return new Map((s?.locks ?? []).map((l) => [l.id, l]));
}

export function shortPath(p: string | null | undefined): string {
  if (!p) return "—";
  const parts = p.split("/");
  return parts.slice(-2).join("/");
}

export function endReasonLabel(r: string | null | undefined): string {
  switch (r) {
    case "exit":
      return "exited normally";
    case "deadlock_kill":
      return "terminated after confirmed deadlock";
    case "stall_kill":
      return "terminated after stall";
    case "timeout":
      return "terminated by timeout";
    case "user_stop":
      return "stopped by user";
    case "imported":
      return "imported trace";
    default:
      return r ?? "—";
  }
}
