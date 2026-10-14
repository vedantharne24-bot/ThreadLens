import { describe, expect, it } from "vitest";

import type { LockView, Snapshot, ThreadView } from "@/types/api";
import { diffLines } from "@/utils/code";
import { fmtDuration } from "@/utils/format";
import { buildGraph } from "@/utils/graph";

function thread(index: number, extra: Partial<ThreadView> = {}): ThreadView {
  return {
    index, tid: 100 + index, handle: 0, name: `t${index}`, label: `t${index}`, state: "running", parent: null,
    wait: null, held: [], started_seq: 0, started_ts: 0, ended_seq: null, ended_ts: null, exit_kind: null,
    late: false, detached: false, terminated_by_process_exit: false, last_seq: 0, last_ts: 0, last_type: null,
    events: 0, acquisitions: 0, wait_ns_total: 0, in_cycle: false, ...extra,
  };
}

function lock(id: number, owner: number | null, waiters: number[] = []): LockView {
  return {
    id, label: `L${id}`, annotation: null, symbol: null, stable_key: null, addr: "0x1", kind: "normal", owner,
    count: owner === null ? 0 : 1, owner_since_seq: 0, owner_since_ts: 0, waiters, destroyed: false, first_seq: 0,
    acquisitions: 1, contended: 0, try_failures: 0, timeouts: 0, wait_ns_total: 0, wait_ns_max: 0, hold_ns_total: 0,
    hold_ns_max: 0, holds: 0,
  };
}

const wait = (l: number) => ({ kind: "lock" as const, since_seq: 5, since_ts: 0, timed: false, lock: l, cond: null, target: null, signaled: null });

/** Two threads in a classic deadlock: T1 holds L1 waits L2, T2 holds L2 waits L1. */
function deadlockSnapshot(): Snapshot {
  return {
    seq: 10, first_ts: 0, now_ts: 0, ended: false,
    threads: [
      thread(0),
      thread(1, { state: "blocked_lock", wait: wait(2), held: [{ lock: 1, since_seq: 1, since_ts: 0 }], in_cycle: true }),
      thread(2, { state: "blocked_lock", wait: wait(1), held: [{ lock: 2, since_seq: 2, since_ts: 0 }], in_cycle: true }),
    ],
    locks: [lock(1, 1, [2]), lock(2, 2, [1]), lock(3, null)],
    wait_for: {
      edges: [
        { from: 1, to: 2, kind: "lock", lock: 2, since_seq: 5, since_ts: 0, timed: false, inferred: false },
        { from: 2, to: 1, kind: "lock", lock: 1, since_seq: 6, since_ts: 0, timed: false, inferred: false },
      ],
      cycles: [[1, 2]],
    },
    lock_order: { edges: [], cycles: [] },
    findings: [], coverage: {} as Snapshot["coverage"], counts: {}, marks: [], process: {},
    stats: {} as Snapshot["stats"], verdict: { classification: "confirmed_deadlock", label: "", findings: 1, notes: [] },
  };
}

describe("buildGraph", () => {
  it("builds the resource-allocation graph with holds and waits edges", () => {
    const g = buildGraph(deadlockSnapshot(), "resource");
    const ids = g.nodes.map((n) => n.id).sort();
    expect(ids).toEqual(["l:1", "l:2", "t:0", "t:1", "t:2"]); // idle L3 hidden by default
    const kinds = g.edges.map((e) => `${e.kind}:${e.source}>${e.target}`).sort();
    expect(kinds).toEqual(["holds:l:1>t:1", "holds:l:2>t:2", "waits:t:1>l:2", "waits:t:2>l:1"]);
    expect(g.edges.every((e) => e.classes.includes("cycle"))).toBe(true);
  });

  it("orders cycle members thread → awaited lock → thread for a ring layout", () => {
    const g = buildGraph(deadlockSnapshot(), "resource");
    const order = [...g.nodes].sort((a, b) => (a.order ?? 0) - (b.order ?? 0)).map((n) => n.id);
    expect(order.slice(0, 4)).toEqual(["t:1", "l:2", "t:2", "l:1"]);
  });

  it("shows idle locks only when asked", () => {
    expect(buildGraph(deadlockSnapshot(), "resource", { showIdle: true }).nodes.some((n) => n.id === "l:3")).toBe(true);
  });

  it("builds the wait-for graph from backend edges only", () => {
    const g = buildGraph(deadlockSnapshot(), "waitfor");
    expect(g.edges.map((e) => `${e.source}>${e.target}`).sort()).toEqual(["t:1>t:2", "t:2>t:1"]);
    expect(g.cycleCount).toBe(1);
  });

  it("marks lock-order cycles as potential, not as deadlock", () => {
    const s = deadlockSnapshot();
    s.lock_order = {
      edges: [
        { from: 1, to: 2, count: 1, threads: [1], first_thread: 1, first_seq_src: 1, first_seq: 2, first_ts: 0, last_seq: 2, guards: [], timed_only: false, via_cond: false },
        { from: 2, to: 1, count: 1, threads: [2], first_thread: 2, first_seq_src: 3, first_seq: 4, first_ts: 0, last_seq: 4, guards: [], timed_only: false, via_cond: false },
      ],
      cycles: [[1, 2]],
    };
    const g = buildGraph(s, "lockorder");
    expect(g.edges.every((e) => e.classes.includes("potential") && !e.classes.includes("cycle"))).toBe(true);
  });

  it("returns an empty model without a snapshot", () => {
    expect(buildGraph(null, "resource")).toEqual({ nodes: [], edges: [], cycleCount: 0 });
  });
});

describe("diffLines", () => {
  it("finds the changed lines of the fix", () => {
    const a = ["lock(from)", "lock(to)", "work()"];
    const b = ["first = min(from, to)", "lock(first)", "lock(second)", "work()"];
    const d = diffLines(a, b);
    expect(d.filter((x) => x.kind === "same").map((x) => x.text)).toEqual(["work()"]);
    expect(d.filter((x) => x.kind === "del")).toHaveLength(2);
    expect(d.filter((x) => x.kind === "add")).toHaveLength(3);
  });
});

describe("fmtDuration", () => {
  it("formats nanoseconds with sensible units", () => {
    expect(fmtDuration(500)).toBe("500 ns");
    expect(fmtDuration(2_500)).toBe("3 µs");
    expect(fmtDuration(12_345_678)).toBe("12.3 ms");
    expect(fmtDuration(2_000_000_000)).toBe("2.00 s");
    expect(fmtDuration(null)).toBe("—");
  });
});
