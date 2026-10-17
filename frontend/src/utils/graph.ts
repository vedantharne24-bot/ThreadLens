import type { Snapshot } from "@/types/api";
import { lockHolders, modeWord } from "@/utils/format";

export type GraphMode = "resource" | "waitfor" | "lockorder";

export interface GNode {
  id: string;
  kind: "thread" | "lock" | "sem";
  /** thread index, lock id, or index into snapshot.semaphores */
  ref: number;
  label: string;
  sub: string;
  classes: string[];
  order?: number; // layout hint: position along a cycle
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  kind: "holds" | "waits" | "join" | "reacquire" | "order";
  label?: string;
  classes: string[];
}

export interface GraphModel {
  nodes: GNode[];
  edges: GEdge[];
  cycleCount: number;
}

export const MODE_INFO: Record<GraphMode, { title: string; semantics: string }> = {
  resource: {
    title: "Resource allocation",
    semantics:
      "Current state. Thread → lock means “is waiting for”; lock → thread means “is held by” (an rwlock can be read-held by several threads, and a writer waits for all of them). Semaphores have no holder: only the threads waiting on them are drawn. A closed loop of these arrows is a circular wait.",
  },
  waitfor: {
    title: "Wait-for graph",
    semantics:
      "Current state, threads only. A → B means A is blocked until B releases a lock or exits; a writer waiting on an rwlock points at every reader. A cycle here is a deadlock (confirmed when every edge is an observed blocking wait). Semaphore waits have no owner and add no edge.",
  },
  lockorder: {
    title: "Lock-order graph",
    semantics:
      "History. L1 → L2 means some thread acquired L2 while holding L1. A cycle means locks are taken in conflicting orders: a POTENTIAL deadlock, even if this run never hung.",
  },
};

function cycleSets(s: Snapshot) {
  const threads = new Set<number>();
  const pairs = new Set<string>();
  for (const c of s.wait_for.cycles) {
    c.forEach((n, i) => {
      threads.add(n);
      pairs.add(`${n}>${c[(i + 1) % c.length]}`);
    });
  }
  return { threads, pairs };
}

export function buildGraph(s: Snapshot | null, mode: GraphMode, opts: { showIdle?: boolean; showExited?: boolean } = {}): GraphModel {
  if (!s) return { nodes: [], edges: [], cycleCount: 0 };
  const threads = new Map(s.threads.map((t) => [t.index, t]));
  const locks = new Map(s.locks.map((l) => [l.id, l]));
  const nodes: GNode[] = [];
  const edges: GEdge[] = [];

  if (mode === "lockorder") {
    const cyc = new Set<string>();
    const cycNodes = new Set<number>();
    for (const c of s.lock_order.cycles) {
      c.forEach((n, i) => {
        cycNodes.add(n);
        cyc.add(`${n}>${c[(i + 1) % c.length]}`);
      });
    }
    const filtered = new Set(
      s.findings.filter((f) => f.kind === "lock_order" && f.status === "filtered").flatMap((f) => f.locks),
    );
    const used = new Set<number>();
    s.lock_order.edges.forEach((e) => {
      used.add(e.from);
      used.add(e.to);
    });
    for (const id of opts.showIdle ? locks.keys() : used) {
      const l = locks.get(id);
      if (!l) continue;
      nodes.push({
        id: `l:${id}`,
        kind: "lock",
        ref: id,
        label: l.label,
        sub: `L${id} · ${l.acquisitions} acq`,
        classes: ["lock", ...(cycNodes.has(id) ? [filtered.has(id) ? "potential-filtered" : "potential"] : [])],
      });
    }
    for (const e of s.lock_order.edges) {
      const inCycle = cyc.has(`${e.from}>${e.to}`);
      edges.push({
        id: `o:${e.from}:${e.to}`,
        source: `l:${e.from}`,
        target: `l:${e.to}`,
        kind: "order",
        label: `${e.count}×`,
        classes: ["order", ...(inCycle ? ["potential"] : []), ...(e.timed_only ? ["timed"] : [])],
      });
    }
    return { nodes, edges, cycleCount: s.lock_order.cycles.length };
  }

  const { threads: cycThreads, pairs } = cycleSets(s);
  const visibleThreads = s.threads.filter(
    (t) => t.state !== "exited" || opts.showExited || t.held.length > 0 || cycThreads.has(t.index),
  );
  for (const t of visibleThreads) {
    const state = t.state;
    nodes.push({
      id: `t:${t.index}`,
      kind: "thread",
      ref: t.index,
      label: t.label,
      sub: `T${t.index} · tid ${t.tid}`,
      classes: ["thread", state, ...(cycThreads.has(t.index) ? ["cycle"] : [])],
    });
  }

  if (mode === "waitfor") {
    for (const e of s.wait_for.edges) {
      const inCycle = pairs.has(`${e.from}>${e.to}`);
      const lock = e.lock !== null ? locks.get(e.lock) : undefined;
      const how = e.kind === "rwlock" ? modeWord(e.mode) : "";
      edges.push({
        id: `w:${e.from}:${e.to}:${e.kind}`,
        source: `t:${e.from}`,
        target: `t:${e.to}`,
        kind: e.kind === "join" ? "join" : e.kind === "cond_reacquire" ? "reacquire" : "waits",
        label: e.kind === "join" ? "join" : how ? `${lock?.label ?? "rwlock"} (${how})` : lock?.label,
        classes: [
          e.kind === "join" ? "join" : e.kind === "cond_reacquire" ? "reacquire" : "waits",
          ...(inCycle ? ["cycle"] : []),
          ...(e.timed ? ["timed"] : []),
          ...(e.inferred ? ["inferred"] : []),
        ],
      });
    }
    return { nodes, edges, cycleCount: s.wait_for.cycles.length };
  }

  // resource allocation graph: threads + locks (+ semaphores that have waiters)
  const relevant = new Set<number>();
  for (const l of s.locks) {
    if (l.owner !== null || (l.readers ?? []).length > 0 || l.waiters.length > 0 || opts.showIdle) relevant.add(l.id);
  }
  for (const t of visibleThreads) if (t.wait?.lock !== null && t.wait?.lock !== undefined) relevant.add(t.wait.lock);
  const visibleIds = new Set(visibleThreads.map((t) => t.index));
  for (const id of relevant) {
    const l = locks.get(id);
    if (!l || l.destroyed) continue;
    const holders = lockHolders(l);
    const holderInCycle = (h: number) => cycThreads.has(h) && l.waiters.some((w) => cycThreads.has(w) && pairs.has(`${w}>${h}`));
    const lockInCycle = holders.some((h) => holderInCycle(h.thread));
    nodes.push({
      id: `l:${id}`,
      kind: "lock",
      ref: id,
      label: l.label,
      sub: `L${id} · ${l.kind}`,
      classes: ["lock", ...(l.primitive === "rwlock" ? ["rwlock"] : []), ...(holders.length ? ["held"] : []), ...(lockInCycle ? ["cycle"] : [])],
    });
    for (const h of holders) {
      if (!visibleIds.has(h.thread)) continue;
      const inCycle = holderInCycle(h.thread);
      const label =
        h.mode === "r" ? (h.count > 1 ? `read ×${h.count}` : "read by") : h.mode === "w" ? "write-held by" : h.count > 1 ? `held ×${h.count}` : "held by";
      edges.push({
        id: `h:${id}:${h.thread}`,
        source: `l:${id}`,
        target: `t:${h.thread}`,
        kind: "holds",
        label,
        classes: ["holds", ...(h.mode === "r" ? ["shared"] : []), ...(inCycle ? ["cycle"] : [])],
      });
    }
  }
  const semIndex = new Map((s.semaphores ?? []).map((sem, i) => [sem.addr, i]));
  const semShown = new Set<number>();
  (s.semaphores ?? []).forEach((sem, i) => {
    if (sem.destroyed || (!sem.waiters.length && !opts.showIdle)) return;
    semShown.add(i);
    nodes.push({
      id: `s:${i}`,
      kind: "sem",
      ref: i,
      label: sem.label,
      sub: `semaphore · count ${sem.value ?? "?"}`,
      classes: ["sem", ...(sem.waiters.length ? ["waited"] : [])],
    });
  });
  for (const t of visibleThreads) {
    const w = t.wait;
    if (!w) continue;
    if ((w.kind === "lock" || w.kind === "rwlock" || (w.kind === "cond" && w.signaled)) && w.lock !== null && relevant.has(w.lock)) {
      const l = locks.get(w.lock);
      const holders = l ? lockHolders(l) : [];
      const inCycle = holders.some((h) => pairs.has(`${t.index}>${h.thread}`));
      const how = w.kind === "rwlock" ? modeWord(w.mode) : "";
      edges.push({
        id: `r:${t.index}:${w.lock}`,
        source: `t:${t.index}`,
        target: `l:${w.lock}`,
        kind: w.kind === "cond" ? "reacquire" : "waits",
        label: w.kind === "cond" ? "re-acquire" : how ? `waits to ${how}${w.timed ? " (timed)" : ""}` : w.timed ? "waits (timed)" : "waits for",
        classes: [w.kind === "cond" ? "reacquire" : "waits", ...(inCycle ? ["cycle"] : []), ...(w.timed ? ["timed"] : [])],
      });
    }
    if (w.kind === "sem" && w.sem !== null) {
      const i = semIndex.get(w.sem);
      if (i !== undefined && semShown.has(i)) {
        edges.push({
          id: `q:${t.index}:${i}`,
          source: `t:${t.index}`,
          target: `s:${i}`,
          kind: "waits",
          label: w.timed ? "sem_wait (timed)" : "sem_wait",
          classes: ["waits", "sem", ...(w.timed ? ["timed"] : [])],
        });
      }
    }
    if (w.kind === "join" && w.target !== null && visibleIds.has(w.target)) {
      edges.push({
        id: `j:${t.index}:${w.target}`,
        source: `t:${t.index}`,
        target: `t:${w.target}`,
        kind: "join",
        label: "joins",
        classes: ["join", ...(pairs.has(`${t.index}>${w.target}`) ? ["cycle"] : [])],
      });
    }
  }
  // order cycle members thread -> lock it waits for -> next thread ... for ring layouts
  const order = new Map<string, number>();
  let k = 0;
  for (const c of s.wait_for.cycles) {
    for (const ti of c) {
      order.set(`t:${ti}`, k++);
      const w = threads.get(ti)?.wait;
      if (w?.lock !== null && w?.lock !== undefined) order.set(`l:${w.lock}`, k++);
    }
  }
  for (const n of nodes) n.order = order.get(n.id) ?? 1000 + nodes.indexOf(n);
  return { nodes, edges, cycleCount: s.wait_for.cycles.length };
}
