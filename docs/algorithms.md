# Algorithms

## 1. State tracking

For every in-band event (in sequence order) the analyzer updates:

* **threads** — state (`running`, `blocked_lock`, `waiting_cond`,
  `waiting_sem`, `blocked_join`, `barrier`, `exited`), the single thing it
  waits for (`Wait`, with the requested mode for rwlocks), the locks it holds
  (`Held`, with mode `x` mutex, `r`/`w` rwlock), lifecycle data and counters;
* **locks** — owner (the writer, for an rwlock), recursion count, the set of
  readers with their read-recursion counts (rwlocks), waiters, kind and
  statistics (acquisitions, contended acquisitions, wait/hold time);
* **condition variables** — waiters, signals, signals that found no waiter;
* **semaphores** — waiters, initial value (when `sem_init` was observed),
  posts, successful waits (so the count = initial + posts − waits), posts that
  found no waiter, timeouts and try failures.

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
| `RW_REQUEST(t, R, mode)` | `t.wait = rwlock R for mode`; cross-check glibc's writer TID; check for a cycle |
| `RW_ACQUIRED(t, R, w)` | `R.owner = t` (the writer); lock-order edges `H → R` with modes (blocking only); re-check cycles for R's waiters |
| `RW_ACQUIRED(t, R, r)` | add t to `R.readers` (or increment its read count); lock-order edges as above |
| `RW_RELEASE(t, R)` | t is the writer → free; t is a reader → decrement, drop at 0; t holds nothing → misuse, and R's holder state is marked untrusted |
| `SEM_WAIT_BEGIN(t, S)` | `t.wait = sem S` (no wait-for edge) |
| `SEM_WAIT_END(t, S, 0)` | clear the wait; `S.waits += 1`; if the count would go below 0 (a post was not observed) → anomaly |
| `SEM_POST(t, S)` | `S.posts += 1`; no waiter → remember a *banked* post (unlike a condvar signal it is not lost) |
| `THREAD_EXIT(t)` | still holding locks (including read locks) → *abandoned lock* finding; conflicting waiters on them → permanent-block finding |

All operations are O(1) except the cycle checks below (and O(readers) for an
rwlock write request).

## 2. Current wait-for graph and deadlock detection

**Definition.** Edge `A → B` iff thread A is blocked waiting for something only B
can provide:

* A has an outstanding `LOCK_REQUEST` for mutex `L` and `L.owner = B` (`B = A`
  for a normal mutex re-locked by its owner — a self-loop; recursive and
  error-checking mutexes do not wait);
* A is in `pthread_join(B)` and B is alive;
* (inferred) A was signalled out of `pthread_cond_wait(c, m)` and `m.owner = B`;
* A has an outstanding `RW_REQUEST` on rwlock `R` and B holds `R` in a
  conflicting mode: B is the writer (any request waits for it), or A requests
  *write* and B is one of the readers — **one edge per reader**, including
  A itself when A already reads `R` (an in-place upgrade, which glibc never
  grants: a self-loop). A request by the writer itself fails with EDEADLK and
  adds no edge. A *read* request with no writer proceeds (glibc prefers
  readers), except on writer-preferring rwlocks, where it queues behind the
  waiting writers (inferred edges).

Semaphore waits add **no** edge: a semaphore has no owner, any thread may post
it, so "A waits for B" cannot be stated. They are handled by stall detection
(§5), like condition-variable waits.

**Observation.** With mutexes, joins and condition variables a blocked thread
waits for exactly one thing, so every node has out-degree ≤ 1: the wait-for
graph is a *functional graph*. The cycle reachable from a node is found by
following successors:

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
`JOIN_BEGIN`, `THREAD_START` of a pending join target, `COND_SIGNAL`,
`RW_REQUEST`, `RW_ACQUIRED` with waiters).
Snapshots list all cycles with one pass over all nodes (`all_cycles`, O(T)).

### Shared ownership: rwlocks make the graph non-functional

A writer waiting on an rwlock has an edge to **every** reader, so its
out-degree can exceed 1 and the successor walk no longer applies. The wait is
an AND-wait — the writer proceeds only when *all* of those readers have
released — so a cycle through **any one** of its edges is still a deadlock
(AND model: a cycle is necessary and sufficient). While at least one thread is
waiting on an rwlock the analyzer therefore switches to depth-first search;
otherwise it keeps the O(chain) walk:

```text
CHECK-CYCLE-FROM(v):
    if some thread waits on an rwlock:  FIND-CYCLE-DFS(v, successors)   ▷ O(V + E) reachable part
    else:                               FIND-CYCLE-FROM(v)              ▷ functional fast path

FIND-CYCLE-DFS(v):
    on_path ← {v}; path ← [v]; stack ← [iter(successors(v))]; done ← ∅
    while stack ≠ []:
        u ← next(top(stack))
        if u = NIL:  pop stack; remove last of path from on_path; continue
        if u ∈ on_path: return path[index(u) ..]          ▷ back edge: cycle
        if u ∈ done: continue
        done ← done ∪ {u}; push u on path and on_path; push iter(successors(u))
    return NIL
```

Successors are generated with a self-loop first, so an upgrade is reported as
the self-deadlock it is. Snapshots use Tarjan's SCC algorithm and list a
shortest cycle through every edge inside each component (bounded, deduplicated
by rotation) — `cycles_multi`. Cycle identity (for confirmation and
resolution) is the set of edges `(from, to, kind, lock)` along the cycle, so a
writer's other readers leaving does not "change" the reported cycle, and the
heartbeat confirmation of §2 applies unchanged.

### Classification of a wait-for cycle

```text
ON-CYCLE(nodes, edges):
    if coverage incomplete or any lock edge has unknown ownership:
        INCONCLUSIVE                              ▷ never confirm on partial data
    else if any edge is inferred (cond re-acquire, reader queued behind a writer):
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
   return after the owner unlocks. The same holds for an untimed `RW_REQUEST`:
   a writer's `pthread_rwlock_wrlock` returns only after *every* reader and
   any writer have released, a reader's only after the writer has. A holder
   that is itself blocked in such a call cannot release. Following the cycle,
   no thread on it can proceed.
4. *Persistence.* The heartbeat after the cycle-forming event proves that no
   event recorded before it (e.g. a release) is still in transit.

Hence: **under the observed synchronization model** (only intercepted pthread
operations; no unlock performed by un-instrumented code) the cycle is a
deadlock. What the argument cannot cover is listed in `limitations.md`.

## 3. Historical lock-order graph (potential deadlocks)

**Definition.** Edge `L1 → L2` iff some thread acquired `L2` with a *blocking*
lock call while holding `L1` (GoodLock / lockdep). Nodes are mutexes and
rwlocks alike; `pthread_rwlock_rdlock` and `wrlock` acquisitions take part.
Each edge stores the observing threads, a count, the first evidence (both
acquisitions with call sites), whether every observation was timed, the
**modes** seen (how `L1` was held — `x` mutex, `r`/`w` rwlock — and how `L2` was
requested) and the **guard set** — the locks held *exclusively* (mutex or
write-held rwlock) during *every* observation besides `L1`, `L2`.

Try-lock acquisitions (`pthread_mutex_trylock`, `pthread_rwlock_try*lock`) add
**no** edge: a try-lock never waits, so it can never be part of a circular wait
(this is why C++ `std::scoped_lock`, which uses lock/try-lock/back-off, is
correctly reported as clean).

```text
ON-ACQUIRE(t, L, mode, blocking):
    if not blocking: return
    for each H held by t (in mode m_H), H ≠ L:
        new ← OBSERVE-EDGE(H → L, thread t, modes (m_H, mode),
                           guards = exclusively-held(t) − {H, L})
        if new:
            path ← BFS(L ⇝ H)                    ▷ shortest path back
            if path: REPORT-POTENTIAL(cycle = H → L → … → H)
```

Cost: O(V + E) per *new* edge; repeated observations are O(1) except filter
re-evaluation for edges that belong to a reported cycle.

**Filters (the first two from Agarwal et al., IBM J. R&D 2010)** — findings are
kept but marked *filtered* (severity info):

* *single-thread cycle*: every edge was observed only in the same single thread;
* *gate lock*: some lock G was held exclusively during every observation of every
  edge (a lock held for *reading* does not serialize two readers, so it never
  counts as a gate);
* *shared-mode cycle*: at some lock `L` of the cycle, the incoming edge only ever
  *requested* `L` for reading and the outgoing edge only ever *held* `L` for
  reading. On a reader-preferring rwlock (glibc's default) a read request is
  never blocked by another reader, so the circular wait cannot form at `L`.
  Writer-preferring rwlocks are excluded, because there a read request can
  queue behind a third, waiting writer.

**Cross-run analysis.** Lock-order edges of several sessions are merged using
stable lock keys (annotation or symbol name), with the union of their modes.
Components with cycles are found with Tarjan's SCC algorithm (O(V + E)) and a
shortest cycle is extracted from each; a cycle whose edges come from different
sessions is reported as "only visible by merging runs", and the shared-mode
filter is applied to merged cycles too.

## 4. Contention

Per lock: acquisitions, *contended* acquisitions (the lock was held by another
thread at request time — by our state or by glibc's owner field), total and
maximum wait (measured inside the target between request and acquisition),
hold times. A lock is reported as **excessive contention** when

```
acquisitions ≥ 20  and  contended / acquisitions ≥ 0.5  and  total wait ≥ 100 ms
```

or when a single wait outside any cycle exceeds 500 ms. For rwlocks a request is
*contended* when a conflicting holder existed at request time (a writer, or —
for a write request — any reader). Thresholds are configurable in Settings.
Contention is never reported as a deadlock. A long wait that later turns out to
be part of a stall (§5) is filtered as a symptom of the hang, and restored if
the stall resolves.

## 5. Stall detection (no lock cycle)

On each heartbeat: if no in-band event arrived for `stall_ms` (default 1 s),
every live thread is in an **untimed** wait (mutex or rwlock with a conflicting
holder, join on a live thread, condition variable not signalled, semaphore,
barrier) and no wait-for cycle explains it, report a **stall** with evidence.
If a signal on the same condition variable happened *before* the wait began and
found no waiter, the explanation points to a lost wake-up. For a semaphore the
explanation gives the count the trace implies (initial value + posts −
successful waits). Every wait-for chain that ends at a thread waiting on a
semaphore or condition variable is spelled out ("consumer → producer, which
holds `buffer.mu` and is itself waiting on semaphore `slots`"): because those
primitives have no owner, the chain stops there instead of closing a cycle. The
cause is reported as inconclusive: a wait-for graph cannot say who *should*
have signalled or posted.

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
| Wait-for cycle check | O(chain length) ≤ O(T); O(T + E) depth-first while an rwlock wait exists |
| Lock-order cycle check | O(V + E) per new edge |
| Snapshot | O(T + L + E) |
| Replay to seq n | O(min(n, 5 000)) after a one-time O(N) index build |
| Cross-run merge | O(Σ edges) + Tarjan O(V + E) |
