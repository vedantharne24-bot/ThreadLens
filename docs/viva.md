# Viva preparation — Operating Systems questions and answers

Answers refer to the actual implementation so they can be shown on screen.

## A. Processes, threads and synchronization basics

**1. What is the difference between a process and a thread?**
A process owns an address space, file descriptors and other resources; a thread
is a schedulable execution context (registers, stack, TID) inside a process.
Threads of one process share memory, which is why they need synchronization.
On Linux both are kernel *tasks* created by `clone()`; threads share the
`mm_struct`. ThreadLens identifies threads by kernel TID (`gettid`) and by its
own index (T0 = main).

**2. What is a mutex and how does glibc implement `pthread_mutex_lock`?**
A mutual-exclusion lock with an owner. glibc's NPTL uses an atomic
compare-and-swap on a lock word in user space; only when the lock is contended
does it call the `futex(FUTEX_WAIT)` system call to sleep, and unlock uses
`FUTEX_WAKE`. So an uncontended lock costs no system call. glibc also stores the
owner TID in `__data.__owner`, which ThreadLens reads to cross-check ownership.

**3. Mutex types?**
Normal (no checks; relocking self-deadlocks), recursive (owner may relock;
count), error-checking (relock → `EDEADLK`, foreign unlock → `EPERM`), robust
(if the owner dies, the next locker gets `EOWNERDEAD`). The `misuse` scenario
demonstrates each; ThreadLens reads the type from `__data.__kind`.

**4. What does `pthread_cond_wait` do with the mutex?**
It atomically releases the mutex and sleeps; before returning it re-acquires
the mutex. That is why ThreadLens records `COND_WAIT_BEGIN` (mutex released)
and `COND_WAIT_END` (mutex re-acquired), and why the predicate must be checked in
a `while` loop (spurious wake-ups, signals that happened earlier) — see
`producer_consumer.c` (correct) vs `lost_wakeup.c` (bug).

**5. Signal vs broadcast?**
`signal` wakes at least one waiter, `broadcast` wakes all. A signal with no waiter
is lost — condition variables have no memory, which is the cause of the
lost-wake-up stall.

**6. What is a barrier?**
All N threads block until the N-th arrives. The scenarios use a *timed gate*
(mutex + condvar with timeout) instead of a plain barrier so that the corrected
programs, where one thread cannot arrive, still finish.

## B. Deadlock theory

**7. Define deadlock. What are the Coffman conditions?**
A set of threads each waiting for an event only another member can cause.
Necessary conditions: mutual exclusion, hold and wait, no preemption, circular
wait. ThreadLens prints these for every confirmed deadlock.

**8. Prevention vs avoidance vs detection?**
*Prevention* breaks a condition by design (e.g. global lock ordering breaks
circular wait — the `bank_ordered` fix; acquiring all locks at once —
`std::scoped_lock`). *Avoidance* decides at runtime whether granting a request is
safe (Banker's algorithm; needs maximum claims in advance). *Detection* lets
deadlocks happen and finds them (wait-for-graph cycles) — what ThreadLens does —
followed by *recovery* (here: terminating the isolated test process).

**9. Resource-allocation graph vs wait-for graph?**
RAG: thread → resource (request) and resource → thread (assignment). For
single-instance resources, collapsing resources gives the wait-for graph
(thread → thread). With single-instance resources, a cycle is necessary and
sufficient for deadlock. ThreadLens shows both (Lock Graph → Resources / Wait-for).

**10. Why is a cycle sufficient here but not with multi-instance resources?**
With several instances (e.g. a semaphore with count 3), another instance may be
released by a thread outside the cycle. Mutexes are single-instance, so a cycle
of blocking waits is a deadlock.

**11. How does ThreadLens detect cycles efficiently?**
Each blocked thread waits for exactly one thing, so the wait-for graph has
out-degree ≤ 1 (a functional graph). Following successors from the thread that
just started waiting finds any cycle in O(chain length). For the lock-order graph
(any out-degree) it runs a BFS back from the new edge (O(V+E)); for merged runs,
Tarjan's SCC.

**12. What is a potential deadlock / lock-order inversion?**
Thread 1 takes A then B, thread 2 takes B then A. If they interleave badly,
deadlock. ThreadLens' lock-order graph records "B acquired while holding A";
a cycle is reported as *potential*. `lock_order both` completes in every run yet
is reported as potential.

**13. Why are try-locks excluded from the lock-order graph?**
`pthread_mutex_trylock` never blocks, so it cannot be one of the waits in a
circular wait. Including it would give false alarms for correct back-off code
such as `std::lock`.

**14. What is a self-deadlock?**
A thread relocks a normal mutex it already owns: it waits for itself
(self-loop). Error-checking mutexes return `EDEADLK` instead; recursive mutexes
succeed.

**15. Can `pthread_join` deadlock?**
Yes: `join_deadlock.c` — main holds `config_lock` and joins a worker that needs
`config_lock`. The cycle mixes a join edge and a lock edge.

**16. Livelock vs deadlock vs starvation?**
Livelock: threads keep running but make no progress (e.g. polite back-off forever).
Starvation: one thread never gets a resource while others progress. Deadlock:
nobody progresses because of a circular wait. Contention is merely slowness —
ThreadLens never reports it as deadlock.

## C. ThreadLens implementation

**17. How does the collector intercept calls without recompiling?**
`LD_PRELOAD` loads `libthreadlens.so` before other libraries; the dynamic linker
resolves the program's `pthread_mutex_lock` symbol to our wrapper first. The
wrapper records an event and calls the real function from
`dlsym(RTLD_NEXT, "pthread_mutex_lock")`.

**18. Why `dlvsym` for condition variables?**
glibc exports two versions of `pthread_cond_wait` (GLIBC_2.2.5 old layout and
GLIBC_2.3.2). Plain `dlsym` can return the old one, which uses a different
`pthread_cond_t` layout; `dlvsym(..., "GLIBC_2.3.2")` selects the right one.

**19. How do you make sure events are in the right order?**
Each event claims a slot in a lock-free ring with an atomic compare-and-swap; the
slot index *is* the sequence number. Releases are recorded **before** the real
unlock and acquisitions **after** the real lock returns, so a release always has
a smaller sequence number than the next acquisition of that mutex by another
thread. The `tl_tracecheck` test verifies this on real traces.

**20. Why a lock-free ring buffer and a separate flusher thread?**
Application threads must never block on I/O or on a lock owned by the tool
(that could itself deadlock or perturb timing). They only write 64 bytes into
memory; the flusher does the socket writes. The ring is bounded; when it is full
a producer waits at most `THREADLENS_BLOCK_US` and then drops — and the drop is
counted.

**21. What is a heartbeat and why is it needed?**
A periodic out-of-band record carrying "all events with seq < N have been sent".
When a cycle forms at seq f and a heartbeat with N > f arrives with the cycle still
present, no release can be in transit — so ThreadLens confirms the deadlock. An
extra heartbeat is sent ~2 ms after the ring drains, which makes confirmation
fast.

**22. What happens when events are lost?**
The loss counter becomes non-zero; coverage is incomplete; unconfirmed cycles
become *inconclusive*, lock-order cycles found afterwards are inconclusive and
the session verdict is inconclusive (unless a deadlock was confirmed while the
trace was still complete). ThreadLens never guesses with missing data.

**23. How do you get lock names and source lines?**
`nm -S` lists global variables with sizes; a lock address minus the PIE load
bias (sent in the handshake) falls into a symbol (`lock_a`, `accounts+0x28`).
The return address of each call is mapped with `addr2line -f` to
`transfer (bank_transfer.c:34)`. Programs can also name locks explicitly with
`tl_name_lock()`.

**24. What is PIE / ASLR and why does it matter?**
Position-independent executables are loaded at a random base each run
(address-space layout randomization), so addresses differ between runs. ThreadLens
subtracts the load bias before symbolization and matches locks across runs by
name, not address.

**25. How is a deadlocked test program stopped?**
It runs in its own session/process group (`setsid`). After confirmation the
supervisor sends SIGKILL to the whole group (`killpg`). Individual threads are not
cancelled: their locks and data would be left inconsistent, and a thread blocked
in `pthread_mutex_lock` is not at a cancellation point anyway.

**26. What resource limits are applied and how?**
`prlimit` right after spawning: address space 2 GiB, 512 processes, 64 MiB file
size, no core dumps, CPU seconds; plus a wall-clock timeout. These are not a
security sandbox (same user) — see `security.md`.

**27. How does `fork` interact with the collector?**
`pthread_atfork` disables the collector in the child (its copy of the ring has no
flusher and it must not share the socket); exec'd children do not inherit the
`THREADLENS_*` variables.

**28. What does event replay do? How is it different from `rr`?**
It re-applies recorded events 0…n to the deterministic analyzer to show the
graph at event n (with checkpoints every 5 000 events). `rr` records all
nondeterministic inputs and re-executes the program deterministically, giving
memory and registers. ThreadLens never re-executes.

**29. Why is the analyzer written as a pure function of the events?**
So live analysis, replay, imports and tests run the same code, and the replayed
state is provably the live state (tested at every sequence number).

**30. How does the backend avoid being blocked by the dashboard?**
The socket is read by the asyncio loop; the UI receives throttled snapshots
(≤ 10/s) through bounded per-client queues that drop the oldest update; the
database is written by a separate thread.

## D. Evaluation and honesty

**31. How accurate is it?** Quote `results.md`: verdict accuracy and
precision/recall per class over the evaluation runs, zero false confirmed
deadlocks in those runs. Stress the scope: 15+ controlled scenarios, not a
benchmark suite of real applications.

**32. What is the overhead?** Quote the overhead table. Passive interposition is
almost free; the full live pipeline on a deliberately lock-bound program is
dominated by back-pressure to the Python analyzer. Real programs that do work
between lock operations see far less.

**33. Why did ThreadSanitizer not report the bank deadlock?**
TSan's deadlock detector checks for lock-order cycles after a lock is acquired;
in the actual deadlock the second acquisitions never return, so no inversion is
ever recorded — the program just hangs (the upstream test `must_deadlock.cpp` is
marked as an expected failure for this reason). It did report the
*potential* inversion in `lock_order both`. Helgrind behaves the same way (its
lock-order graph is also built from completed acquisitions); its only messages
for the hung programs were "Exiting thread still holds 1 lock" when the
watchdog killed them. ThreadLens confirms the hang from the wait-for graph.

**34. What can ThreadLens not detect?** rwlock/semaphore/spinlock deadlocks,
lock-free bugs, data races, deadlocks in un-executed paths (prediction only
covers observed orders), the cause of condition-variable hangs (reported as
stalls).

**35. Is a successful corrected run a proof of correctness?** No. It shows that
the tested scenario did not reproduce the problem in that execution; other
interleavings may still fail. The lock-order graph of the fixed program having
no cycle is stronger evidence, but still not a proof.

**36. What is novel?** Not the algorithms (GoodLock, wait-for graphs, lockdep
ideas are established). The contribution is the integrated, evidence-linked,
time-aware workflow: ordering-preserving collection, conservative
confirmed/potential/stall/inconclusive classification, replay at any event, and
original-vs-corrected comparison, with measured evaluation.

## E. Quick numbers to remember (from `results.md`)

* Evaluation runs: **90** (18 scenarios × 5); verdict accuracy **100 %**.
* Confirmed-deadlock precision / recall: **100 % / 100 %** (25 TP, 0 FP, 0 FN, 65 TN).
* Median end-to-end detection latency: **2.2 ms** (p95 2.9 ms, max 3.3 ms); in-trace
  confirmation delay median 2.0 ms.
* Overhead (8 threads, lock-bound stress): passive **1.05×**, trace **1.9×**, live **56×**
  (lossless, bounded by ~100 k events/s backend ingest). 1 thread: live 7.3×.
* Without back-pressure (`block_us=0`) the program is not slowed but ~87 % of stress events
  are lost and the verdict is correctly *inconclusive*.
* Backend analysis rate: ~370 k events/s offline (decoder ~0.9 M records/s); live ingest
  ~100–130 k events/s including SQLite.
* ThreadSanitizer and Helgrind: both hung without a deadlock report on all 3 actual
  deadlocks; both reported the single-run lock-order inversion; Helgrind flagged the misuse
  program with 13 errors (its strength: API misuse and data races).

**Viva tip:** if asked "why is live overhead so high on stress?", answer: by design the
collector applies back-pressure so that no event is lost; the bottleneck is the Python
analyzer, not the interposition (passive ≈ 1.0×). A native analyzer core is listed as
future work, and `block_us=0` trades completeness for zero slowdown.
