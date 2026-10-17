# Testing and evaluation

## Running the tests

```bash
# Linux / WSL2, from the project root
scripts/setup_linux.sh                        # builds, then runs the collector's ctest suite
~/.threadlens/venv/bin/python -m pytest -q backend   # unit + API + real-program integration tests
cd frontend && npm run typecheck && npm test  # TypeScript checks and frontend unit tests
python scripts/evaluate.py --reps 5           # measured evaluation → docs/results.md
```

## Test inventory

### Collector (C, `ctest`)

| Test | What it proves |
|---|---|
| `collector_trace_invariants` | `tl_selftest` (6 threads: mutexes, recursive mutex, try-locks, timed locks, condvar ping-pong, barrier, `pthread_exit`, read-write locks with recursive reads and try/timed variants, a counting semaphore with `sem_trywait`/`sem_timedwait`) produces a trace in which sequence numbers are contiguous, every blocking acquisition follows its request, **no thread ever acquires a mutex the trace says another thread holds** (the ordering rule), **no write acquisition of an rwlock overlaps another holder and no read acquisition overlaps a writer**, semaphore wait begin/end pair up and **a semaphore never passes more waits than its initial value plus the posts recorded before them**, thread starts and exits balance and heartbeats exist — checked by `tl_tracecheck`; the program also checks its own counters (rwlock writes/reads, final semaphore count) |
| `collector_passthrough_without_output` | with `LD_PRELOAD` but no output configured the program runs normally |
| `collector_deadlock_trace` | a deadlocked process (killed by its watchdog) still delivers its events and heartbeats |

### Backend (pytest, 124 tests)

| File | Coverage |
|---|---|
| `test_engine_deadlock.py` | suspected → confirmed by heartbeat; heartbeat before the cycle does not confirm; end of stream confirms; evidence ordering and roles; wait-for edges; contention is not deadlock; 3-thread cycle; self-deadlock (normal) vs recursive vs error-checking; join + lock cycle with a not-yet-started target; timed cycle → potential, then resolved; lost events → inconclusive; loss after formation → inconclusive; inferred condvar re-acquire edge → potential |
| `test_engine_state.py` | lifecycle states and parents; recursion counts; condvar release/re-acquire; lock-order inversion → potential; try-lock adds no edge; single-thread and gate-lock filters; 3-lock order cycle; contention classification; stall / lost wake-up; timed waits are not stalls; failed unlocks restore ownership; non-owner unlock of a normal mutex; abandoned locks; anomalies; process exit |
| `test_engine_rwlock_sem.py` | **rwlocks:** readers share, a waiting writer has an edge to every reader, a reader waits for the writer, recursive reads do not wait; a cycle through a writer blocked by two readers is found (DFS), suspected → confirmed, and survives the other reader leaving; a writer blocked by a running reader is not a deadlock; read-to-write upgrade = self-deadlock; writer relock → EDEADLK misuse without an edge; timed rwlock cycle → potential; writer-preferring reader queue → inferred edge → potential; foreign unlock → misuse and later cycles inconclusive; exit holding a read lock abandons writers; impossible overlapping acquisition → anomaly. **Lock order:** modes recorded, try variants add no edge, read-only cycles filtered, read/write inversion → potential, mixed mutex/rwlock cycle, read-held gate does not filter. **Semaphores:** count accounting, no wait-for edges, banked posts, try/timeout outcomes, wait without post → anomaly (not for process-shared), destroy with waiters, failed post undone, stall with the ownerless-chain explanation, long-wait contention filtered by the stall and restored when it resolves, timed waits are not stalls, event descriptions |
| `test_wire_graphs_ingest.py` | binary encode/decode round trip with arbitrary chunking (incl. rwlock and semaphore records); bad magic/type/size rejected; strict import schema; sequence guard (pass-through, duplicates, reordering, gaps); functional-graph cycles; DFS cycle search with several successors (self-loop first, diamonds are not cycles); shortest cycle per edge via SCCs; lock-order cycle search and modes; Tarjan SCC |
| `test_api_replay.py` | health (supported/unsupported primitives); import → analyze → query (filters, search, pagination); rwlock + semaphore import: cycle, event descriptions and metadata, lock readers, semaphores, timeline lanes, **replay equals incremental analysis on the DFS path**; malformed input rejected (path tricks, unknown types/fields, disallowed executables); raw export → re-import gives identical findings; **replay at every sequence number equals incremental analysis**; comparison and cross-run merge with ASLR-shifted addresses; cross-run read-only cycle filtered; timeline; settings validation; deletion; WebSocket for finished sessions |
| `test_process_and_collector.py` | executable validation (outside dirs, scripts, symlink escape, non-executable); argument validation; timeout kills the whole process group; real traces: contiguous seq, request/acquire pairing, condvar begin/end pairing, **release precedes next acquire for every mutex**, deadlocked process delivers events, C++ `std::scoped_lock` is intercepted (try-lock records), pass-through |
| `test_live_scenarios.py` | **real programs through the live API**: 20 catalog scenarios (all except the cross-run pair and stress, which have dedicated tests) produce the ground-truth verdict, required classes appear, forbidden ones do not, completion matches, no events lost, cycle length and detection latency for deadlocks; symbolized evidence; cross-run inversion from two real runs; stopping a running session; loss downgrades the verdict; live WebSocket stream (hello → updates → end); uninstrumented baseline |

### Frontend

`npm run typecheck` (strict TypeScript) and `npm test` (Vitest: graph model
construction, cycle ordering, rwlock read-held edges and writer wait labels,
semaphore nodes without a holder, diffing helpers). End-to-end checks were done
in a browser against the running backend (landing page, Test Lab, original + fix
run, graph, diagnostics, replay, events, threads; for the rwlock deadlock and the
semaphore stall: resource graph, semaphore node panel, thread wait cells).

## Test cases and expected outcomes (ground truth)

The ground truth lives in `scenarios/catalog.json` and is used by both
`test_live_scenarios.py` and `scripts/evaluate.py`.

| Scenario | Program | Expected verdict | Completes? |
|---|---|---|---|
| bank-deadlock | `bank_deadlock 5` | confirmed deadlock (2-cycle) | no — terminated |
| bank-ordered | `bank_ordered 5` | no issue | yes |
| bank-deadlock-cpp | `bank_deadlock_cpp 5` | confirmed deadlock (2-cycle) | no |
| bank-scoped-cpp | `bank_scoped_cpp 5` | no issue | yes |
| healthy | `healthy 4` | no issue | yes |
| contention | `contention 8 15 2000` | excessive contention | yes |
| producer-consumer | `producer_consumer 60` | no issue | yes |
| lock-order-both | `lock_order both` | potential deadlock | yes |
| lock-order-ab / -ba | `lock_order ab` / `ba` | no issue each; potential when merged | yes |
| lifecycle | `lifecycle` | no issue | yes |
| misuse | `misuse` | API misuse | yes |
| self-deadlock | `misuse self-deadlock` | confirmed deadlock (self-loop) | no |
| philosophers | `dining_philosophers` | confirmed deadlock (5-cycle) | no |
| philosophers-fixed | `dining_philosophers --fixed` | no issue | yes |
| join-deadlock | `join_deadlock` | confirmed deadlock (join + lock) | no |
| lost-wakeup | `lost_wakeup` | stall (inconclusive cause) | no |
| rwlock-deadlock | `rwlock_deadlock` | confirmed deadlock (2-cycle through a writer blocked by two readers) | no |
| rwlock-ordered | `rwlock_deadlock --fixed` | no issue | yes |
| rwlock-upgrade | `rwlock_deadlock upgrade` | confirmed deadlock (self-loop: read-to-write upgrade) | no |
| sem-stall | `bounded_buffer_sem` | stall (semaphore wait inside the critical section; no contention finding) | no |
| sem-ordered | `bounded_buffer_sem --fixed` | no issue | yes |
| stress | `stress 8 20000 16 30 200` | no issue (or inconclusive if events are lost) | yes |

## Evaluation methodology (`scripts/evaluate.py`)

* **Accuracy.** Every scenario is run *R* times (default 5) through the live
  backend. For each classification C, a run is a *positive* if the ground truth
  lists C in `must`, a *negative* if it lists C in `must_not`; precision and
  recall are computed per class. Session verdict accuracy and completion
  accuracy are also reported.
* **Detection latency.** For confirmed deadlocks: backend `CLOCK_MONOTONIC` at
  confirmation minus the target's timestamp of the cycle-forming event. Both are
  the same kernel clock, so the value includes collection, transport and
  analysis. The in-trace confirmation delay (event → confirming heartbeat) is
  reported separately.
* **Overhead.** The lock-bound `stress` program (T threads × 20 000 lock
  operations, 16 locks, 30 % nested) is timed by the program itself in four
  modes: baseline (no collector), passive (collector loaded, no output),
  trace file, live (full pipeline). Median of *R* runs, T ∈ {1, 2, 4, 8}.
  Thread scaling is read from the same table.
* **Event loss.** `stress` with tiny rings and different back-pressure
  budgets; reports delivered/lost events and the resulting verdict.
* **Throughput.** Backend ingest rate = events / wall time in live mode, plus the
  raw decoder/analyzer rate measured offline.
* **Existing tools.** The ThreadSanitizer builds of the scenarios are run with
  `detect_deadlocks=1`; Helgrind is run when valgrind is installed.
* **Diagnosis time (user study).** Not performed: it needs participants.
  A protocol is proposed below.

Measured results: **[`results.md`](results.md)** (generated; raw data in
`results/evaluation.json`).

## False positives, false negatives and uncertainty

* No false *confirmed* deadlocks were observed in the evaluation runs (see
  per-class precision in `results.md`). The confirmation rule is designed so
  that a false confirmation would require an unlock performed outside the
  intercepted API.
* Potential (lock-order) findings can be false positives when phases can never
  overlap; `lock-order-both` is such a program by construction (its hand-off
  serializes the two orders) and is reported — correctly — as *potential*, not
  confirmed.
* False negatives: inversions on code paths that did not execute; deadlocks
  through unsupported primitives; stalls shorter than the stall threshold.
* Incomplete traces are explicitly reported (coverage panel) and downgrade
  verdicts to *inconclusive* rather than producing a guess.

## Proposed user study (future work)

Within-subjects, 8–12 OS students, four buggy programs (two deadlocks, one
lock-order inversion, one lost wake-up) in counterbalanced order; condition A:
gdb + printf; condition B: ThreadLens. Measure time to a correct root-cause
statement and to a correct fix, plus SUS questionnaire. Hypothesis: ThreadLens
reduces time to root cause for deadlocks; no difference expected for data races
(out of scope).
