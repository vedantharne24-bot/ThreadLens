# Literature review and existing-system comparison

This review positions ThreadLens among existing concurrency tools and research. It
deliberately separates four families that are often conflated:

1. **data-race detectors** (Helgrind, DRD, ThreadSanitizer) — find unsynchronized
   memory accesses;
2. **deadlock detectors / predictors** (GoodLock and its successors, lockdep,
   DeadlockFuzzer, SPDOffline, Dimmunix) — find or predict circular waits;
3. **lock profilers and tracers** (`perf lock`, eBPF/bpftrace) — measure
   contention and timing;
4. **record-and-replay systems** (`rr`) — reproduce a whole execution
   deterministically.

ThreadLens belongs mainly to family 2, borrows instrumentation ideas from 1 and 3,
and offers *event* replay, which is different from the *execution* replay of 4.

---

## 1. Valgrind Helgrind

* **Problem.** Errors in programs using POSIX threads.
* **Approach.** Dynamic binary instrumentation inside Valgrind's virtual CPU.
  Races are found with a happens-before analysis; lock ordering is checked with a
  directed graph of the order in which locks were acquired, updated on every
  acquisition and checked for cycles.
* **Bug classes.** pthread API misuse (unlocking a lock that is not held or is
  held by another thread, destroying a locked mutex, recursively locking a
  non-recursive mutex, a thread exiting while holding locks, error return codes),
  **potential deadlocks from inconsistent lock ordering**, and data races.
* **Debugging support.** Textual reports with stack traces; for a two-lock cycle
  it shows where the original order was established and where it was violated.
* **Limitations (from its manual).** For cycles of more than two locks not all
  locks are shown; condition variables are only partially handled (a signal that
  happens before the wait is invisible); spinlocks are not supported; custom
  primitives built from futexes/atomics need annotations; lock-order checking of
  try-locks is listed as a possible source of false errors.
* **Cost.** "Slowdowns on the order of 100:1 are not unusual."
* **What ThreadLens takes / improves.** The lock-order-graph idea; ThreadLens
  adds a *current* wait-for graph so that an actual hang is confirmed (Helgrind
  reports potentials), records ordered evidence and source lines, ignores
  try-lock acquisitions as lock-order edges by construction, and runs natively
  through `LD_PRELOAD` (no virtual CPU).

Source: Valgrind User Manual, Helgrind — <https://valgrind.org/docs/manual/hg-manual.html>

## 2. Valgrind DRD

* **Problem.** Data races and synchronization errors.
* **Approach.** Dynamic binary instrumentation; happens-before race detection.
* **Bug classes.** Data races, lock contention (only when a hold-time threshold
  such as `--exclusive-threshold` is set), many kinds of POSIX API misuse
  (unlocking unlocked/foreign mutexes, recursive locking of normal mutexes,
  destroying locked objects, signalling without holding the mutex, waiting with
  an unlocked or foreign mutex, exiting while holding locks, invalid joins).
* **Deadlocks.** The manual does not describe lock-order or deadlock detection
  as a DRD feature.
* **Cost.** Memory 1.1–3.6× native; most applications run 20–50× slower.
* **Relevance.** DRD's misuse list guided ThreadLens' API-misuse findings
  (EDEADLK, EPERM, EBUSY, abandoned locks, waiting without the mutex).

Source: Valgrind User Manual, DRD — <https://valgrind.org/docs/manual/drd-manual.html>

## 3. ThreadSanitizer (TSan)

* **Problem.** Data races in C/C++ (and Go); an optional deadlock detector.
* **Approach.** Compile-time instrumentation (`-fsanitize=thread`) plus a
  runtime library; the deadlock detector is enabled with
  `TSAN_OPTIONS=detect_deadlocks=1` and reports
  `lock-order-inversion (potential deadlock)` with the stacks of the
  acquisitions that form a cycle (`second_deadlock_stack=1` adds the second
  stack).
* **Limitations.** Requires recompilation; reports *potential* inversions; the
  upstream test `must_deadlock.cpp` is marked as an expected failure because the
  detector checks cycles only after `pthread_mutex_lock` returns, so an actual
  hang can go unreported.
* **Relevance.** TSan is the comparison baseline in `docs/results.md` because it
  is available on this machine. ThreadLens does not detect data races.

Sources: LLVM compiler-rt TSan tests
(<https://llvm.googlesource.com/llvm-project.git/+/refs/heads/main/compiler-rt/test/tsan/must_deadlock.cpp>),
example reports (<https://gitlab.isc.org/isc-projects/bind9/-/issues/1494>).

## 4. Linux `perf lock`

* **Problem.** Analysing lock behaviour and statistics.
* **Approach.** Subcommands `record`, `report`, `script`, `info` and
  `contention`; `contention -b` collects statistics with BPF; options group
  results by thread, lock address, owner, cgroup or call stack.
* **Scope.** The lock types it lists (spinlock, rwsem, rtmutex, mutex,
  semaphore…) and its use of kernel symbols show that it targets **kernel**
  locks; it does not reconstruct user-space pthread ownership or wait-for cycles.
* **Relevance.** Shows what contention metrics are useful (wait time, owner,
  call stack); ThreadLens reports comparable per-lock statistics for pthread
  mutexes and keeps contention strictly separate from deadlock.

Source: `perf-lock(1)` — <https://man7.org/linux/man-pages/man1/perf-lock.1.html>

## 5. eBPF and bpftrace

* **Approach.** Attach probes to user functions (uprobes/uretprobes) or to
  glibc's built-in SystemTap (SDT) probes, which include mutex entry/acquire
  probes, and aggregate in the kernel.
* **Strengths.** No target restart; works for statically linked programs
  (uprobes on the binary); low overhead.
* **Limitations.** Requires root or CAP_BPF and a BPF-capable kernel; on glibc
  ≥ 2.34 the pthread functions live in `libc.so.6`; no deadlock analysis by
  itself; not available in the development environment used here (no
  password-less sudo, WSL2).
* **Relevance.** Listed as future work: an eBPF collector could produce the same
  64-byte records and reuse the whole backend.

Source: bpftrace manual (`bpftrace(8)`), glibc `nptl/DESIGN-systemtap-probes.txt`.

## 6. Linux kernel lockdep

* **Approach.** The *runtime locking correctness validator* tracks lock
  **classes** (all instances of "the inode lock" form one class) and the
  dependencies between classes; "L1 → L2" means a task acquired L2 while
  holding L1 — "that dependency just means the order ever happened". Cycles and
  IRQ-safety violations produce a report ("splat").
* **Relevance.** The idea that an ordering *ever happened* is exactly the
  semantics of ThreadLens' historical lock-order graph; the idea of classes
  motivates matching locks across runs by stable names (annotation or global
  symbol) instead of addresses.

Source: `Documentation/locking/lockdep-design.rst` in the Linux kernel tree.

## 7. GoodLock and deadlock-potential detection (IBM)

Agarwal, Bensalem, Farchi, Havelund, Nir-Buchbinder, Stoller, Ur and Wang,
"Detection of deadlock potentials in multithreaded programs", *IBM Journal of
Research and Development*, 2010 (DOI 10.1147/JRD.2010.2060276).

* Builds on Havelund's GoodLock algorithm: nested acquisitions are recorded in a
  lock graph; a cycle signals a possible deadlock.
* Adds filters for false alarms: cycles within one thread, cycles protected by a
  **gate lock**, and cycles whose segments cannot run in parallel; merges lock
  graphs **from several runs**; uses static analysis to reduce dynamic overhead.
* **ThreadLens implements** the lock graph, the single-thread filter and the
  gate-lock filter (as *filtered* findings, still visible), and the cross-run
  merge (keyed by stable lock names). It does **not** implement the
  segment/happens-before filter (documented in `limitations.md`).

Sources: <https://research.ibm.com/publications/detection-of-deadlock-potentials-in-multithreaded-programs>,
<https://www3.cs.stonybrook.edu/~stoller/papers/deadlock-IBM-2010.pdf>

## 8. Randomized confirmation of real deadlocks (DeadlockFuzzer)

Joshi, Park, Sen and Naik, "A randomized dynamic program analysis technique for
detecting real deadlocks", PLDI 2009. Stage 1 predicts candidate deadlocks
from one execution; stage 2 drives a randomized scheduler to make them occur,
so reported deadlocks are real (no false warnings). Prototype for Java.
*Relevance:* ThreadLens' "potential" vs "confirmed" split mirrors the two
stages, but confirmation comes from observing the hang, not from scheduling.

Source: <https://www.cis.upenn.edu/~mhnaik/papers/pldi09b.pdf>

## 9. Deadlock immunity (Dimmunix)

Jula, Tralamazza, Zamfir and Candea, "Deadlock Immunity: Enabling Systems to
Defend Against Deadlocks", OSDI 2008. After a deadlock occurs, its signature is
recorded and future executions avoid the same pattern; implemented for Java and
POSIX threads, evaluated on MySQL, SQLite, JBoss and others with modest overhead.
*Relevance:* a deployment-time *avoidance* approach; ThreadLens is a
development-time *diagnosis* tool. Dimmunix-style signatures (lock sets + call
sites) are what ThreadLens' evidence already contains.

Source: <https://www.usenix.org/conference/osdi-08/deadlock-immunity-enabling-systems-defend-against-deadlocks>

## 10. Sound dynamic deadlock prediction (SPDOffline / SPDOnline)

Tunç, Mathur, Pavlogiannis and Viswanathan, "Sound Dynamic Deadlock Prediction
in Linear Time", PLDI 2023. Shows that sound and complete prediction is
intractable in general, introduces *sync-preserving* deadlocks and *abstract
deadlock patterns*, and gives SPDOffline (linear time per abstract pattern) and
SPDOnline (online, two-thread patterns). *Relevance:* the state of the art for
**prediction** soundness; ThreadLens' lock-order findings are explicitly
unsound "potentials", and SPD-style filtering is listed as future work.

Source: <https://www.comp.nus.edu.sg/~umathur/publications/2023-pldi-tunc-syncp-deadlock-prediction/>

## 11. Record and replay: rr

O'Callahan, Jones, Froyd, Huey, Noll and Partush, "Engineering Record and Replay
for Deployability", USENIX ATC 2017. rr records low-parallelism Linux
workloads entirely in user space on stock hardware/kernels and replays them
deterministically under gdb; threads are serialized onto a single core during
recording.
*Relevance:* rr gives **execution** replay of everything (registers, memory),
not a concurrency diagnosis. ThreadLens gives **event** replay of the
synchronization trace (no re-execution), cheap enough to keep for every run.
The two are complementary: record a failing run with rr, diagnose the
synchronization structure with ThreadLens.

Source: <https://www.usenix.org/conference/atc17/technical-sessions/presentation/ocallahan>

## 12. Visualization of thread behaviour: ThreadRadar

Moseler, Kreber and Diehl, "The ThreadRadar visualization for debugging
concurrent Java programs", *Journal of Visualization* 2022
(DOI 10.1007/s12650-022-00843-w; earlier VINCI 2021 paper). Combines statistical
profiling, clustering of threads and glyph visualizations embedded in an IDE
next to the source code; a small user study reported formative results.
*Relevance:* evidence that placing thread information next to code helps; it
visualizes *activity*, not lock causality. ThreadLens visualizes *causality*
(who waits for whom) and links every node to its recorded events and source line.

Sources: <https://link.springer.com/article/10.1007/s12650-022-00843-w>,
<https://www.st.uni-trier.de/moseler/publications/jov22-the-threadRadar-visualization_version-of-record.pdf>

## 13. Other related work (brief)

* **UNDEAD** (ASE 2017) and **AirLock** (ICSE 2020) target low-overhead deadlock
  detection/prediction in production; AirLock's authors note that earlier
  predictors such as UnDead and MagicLock have large overheads.
* **Intel VTune "Threading"/locks-and-waits analysis** and **Trace Compass /
  LTTng** offer timeline visualizations of waits; they do not build wait-for
  cycles from pthread ownership.

---

## Comparison summary

| Capability | Helgrind | DRD | TSan | perf lock | rr | **ThreadLens** |
|---|---|---|---|---|---|---|
| Data races | ✔ | ✔ | ✔ | – | (replay) | – (out of scope) |
| Potential deadlock (lock order) | ✔ | – | ✔ | – | – | ✔ (+ try-lock aware, filters, cross-run) |
| Confirmed actual deadlock (wait-for cycle) | – | – | – (hang) | – | (replay the hang) | ✔ |
| Stall without lock cycle (cond var) | – | – | – | – | – | ✔ (inconclusive cause) |
| pthread API misuse | ✔ | ✔ | partial | – | – | ✔ (subset) |
| Contention metrics | – | threshold | – | ✔ (kernel) | – | ✔ (pthread) |
| Needs recompilation | no | no | **yes** | no | no | no |
| Live dashboard | – | – | – | – | – | ✔ |
| Evidence linked to events + source lines | stacks | stacks | stacks | – | debugger | ✔ |
| Replay | – | – | – | – | **execution** | **event** |
| Typical slowdown | ~100× (docs) | 20–50× (docs) | recompiled build | low | single core | measured in `results.md` |

## Validated contribution statement

None of the surveyed tools introduces the combination ThreadLens offers, but each
*ingredient* is established. The defensible contribution is therefore:

> **An evidence-linked, time-aware concurrency debugging workflow for Linux
> pthread programs** that integrates (i) ordering-preserving live collection via
> `LD_PRELOAD`, (ii) a conservative classification separating confirmed
> wait-for cycles, potential lock-order inversions, contention, stalls and
> inconclusive cases, (iii) graph reconstruction at any recorded event, and
> (iv) original-versus-corrected comparison.

ThreadLens does **not** claim a new deadlock-detection algorithm.
