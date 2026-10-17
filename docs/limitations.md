# Limitations and future scope

Stated plainly, so that every claim made in the demo can be defended.

## What ThreadLens cannot see

| Not observed | Why | Consequence |
|---|---|---|
| `pthread_spin_*` | not intercepted in this version | a deadlock involving a spinlock is invisible (the spinning thread looks busy, not blocked) |
| `sem_open` / `sem_close` / `sem_unlink` (named semaphores) | creation is not intercepted | waits and posts on them **are** observed, but the initial count is unknown, so the count is not tracked and a wait without a matching post is not flagged |
| Semaphores posted by another process (`pshared = 1`) | only one process is monitored | the count is not tracked; waits still count towards stall detection |
| Locks built from atomics/futexes, lock-free code | no API call to intercept | invisible |
| glibc-internal locks (stdio, malloc arenas, dynamic loader) | internal calls bypass the PLT | invisible (they very rarely participate in application deadlocks) |
| Statically linked programs | `LD_PRELOAD` does not apply | collector never connects; the session reports this |
| Child processes (`fork`/`exec`) | collector disables itself in children | only the launched process is analysed |
| Process-shared mutexes between processes | only one process is monitored | ownership by another process is unknown |
| `pthread_timedjoin_np`, `pthread_tryjoin_np` | not intercepted | no join edge for those calls |
| Data races | out of scope (use TSan/Helgrind) | — |

## Analysis limitations

* **Confirmed** means confirmed *under the observed synchronization model*: an
  unlock performed by un-instrumented code (e.g. a statically linked helper
  library) could in principle break a reported cycle. Coverage problems that
  ThreadLens *can* see (loss, gaps, anomalies) always prevent confirmation.
* **Potential deadlocks are predictions.** The lock-order graph does not prove
  that the conflicting code paths can run concurrently (no happens-before or
  sync-preserving analysis as in SPDOffline). Two classic false-positive filters
  are applied (single-thread cycles, gate locks); other false positives are
  possible, e.g. when the program's logic guarantees phases never overlap.
* **False negatives of prediction.** Only orders that occurred in the observed
  executions are known; an inversion on a path that never ran is missed. Merging
  several runs (cross-run analysis) reduces but does not remove this.
* **Condition-variable and semaphore hangs** cannot be attributed to a specific
  missing signaller or poster: neither primitive has an owner, so waiting on one
  adds no wait-for edge. They are reported as *stalls* with an inconclusive
  cause, and the explanation shows any wait-for chain that ends at such a wait
  (e.g. "consumer → producer, which holds `buffer.mu` and waits on semaphore
  `slots`"). A deadlock that closes only through a semaphore — the classic
  `sem_wait` inside the critical section — is therefore a *stall*, never a
  *confirmed deadlock*. Edges for re-acquiring the mutex after a signal are
  *inferred* and only produce *potential* findings.
* **Read-write locks.** A writer waits for every current reader (an AND-wait),
  so the wait-for graph is searched depth-first while an rwlock wait is
  outstanding. glibc does not record which threads hold read locks, so reader
  sets come only from observed events; a `pthread_rwlock_unlock` by a thread
  that holds nothing (undefined behaviour) makes every later cycle through
  that lock *inconclusive*. On writer-preferring rwlocks
  (`PTHREAD_RWLOCK_PREFER_WRITER_NONRECURSIVE_NP`) a reader queues behind
  waiting writers inside glibc; those edges are *inferred* and only produce
  *potential* findings. Read-to-write upgrades are reported as self-deadlocks.
* **Lock-order cycles through read locks.** A link where a lock is only ever
  held and requested for reading cannot block on a reader-preferring rwlock
  (glibc's default), so such cycles are *filtered* ("shared-mode cycle"). Locks
  held for reading never count as gate locks.
* **Stall detection is time-based** (default 1 s of silence with every thread in
  an untimed wait). A thread spinning in user code without pthread calls is not
  "waiting", so it prevents a stall report rather than causing a false one.
* **Contention thresholds** are heuristics; wait times include a small collector
  overhead.
* **Owner cross-check** reads glibc's private `__data.__owner` field without
  locking: occasional benign mismatches are expected and are not used to decide
  anything.
* **glibc/x86-64 specific details**: mutex kind and owner come from glibc's
  `pthread_mutex_t` layout, the rwlock preference kind and current writer from
  `pthread_rwlock_t.__data.__flags` / `__cur_writer`; the versioned
  condition-variable symbols are resolved for x86-64 (`GLIBC_2.3.2`). The
  semaphore count shown in events is read with `sem_getvalue` just before the
  call and is informational only; the analysis uses its own count
  (initial value + posts − successful waits).

## Collection limitations

* **Overhead.** Every intercepted call costs a few hundred nanoseconds plus
  back-pressure when the backend is slower than the program (the default budget
  is 100 ms per event, see `results.md`). Lock-bound programs can slow down
  noticeably; timing-sensitive bugs may hide (probe effect).
* **Event loss** is possible when `block_us` is small or the backend stalls; it
  is counted and makes verdicts inconclusive.
* **Thread names** are at most 15 characters (kernel limit); lock annotations at
  most 23.
* **Symbolization** needs a non-stripped executable (`-g` for file:line).
  Call sites in shared libraries (e.g. libstdc++'s condition variable) are shown
  as addresses.
* **Deadlocked threads cannot be "unstuck".** The supervisor terminates the
  whole process group; individual blocked threads are never cancelled or
  unwound, because their locks and invariants would be left inconsistent.

## Replay

Event replay re-applies recorded events to the analysis engine. It does **not**
re-execute the program, cannot show memory or registers, and cannot explore
other interleavings. For execution replay use `rr`.

## Environment notes

* Developed and evaluated on **WSL2** (Ubuntu 26.04, kernel 6.18). WSL2 runs a
  real Linux kernel, but timing (scheduling, timers) can differ from bare metal,
  so measured overheads should be treated as indicative for this machine.
* The tool comparison (ThreadSanitizer, Helgrind) uses the same scenario programs;
  it shows qualitative differences on controlled bugs, not performance on real
  applications.

## Future scope

1. Intercept spinlocks and `sem_open`; model writer-preferring rwlock queues
   from glibc's internal state instead of inferring them.
2. eBPF/uprobe collector producing the same records (no restart, static
   binaries) for systems where root is available.
3. Happens-before / sync-preserving filtering of lock-order cycles (SPD-style)
   to reduce false potentials, and DeadlockFuzzer-style directed scheduling to
   try to *confirm* a predicted inversion.
4. Multi-process sessions (follow `fork`, process-shared mutexes).
5. Faster ingest (native decoder/analyzer core) to remove back-pressure on very
   lock-heavy programs.
6. Integration with `rr`: open the recorded execution at the event selected in
   the timeline.
7. User study measuring diagnosis time with and without ThreadLens.
