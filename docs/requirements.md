# Requirements, scope and acceptance criteria

## Scope

**In scope:** dynamically linked Linux (glibc, x86-64) programs written in C or
C++ that synchronize with POSIX threads; live monitoring of one process per
session; offline analysis of recorded traces.

**Out of scope:** data-race detection, kernel locks, other operating systems,
languages with their own runtimes (Java, Go), multi-process synchronization.

## Supported primitives

| Primitive | Support |
|---|---|
| `pthread_mutex_lock / unlock / trylock / timedlock / clocklock` | full (request, acquisition, failure, release, errors) |
| mutex types normal, recursive, error-checking, adaptive, robust | recursion counts, EDEADLK/EPERM/EBUSY, EOWNERDEAD |
| `pthread_mutex_init / destroy` | lock identity, address reuse, EBUSY |
| `pthread_rwlock_rdlock / wrlock / tryrdlock / trywrlock / timedrdlock / timedwrlock / clockrdlock / clockwrlock / unlock` | shared readers and one writer; a waiting writer has a wait-for edge to every reader (read-to-write upgrade = self-loop); EDEADLK, misuse, lock-order edges with modes |
| `pthread_rwlock_init / destroy` | lock identity (same id space as mutexes), preference kind, destroy while in use |
| `pthread_cond_wait / timedwait / clockwait / signal / broadcast` | release/re-acquire of the mutex, lost signals, cancellation inside wait |
| `sem_wait / trywait / timedwait / clockwait / post`, `sem_init / destroy` | no owner, so no wait-for edges; count accounting (initial value + posts − waits), posts with no waiter, stall detection and explanation, misuse (EOVERFLOW, EINVAL, destroy with waiters) |
| `pthread_create / join / detach / exit / cancel`, thread return | lifecycle, join edges in the wait-for graph |
| `pthread_barrier_wait` | thread state only |
| `pthread_setname_np` | thread names |
| C++ `std::thread`, `std::mutex`, `std::lock_guard`, `std::scoped_lock`, `std::condition_variable`, `std::shared_mutex` | via the same pthread calls (`std::shared_mutex` is a `pthread_rwlock_t`) |
| spinlocks, `sem_open` (named semaphore creation), futex/atomic locks | **not supported** (see `limitations.md`) |

## Functional requirements

| # | Requirement | Implemented in |
|---|---|---|
| F1 | Launch a supported scenario or a compiled executable as a monitoring session; show session id, executable, start time, status, exit status | `services/session.py`, Test Lab, Overview |
| F2 | Collect thread creation/start/exit, lock request/acquire/release (mutexes and rwlocks), failures and timeouts, condvar wait begin/end, semaphore wait begin/end and posts, join begin/end, monitoring errors and loss indicators; never confuse request with acquisition | `collector.c`, `tl_wire.h` |
| F3 | Maintain thread lifecycle and states, lock ownership (shared reader sets for rwlocks), semaphore counts, outstanding waits, lock-order history, coverage | `detection/engine.py` |
| F4 | Build the current wait-for graph and the historical lock-order graph; detect and classify cycles | `detection/graphs.py`, `engine.py` |
| F5 | For each problem show classification, threads, locks, event sequence, cycle, timestamps, explanation, suggestions, limitations | `diagnostics/findings.py`, Diagnostics page |
| F6 | Event explorer with filters, event details, previous/next navigation, reconstructed graph at a selected event | Event Explorer, Replay |
| F7 | Run the corrected program and compare completion, findings, event counts, contention and time | Replay & Compare, `services/compare.py` |
| F8 | Export events and diagnostic reports | `/api/sessions/{id}/export` (CSV, JSON, NDJSON, raw, report) |
| F9 | Cross-run lock-order analysis | `/api/analysis/lock-order`, Replay & Compare → Cross-run |
| F10 | Settings: theme, reduced motion, 2D/3D, event display, collector options, analysis thresholds, retention | Settings page, `/api/settings` |

## Non-functional requirements

| # | Requirement | Measure |
|---|---|---|
| N1 | Monitoring must not block the target indefinitely | lock-free ring; bounded back-pressure (`THREADLENS_BLOCK_US`); socket send timeout |
| N2 | Loss must be visible | counters in heartbeats/EVENTS_LOST; verdict becomes inconclusive |
| N3 | Conservative diagnosis | classification rules in `algorithms.md`; tests for each downgrade path |
| N4 | Safe execution of untrusted programs | `security.md` |
| N5 | Responsive UI under load | ≤10 Hz throttled snapshots, selector-based stores, virtualized event list, bounded queues |
| N6 | Accessibility | reduced-motion support, 2D alternative to 3D, keyboard navigation in replay, focus rings, ARIA roles/labels, text contrast tokens |
| N7 | Reproducible setup | pinned dependency versions, `scripts/setup_linux.sh`, `frontend/package.json` |

## Acceptance criteria (status tracked in `README.md`)

1. Backend and frontend start.
2. A real instrumented Linux test program is launched.
3. Real thread and synchronization events are collected.
4. Thread activity is displayed from backend data.
5. Lock ownership and waiting relationships are tracked.
6. The intentional deadlock is detected.
7. The dependency cycle is highlighted.
8. The supporting event evidence is shown.
9. The event history can be inspected.
10. The graph can be reconstructed at a selected event.
11. The corrected program runs.
12. Both executions are compared.
13. Diagnostic and event data can be exported.
14. Automated tests pass.
15. The interface is usable with 3D effects disabled.
