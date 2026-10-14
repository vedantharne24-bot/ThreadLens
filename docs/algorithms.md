# Algorithms

## 1. State tracking

For every in-band event (in sequence order) the analyzer updates:

* **threads** — state (`running`, `blocked_lock`, `waiting_cond`,
  `blocked_join`, `barrier`, `exited`), the single thing it waits for (`Wait`),
  the locks it holds (`Held`), lifecycle data and counters;
* **locks** — owner, recursion count, waiters, kind and statistics
  (acquisitions, contended acquisitions, wait/hold time);
* **condition variables** — waiters, signals, signals that found no waiter.

Key transitions (`backend/app/detection/engine.py`):

| Event | Effect |
|---|---|
| `LOCK_REQUEST(t, L)` | `t.wait = lock L`; record owner at request (ours and glibc's); check for a cycle from `t` |
| `LOCK_ACQUIRED(t, L)` | clear the wait; record lock-order edges `H → L` for every held `H` (blocking acquisitions only); `L.owner = t` (or `count += 1` for recursive); re-check cycles for remaining waiters of `L` |
| `LOCK_RELEASE(t, L)` | owner: `count -= 1`, free at 0. Non-owner: normal mutex → really released (misuse); error-checking/recursive/robust → wait for `UNLOCK_FAILED` |
| `UNLOCK_FAILED` | undo the release applied for that thread; misuse finding |
| `COND_WAIT_BEGIN(t, c, m)` | release `m` completely (remember recursion count); `t.wait = cond` |
| `COND_SIGNAL/BROADCAST(c)` | mark waiters as signalled (inferred re-acquire edge); if no waiter: remember a *lost signal* |
| `COND_WAIT_END(t, c, m)` | re-acquire `m` (lock-order edges too); clear the wait |
| `JOIN_BEGIN(t, h)` | `t.wait = join h`; check for a cycle |
| `THREAD_EXIT(t)` | still holding locks → *abandoned lock* finding; waiters on them → permanent-block finding |

All operations are O(1) except the cycle checks below.

## 2. Current wait-for graph and deadlock detection

**Definition.** Edge `A → B` iff thread A is blocked waiting for something only B
can provide:

* A has an outstanding `LOCK_REQUEST` for mutex `L` and `L.owner = B` (`B = A`
  for a normal mutex re-locked by its owner — a self-loop; recursive and
  error-checking mutexes do not wait);
* A is in `pthread_join(B)` and B is alive;
* (inferred) A was signalled out of `pthread_cond_wait(c, m)` and `m.owner = B`.

**Observation.** A blocked thread waits for exactly one thing, so every node has
out-degree ≤ 1: the wait-for graph is a *functional graph*. The cycle reachable
from a node is found by following successors:

```text
FIND-CYCLE-FROM(v):
    position ← empty map; path ← []
    while v ≠ NIL:
        if v ∈ position: return path[position[v] ..]     ▷ the loop part
        position[v] ← |path|; append v to path
        v ← successor(v)                                  ▷ derived from current state
    return NIL
```

Cost: O(length of the chain) ≤ O(T) for T threads; no visited-set over the whole
graph is needed. It runs only after events that can create or redirect an edge
(`LOCK_REQUEST`, `LOCK_ACQUIRED` with waiters, `COND_WAIT_END` with waiters,
`JOIN_BEGIN`, `THREAD_START` of a pending join target, `COND_SIGNAL`).
Snapshots list all cycles with one pass over all nodes (`all_cycles`, O(T)).

### Classification of a wait-for cycle

```text
ON-CYCLE(nodes, edges):
    if coverage incomplete or any lock edge has unknown ownership:
        INCONCLUSIVE                              ▷ never confirm on partial data
    else if any edge is inferred (cond re-acquire):
        POTENTIAL (likely deadlock)
    else if any edge is a timed wait:
        POTENTIAL (timed cycle; resolves by timeout)
    else:
        DEADLOCK, status = suspected              ▷ confirmed below

ON-HEARTBEAT(hb_seq):                             ▷ "all events with seq < hb_seq delivered"
    for each suspected cycle formed at seq f:
        if coverage incomplete: INCONCLUSIVE
        else if hb_seq > f and the same cycle is still present: CONFIRMED
        else if the cycle disappeared: resolved (transient)
END-OF-STREAM: confirm suspected cycles still present.
```

### Why a confirmed cycle is trustworthy (argument for the viva)

1. *Ordering.* The collector records `LOCK_RELEASE` before the real unlock and
   `LOCK_ACQUIRED` after the real lock returns; sequence numbers are ring slot
   indexes claimed atomically. So if B acquires L after A released it, then
   `seq(release by A) < seq(acquire by B)`: the order in the trace is a valid
   order of ownership changes.
2. *Ownership.* When the analyzer has applied every event with sequence < n,
   the tracked owner of each lock is its true owner at "time n" (by 1), provided
   no event is missing — guaranteed by contiguous sequence numbers and a zero
   loss counter.
3. *Blocking.* A thread with an outstanding untimed `LOCK_REQUEST` on a mutex
   owned by another thread is inside `pthread_mutex_lock`, which can only
   return after the owner unlocks. An owner that is itself blocked in such a
   call cannot unlock. Following the cycle, no thread on it can proceed.
4. *Persistence.* The heartbeat after the cycle-forming event proves that no
   event recorded before it (e.g. a release) is still in transit.

Hence: **under the observed synchronization model** (only intercepted pthread
operations; no unlock performed by un-instrumented code) the cycle is a
deadlock. What the argument cannot cover is listed in `limitations.md`.

## 3. Historical lock-order graph (potential deadlocks)

**Definition.** Edge `L1 → L2` iff some thread acquired `L2` with a *blocking*
lock call while holding `L1` (GoodLock / lockdep). Each edge stores the
observing threads, a count, the first evidence (both acquisitions with call
sites), whether every observation was timed, and the **guard set** — the
locks held during *every* observation besides `L1`, `L2`.

Try-lock acquisitions add **no** edge: a try-lock never waits, so it can never be
part of a circular wait (this is why C++ `std::scoped_lock`, which uses
lock/try-lock/back-off, is correctly reported as clean).

```text
ON-ACQUIRE(t, L, blocking):
    if not blocking: return
    for each H held by t, H ≠ L:
        new ← OBSERVE-EDGE(H → L, thread t, guards = held(t) − {H, L})
        if new:
            path ← BFS(L ⇝ H)                    ▷ shortest path back
            if path: REPORT-POTENTIAL(cycle = H → L → … → H)
```

Cost: O(V + E) per *new* edge; repeated observations are O(1) except filter
re-evaluation for edges that belong to a reported cycle.

**Filters (from Agarwal et al., IBM J. R&D 2010)** — findings are kept but marked
*filtered* (severity info):

* *single-thread cycle*: every edge was observed only in the same single thread;
* *gate lock*: some lock G was held during every observation of every edge.

**Cross-run analysis.** Lock-order edges of several sessions are merged using
stable lock keys (annotation or symbol name). Components with cycles are found
with Tarjan's SCC algorithm (O(V + E)) and a shortest cycle is extracted from
each; a cycle whose edges come from different sessions is reported as
"only visible by merging runs".

## 4. Contention

Per lock: acquisitions, *contended* acquisitions (the lock was held by another
thread at request time — by our state or by glibc's owner field), total and
maximum wait (measured inside the target between request and acquisition),
hold times. A lock is reported as **excessive contention** when

```
acquisitions ≥ 20  and  contended / acquisitions ≥ 0.5  and  total wait ≥ 100 ms
```

or when a single wait outside any cycle exceeds 500 ms. Thresholds are
configurable in Settings. Contention is never reported as a deadlock.

## 5. Stall detection (no lock cycle)

On each heartbeat: if no in-band event arrived for `stall_ms` (default 1 s),
every live thread is in an **untimed** wait (lock with an owner, join on a live
thread, condition variable not signalled, barrier) and no wait-for cycle explains
it, report a **stall** with evidence. If a signal on the same condition variable
happened *before* the wait began and found no waiter, the explanation points to a
lost wake-up. The cause is reported as inconclusive: a wait-for graph cannot say
who *should* have signalled.

## 6. Event replay

The analyzer is deterministic, so the state after event *n* is reproduced by
re-applying events 0…n (plus the heartbeats that were applied after each of
them). Checkpoints (pickled analyzer every 5 000 events) make random access
O(5 000) events per query. This is event replay, **not** deterministic
re-execution.

## 7. Complexity summary

| Operation | Cost |
|---|---|
| Collector record | O(1): one CAS + 64-byte write; no syscalls on the fast path |
| State update per event | O(1) amortised (+ O(held locks) on acquire) |
| Wait-for cycle check | O(chain length) ≤ O(T) |
| Lock-order cycle check | O(V + E) per new edge |
| Snapshot | O(T + L + E) |
| Replay to seq n | O(min(n, 5 000)) after a one-time O(N) index build |
| Cross-run merge | O(Σ edges) + Tarjan O(V + E) |
