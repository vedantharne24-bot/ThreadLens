"""The ThreadLens analysis engine.

``Analyzer`` consumes decoded records in sequence order and maintains:

* thread lifecycle and observed thread states,
* lock ownership (with recursion counts; shared reader sets for rwlocks) and
  outstanding wait requests,
* semaphore counts and waiters (no owner, so never wait-for edges),
* the CURRENT wait-for graph (derived on demand from the state),
* the HISTORICAL lock-order graph,
* findings with their supporting evidence.

It is pure and deterministic: no I/O, no clocks. Live sessions, replay and
the unit tests all drive the same code, so the graph shown at a replayed
sequence number is exactly what the live analysis saw at that point.
"""

from __future__ import annotations

import errno
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Protocol

from app.detection.graphs import (LockOrderGraph, all_cycles, canonical_cycle, cycles_multi, find_cycle_dfs,
                                  find_cycle_from)
from app.detection.model import (BARRIER, BLOCKED_JOIN, BLOCKED_LOCK, EXITED, RUNNING, WAITING_COND, WAITING_SEM,
                                 CondInfo, Coverage, Held, LockInfo, ReadHold, SemInfo, ThreadInfo, Wait, WaitEdge)
from app.diagnostics.findings import (API_MISUSE, COMMON_LIMITATIONS, CONFIRMED_DEADLOCK,
                                      CONTENTION_REMEDIES, EXCESSIVE_CONTENTION, INCONCLUSIVE,
                                      LOCK_ORDER_REMEDIES, NO_ISSUE, POTENTIAL_DEADLOCK, SEM_STALL_REMEDIES,
                                      SEVERITY_RANK, STALL, STALL_REMEDIES, VERDICT_ORDER,
                                      CLASSIFICATION_LABELS, Evidence, Finding, deadlock_remedies,
                                      fmt_ms)
from app.models.wire import (MUTEX_KIND_MASK, MUTEX_ROBUST_BIT, RW_PREFER_WRITER_NONRECURSIVE, EventType as E, Ev,
                             ExitKind, Flag, mutex_kind_name, rw_kind, rw_mode, rwlock_kind_name)

RW_MODE_WORDS = {"r": "reading", "w": "writing"}

ERRNO_NAMES = dict(errno.errorcode)  # 1 -> 'EPERM', ...


def errno_name(rc: int) -> str:
    return ERRNO_NAMES.get(rc, str(rc)) if rc else "OK"


@dataclass
class AnalysisConfig:
    contention_min_acquisitions: int = 20
    contention_ratio: float = 0.5           # fraction of acquisitions that had to wait
    contention_total_wait_ms: float = 100.0
    long_wait_ms: float = 500.0             # a single wait this long is reported
    stall_ms: float = 1000.0                # no progress for this long + all blocked => stall
    confirm_requires_heartbeat: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


class Resolver(Protocol):
    def data_symbol(self, addr: int) -> str | None: ...


class Analyzer:
    def __init__(self, config: AnalysisConfig | None = None, resolver: Resolver | None = None) -> None:
        self.cfg = config or AnalysisConfig()
        self.resolver = resolver
        self.threads: dict[int, ThreadInfo] = {}
        self.by_tid: dict[int, ThreadInfo] = {}
        self.by_handle: dict[int, ThreadInfo] = {}
        self.pending_names: dict[int, str] = {}
        self.pending_parent: dict[int, int] = {}
        self.locks: dict[int, LockInfo] = {}
        self.lock_by_addr: dict[int, int] = {}
        self.conds: dict[int, CondInfo] = {}
        self.sems: dict[int, SemInfo] = {}
        self.lock_order = LockOrderGraph()
        self.coverage = Coverage()
        self.counts: Counter[int] = Counter()
        self.findings: dict[str, Finding] = {}
        self.marks: list[dict] = []
        self.process: dict = {}
        self.seq = -1
        self.first_ts: int | None = None
        self.now_ts = 0
        self.last_inband_ts = 0
        self.ended = False
        self.version = 0                 # bumped on every state change (for broadcasting)
        self.findings_version = 0
        self._next_finding = 1
        self._next_lock = 1
        self._next_synthetic_thread = 100000
        self._cycle_keys: dict[frozenset, str] = {}
        self._lo_keys: dict[tuple, str] = {}
        self._lo_edge_findings: dict[tuple[int, int], set[str]] = {}
        self._misuse_keys: dict[tuple, str] = {}
        self._contention_keys: dict[int, str] = {}
        self._stall_keys: dict[frozenset, str] = {}
        self._pending_release: dict[int, tuple] = {}
        self._pending_post: dict[int, int] = {}      # thread -> semaphore of its last SEM_POST
        self._rw_waiters: set[int] = set()           # threads waiting on an rwlock (WFG not functional)
        self._ann_lock: int | None = None
        self._ann_corr: int | None = None
        self._handlers = {
            E.PROCESS_START: self._on_process_start,
            E.PROCESS_EXIT: self._on_process_exit,
            E.THREAD_CREATE: self._on_thread_create,
            E.THREAD_EXIT: self._on_thread_exit,
            E.THREAD_NAME: self._on_thread_name,
            E.JOIN_BEGIN: self._on_join_begin,
            E.JOIN_END: self._on_join_end,
            E.THREAD_DETACH: self._on_detach,
            E.CANCEL_REQUEST: self._on_cancel,
            E.MUTEX_INIT: self._on_mutex_init,
            E.MUTEX_DESTROY: self._on_mutex_destroy,
            E.LOCK_REQUEST: self._on_lock_request,
            E.LOCK_ACQUIRED: self._on_lock_acquired,
            E.LOCK_FAILED: self._on_lock_failed,
            E.LOCK_RELEASE: self._on_lock_release,
            E.UNLOCK_FAILED: self._on_unlock_failed,
            E.LOCK_NAME: self._on_lock_name,
            E.COND_WAIT_BEGIN: self._on_cond_begin,
            E.COND_WAIT_END: self._on_cond_end,
            E.COND_SIGNAL: self._on_cond_signal,
            E.COND_BROADCAST: self._on_cond_signal,
            E.BARRIER_WAIT_BEGIN: self._on_barrier_begin,
            E.BARRIER_WAIT_END: self._on_barrier_end,
            E.USER_MARK: self._on_mark,
            E.RWLOCK_INIT: self._on_rwlock_init,
            E.RWLOCK_DESTROY: self._on_rwlock_destroy,
            E.RW_REQUEST: self._on_rw_request,
            E.RW_ACQUIRED: self._on_rw_acquired,
            E.RW_FAILED: self._on_rw_failed,
            E.RW_RELEASE: self._on_rw_release,
            E.SEM_INIT: self._on_sem_init,
            E.SEM_DESTROY: self._on_sem_destroy,
            E.SEM_WAIT_BEGIN: self._on_sem_wait_begin,
            E.SEM_WAIT_END: self._on_sem_wait_end,
            E.SEM_POST: self._on_sem_post,
            E.SEM_POST_FAILED: self._on_sem_post_failed,
        }

    # Replay checkpoints pickle the analyzer; the handler table (bound methods)
    # and the resolver (holds a lock and subprocess state) are rebuilt instead.
    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("_handlers", None)
        state["resolver"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        resolver = state.get("resolver")
        fresh = Analyzer(state.get("cfg"), resolver)
        self.__dict__.update(state)
        self._handlers = {k: getattr(self, v.__name__) for k, v in fresh._handlers.items()}

    # ------------------------------------------------------------------ API

    def apply(self, ev: Ev) -> tuple[int | None, int | None]:
        """Apply one in-band record. Returns (lock id, correlated seq) for storage."""
        self.seq = ev.seq
        if self.first_ts is None:
            self.first_ts = ev.ts
        if ev.ts > self.now_ts:
            self.now_ts = ev.ts
        self.last_inband_ts = ev.ts
        self.counts[ev.type] += 1
        self._ann_lock = None
        self._ann_corr = None
        self.version += 1
        if ev.type == E.THREAD_START:
            t = self._on_thread_start(ev)
        else:
            t = self._thread_for(ev)
            handler = self._handlers.get(ev.type)
            if handler is not None:
                handler(ev, t)
        if t is not None:
            t.last_seq = ev.seq
            t.last_ts = ev.ts
            t.last_type = ev.type
            t.events += 1
        return self._ann_lock, self._ann_corr

    def apply_meta(self, ev: Ev) -> None:
        """Apply an out-of-band collector record (HELLO, HEARTBEAT, EVENTS_LOST)."""
        self.version += 1
        if ev.type == E.HELLO:
            self.process.update(token=ev.obj, load_bias=ev.aux, protocol=ev.result, ring_capacity=ev.extra)
            self.coverage.ring_capacity = ev.extra
            return
        if ev.ts > self.now_ts:
            self.now_ts = ev.ts
        if ev.type == E.EVENTS_LOST:
            self._set_lost(ev.obj)
            return
        if ev.type == E.HEARTBEAT:
            self.coverage.heartbeats += 1
            self.coverage.last_heartbeat_ts = ev.ts
            self.coverage.last_heartbeat_seq = ev.seq
            self.coverage.ring_depth = ev.aux
            self._set_lost(ev.obj)
            self.tick(heartbeat_seq=ev.seq)

    def tick(self, heartbeat_seq: int | None = None) -> None:
        """Time-based checks: confirmation, cycle resolution, stalls, long waits."""
        self._review_cycles(heartbeat_seq)
        self._check_long_waits()
        self._check_stall()
        self._refresh_contention()

    def finalize(self) -> None:
        """The event stream ended (process exited, was killed, or trace ended)."""
        if self.ended:
            return
        self.ended = True
        self.coverage.stream_ended = True
        self.version += 1
        self._review_cycles(None, end_of_stream=True)
        self._refresh_contention(force=True)
        for fid in list(self._lo_keys.values()):
            self._reevaluate_lock_order(fid)

    # ------------------------------------------------------------ helpers

    def _set_lost(self, total: int) -> None:
        if total > self.coverage.lost_events:
            first = self.coverage.lost_events == 0
            self.coverage.lost_events = total
            if first:
                self._coverage_downgrade("events were dropped by the collector (ring buffer full)")

    def _anomaly(self, kind: str, ev: Ev, detail: str) -> None:
        self.coverage.anomalies += 1
        if len(self.coverage.anomaly_examples) < 50:
            self.coverage.anomaly_examples.append({"kind": kind, "seq": ev.seq, "detail": detail})
        if self.coverage.anomalies == 1:
            self._coverage_downgrade(f"trace anomaly: {detail}")

    def _coverage_downgrade(self, reason: str) -> None:
        """Coverage became incomplete: unconfirmed deadlock candidates become inconclusive,
        other findings get a warning that they may be affected."""
        for f in self.findings.values():
            if f.kind in ("lock_order", "contention", "stall", "misuse", "abandoned") and f.status != "filtered":
                note = f"Coverage became incomplete after this finding was produced ({reason}); it may be affected."
                if note not in f.limitations:
                    f.limitations.insert(0, note)
                    self._touch(f)
            if f.kind == "deadlock" and f.status == "suspected":
                f.classification = INCONCLUSIVE
                f.severity = "warning"
                f.status = "active"
                f.title = "Wait-for cycle observed, but the trace is incomplete"
                f.limitations.insert(0, f"Coverage became incomplete before confirmation: {reason}.")
                self._touch(f)

    def _thread_for(self, ev: Ev) -> ThreadInfo | None:
        if ev.type in (E.PROCESS_START,) and ev.tid not in self.by_tid:
            return None
        t = self.by_tid.get(ev.tid)
        if t is None:
            idx = self._next_synthetic_thread
            self._next_synthetic_thread += 1
            t = ThreadInfo(index=idx, tid=ev.tid, started_seq=ev.seq, started_ts=ev.ts, late=True)
            self.threads[idx] = t
            self.by_tid[ev.tid] = t
            self.coverage.late_threads += 1
            self._anomaly("unknown_thread", ev, f"event from thread tid={ev.tid} that never started")
        return t

    def _lock(self, addr: int, seq: int, kind: int | None = None) -> LockInfo:
        lid = self.lock_by_addr.get(addr)
        if lid is not None:
            lock = self.locks[lid]
            if kind is not None and kind != lock.kind and seq >= 0:
                lock.kind = kind
            return lock
        lid = self._next_lock
        self._next_lock += 1
        lock = LockInfo(id=lid, addr=addr, kind=kind or 0, first_seq=seq)
        if self.resolver is not None:
            try:
                lock.symbol = self.resolver.data_symbol(addr)
            except Exception:  # symbolization must never break analysis
                lock.symbol = None
        self.locks[lid] = lock
        self.lock_by_addr[addr] = lid
        return lock

    def _cond(self, addr: int) -> CondInfo:
        c = self.conds.get(addr)
        if c is None:
            c = self.conds[addr] = CondInfo(addr)
        return c

    def tlabel(self, idx: int | None) -> str:
        if idx is None:
            return "?"
        t = self.threads.get(idx)
        return f"{t.label} (T{idx})" if t else f"T{idx}"

    def llabel(self, lid: int | None) -> str:
        if lid is None:
            return "?"
        lock = self.locks.get(lid)
        return f"{lock.label} (L{lid})" if lock else f"L{lid}"

    def _new_finding(self, **kw) -> Finding:
        fid = f"F{self._next_finding}"
        self._next_finding += 1
        f = Finding(id=fid, detected_seq=self.seq, detected_ts=self.now_ts, updated_seq=self.seq,
                    coverage_at_detection={"complete": self.coverage.complete,
                                           "lost_events": self.coverage.lost_events,
                                           "anomalies": self.coverage.anomalies}, **kw)
        if not f.limitations:
            f.limitations = list(COMMON_LIMITATIONS)
        self.findings[fid] = f
        self.findings_version += 1
        return f

    def _touch(self, f: Finding) -> None:
        f.updated_seq = self.seq
        self.findings_version += 1

    @staticmethod
    def _base_kind(kind: int) -> int:
        return kind & MUTEX_KIND_MASK

    def _refuses_foreign_unlock(self, lock: LockInfo) -> bool:
        # glibc checks ownership on unlock for recursive, error-checking and robust mutexes
        return self._base_kind(lock.kind) in (1, 2) or bool(lock.kind & MUTEX_ROBUST_BIT)

    def _release_fully(self, t: ThreadInfo | None, lock: LockInfo, ts: int) -> None:
        if lock.owner is not None:
            hold = ts - lock.owner_since_ts
            if hold > 0:
                lock.hold_ns_total += hold
                lock.holds += 1
                if hold > lock.hold_ns_max:
                    lock.hold_ns_max = hold
            owner = self.threads.get(lock.owner)
            if owner is not None:
                owner.held = [h for h in owner.held if h.lock != lock.id]
            lock.last_owner = lock.owner
        lock.owner = None
        lock.count = 0

    # --------------------------------------------------------- process/thread

    def _on_process_start(self, ev: Ev, t: ThreadInfo | None) -> None:
        self.process.update(ppid=ev.obj, load_bias=ev.aux, start_seq=ev.seq, start_ts=ev.ts, pid=ev.pid)

    def _on_process_exit(self, ev: Ev, t: ThreadInfo | None) -> None:
        self.coverage.clean_exit = True
        self.process.update(exit_seq=ev.seq, exit_ts=ev.ts, dropped_at_exit=ev.obj, threads_started=ev.extra)
        self._set_lost(ev.obj)
        for th in self.threads.values():
            if th.alive:
                self._clear_wait(th)
                th.state = EXITED
                th.terminated_by_process_exit = True
                th.ended_seq = ev.seq
                th.ended_ts = ev.ts
                th.exit_kind = ExitKind.PROCESS

    def _on_thread_start(self, ev: Ev) -> ThreadInfo:
        idx = ev.extra
        if idx in self.threads:
            self._anomaly("duplicate_thread_index", ev, f"thread index {idx} started twice")
            idx = self._next_synthetic_thread
            self._next_synthetic_thread += 1
        t = ThreadInfo(index=idx, tid=ev.tid, handle=ev.obj, start_routine=ev.aux,
                       started_seq=ev.seq, started_ts=ev.ts, late=bool(ev.flags & Flag.LATE))
        if t.late:
            self.coverage.late_threads += 1
        self.threads[idx] = t
        self.by_tid[ev.tid] = t
        self.by_handle[ev.obj] = t
        name = self.pending_names.pop(ev.obj, None)
        if name:
            t.name = name
        parent = self.pending_parent.pop(ev.obj, None)
        if parent is not None:
            t.parent = parent
        # joins that were waiting for this (not yet started) thread now have a target
        for other in self.threads.values():
            w = other.wait
            if w is not None and w.kind == "join" and w.target_handle == ev.obj and other is not t:
                self._check_cycle_from(other.index, ev)
        return t

    def _on_thread_create(self, ev: Ev, t: ThreadInfo | None) -> None:
        if ev.result != 0 or t is None:
            return
        child = self.by_handle.get(ev.obj)
        if child is not None and child.alive and child.parent is None and child is not t:
            child.parent = t.index
        else:
            self.pending_parent[ev.obj] = t.index

    def _on_thread_exit(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        self._clear_wait(t)
        t.state = EXITED
        t.ended_seq = ev.seq
        t.ended_ts = ev.ts
        t.exit_kind = ev.extra
        if t.held and ev.extra != ExitKind.PROCESS:
            for h in t.held:
                lock = self.locks[h.lock]
                if lock.rw:
                    how = RW_MODE_WORDS.get(h.mode, "?")
                    f = self._misuse(
                        ("exit_holding", t.index, h.lock), "abandoned", "warning",
                        f"{t.label} exited while holding {lock.label} for {how}",
                        f"Thread {self.tlabel(t.index)} terminated while it still held {self.llabel(h.lock)} for "
                        f"{how}. Read-write locks have no robust variant: the lock is never released, so every "
                        + ("later lock call on it (read or write) blocks forever." if h.mode == "w" else
                           "later write lock call on it blocks forever."),
                        threads=[t.index], locks=[h.lock],
                        evidence=[
                            Evidence(h.seq, h.ts, t.index, "RW_ACQUIRED", f"{t.label} acquired {lock.label} for {how}",
                                     "cause", h.lock, h.caller),
                            Evidence(ev.seq, ev.ts, t.index, "THREAD_EXIT", f"{t.label} exited without unlocking", "trigger"),
                        ],
                        remedies=["Unlock every read-write lock on every exit path (including early returns and errors).",
                                  "Use pthread_cleanup_push/pop (C) or RAII guards such as std::shared_lock / "
                                  "std::unique_lock (C++)."])
                else:
                    f = self._misuse(
                        ("exit_holding", t.index, h.lock), "abandoned", "warning",
                        f"{t.label} exited while holding {lock.label}",
                        f"Thread {self.tlabel(t.index)} terminated while it still owned {self.llabel(h.lock)}. "
                        "A normal mutex owned by a dead thread can never be unlocked; every later "
                        "lock() call on it blocks forever.",
                        threads=[t.index], locks=[h.lock],
                        evidence=[
                            Evidence(h.seq, h.ts, t.index, "LOCK_ACQUIRED", f"{t.label} acquired {lock.label}", "cause", h.lock, h.caller),
                            Evidence(ev.seq, ev.ts, t.index, "THREAD_EXIT", f"{t.label} exited without unlocking", "trigger"),
                        ],
                        remedies=["Unlock every mutex on every exit path (including early returns and errors).",
                                  "Use pthread_cleanup_push/pop (C) or RAII guards such as std::lock_guard (C++).",
                                  "If a thread may die while holding a lock, use a robust mutex "
                                  "(PTHREAD_MUTEX_ROBUST) and handle EOWNERDEAD."])
                # threads already waiting on this lock (in a conflicting mode) can now never get it
                for wi in list(lock.waiters):
                    waiter = self.threads.get(wi)
                    if waiter and waiter.wait is not None and (not lock.rw or h.mode == "w" or waiter.wait.mode == "w"):
                        self._abandoned_wait(waiter, lock, ev, holder=t.index)
                f.metrics["held_locks"] = len(t.held)
        self._pending_release.pop(t.index, None)
        self._pending_post.pop(t.index, None)

    def _on_thread_name(self, ev: Ev, t: ThreadInfo | None) -> None:
        target = self.by_handle.get(ev.obj)
        if target is not None and target.index == 0 and "exe_name" not in self.process:
            # the collector reports the main thread's initial name = process name
            self.process["exe_name"] = ev.text
            return
        if target is not None and target.alive:
            target.name = ev.text
        else:
            self.pending_names[ev.obj] = ev.text or ""

    def _on_join_begin(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        t.wait = Wait("join", ev.seq, ev.ts, caller=ev.caller, target_handle=ev.obj)
        t.state = BLOCKED_JOIN
        self._check_cycle_from(t.index, ev)

    def _on_join_end(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        w = t.wait
        if w is not None and w.kind == "join":
            self._ann_corr = w.seq
            t.wait = None
            t.state = RUNNING
        if ev.result != 0:
            target = self.by_handle.get(ev.obj)
            what = {errno.EDEADLK: "a thread tried to join itself (or a join cycle was detected by glibc)",
                    errno.EINVAL: "the target thread is not joinable (detached or already being joined)",
                    errno.ESRCH: "no thread with that handle exists"}.get(ev.result, "join failed")
            self._misuse(("join_error", t.index, ev.obj), "misuse", "warning",
                         f"pthread_join failed with {errno_name(ev.result)}",
                         f"{self.tlabel(t.index)} called pthread_join and got {errno_name(ev.result)}: {what}.",
                         threads=[t.index] + ([target.index] if target else []), locks=[],
                         evidence=[Evidence(ev.seq, ev.ts, t.index, "JOIN_END",
                                            f"join returned {errno_name(ev.result)}", "trigger", None, ev.caller)],
                         remedies=["Join each joinable thread exactly once, from a different thread.",
                                   "Do not join detached threads."])

    def _on_detach(self, ev: Ev, t: ThreadInfo | None) -> None:
        target = self.by_handle.get(ev.obj)
        if target is not None and ev.result == 0:
            target.detached = True

    def _on_cancel(self, ev: Ev, t: ThreadInfo | None) -> None:
        target = self.by_handle.get(ev.obj)
        if target is not None:
            target.cancel_requested = True

    # ---------------------------------------------------------------- mutexes

    def _on_mutex_init(self, ev: Ev, t: ThreadInfo | None) -> None:
        lid = self.lock_by_addr.get(ev.obj)
        if lid is not None:
            old = self.locks[lid]
            if old.owner is not None and not old.destroyed:
                self._misuse(("reinit_locked", lid), "misuse", "warning",
                             f"{old.label} re-initialised while locked",
                             f"pthread_mutex_init was called on {self.llabel(lid)} while "
                             f"{self.tlabel(old.owner)} held it; this is undefined behaviour.",
                             threads=[old.owner] + ([t.index] if t else []), locks=[lid],
                             evidence=[Evidence(ev.seq, ev.ts, t.index if t else None, "MUTEX_INIT",
                                                "mutex re-initialised", "trigger", lid, ev.caller)],
                             remedies=["Initialise each mutex once before any thread uses it."])
            # a re-initialised address is a new lock instance
            del self.lock_by_addr[ev.obj]
        lock = self._lock(ev.obj, ev.seq, kind=ev.extra if ev.result == 0 else None)
        self._ann_lock = lock.id

    def _on_mutex_destroy(self, ev: Ev, t: ThreadInfo | None) -> None:
        lock = self._lock(ev.obj, ev.seq)
        self._ann_lock = lock.id
        if ev.result == 0:
            lock.destroyed = True
            del self.lock_by_addr[ev.obj]  # address may be reused for a new lock
            return
        if ev.result == errno.EBUSY:
            self._misuse(("destroy_locked", lock.id), "misuse", "warning",
                         f"Attempt to destroy {lock.label} while it is locked",
                         f"pthread_mutex_destroy({self.llabel(lock.id)}) returned EBUSY because the mutex "
                         f"was still locked{' by ' + self.tlabel(lock.owner) if lock.owner is not None else ''}.",
                         threads=[i for i in ([t.index] if t else []) + ([lock.owner] if lock.owner is not None else [])],
                         locks=[lock.id],
                         evidence=[Evidence(ev.seq, ev.ts, t.index if t else None, "MUTEX_DESTROY",
                                            "destroy returned EBUSY", "trigger", lock.id, ev.caller)],
                         remedies=["Destroy a mutex only after every thread has stopped using it "
                                   "(e.g. after joining them)."])

    def _on_lock_name(self, ev: Ev, t: ThreadInfo | None) -> None:
        sem = self.sems.get(ev.obj)
        if sem is not None:  # tl_name_lock() also names semaphores
            sem.annotation = ev.text or None
            return
        lock = self._lock(ev.obj, ev.seq)
        lock.annotation = ev.text or None
        self._ann_lock = lock.id

    def _on_lock_request(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.obj, ev.seq, kind=ev.extra)
        self._ann_lock = lock.id
        if t.wait is not None:
            self._anomaly("request_while_waiting", ev, f"{t.label} requested a lock while already waiting")
            self._clear_wait(t)
        own = lock.owner
        t.wait = Wait("lock", ev.seq, ev.ts, caller=ev.caller, timed=bool(ev.flags & Flag.TIMED),
                      lock=lock.id, glibc_owner=ev.aux, tracked_owner=own)
        t.state = BLOCKED_LOCK
        lock.waiters.add(t.index)
        # cross-check our ownership view with the owner TID stored in glibc's mutex
        if ev.aux or own is not None:
            self.coverage.owner_checks += 1
            owner_tid = self.threads[own].tid if own is not None and own in self.threads else 0
            if owner_tid != ev.aux:
                self.coverage.owner_mismatches += 1
        if own is not None and own in self.threads and not self.threads[own].alive:
            self._abandoned_wait(t, lock, ev)
        self._check_cycle_from(t.index, ev)

    def _clear_wait(self, t: ThreadInfo) -> None:
        w = t.wait
        if w is not None and w.kind in ("lock", "rwlock") and w.lock is not None and w.lock in self.locks:
            self.locks[w.lock].waiters.discard(t.index)
        if w is not None and w.kind == "cond" and w.cond is not None and w.cond in self.conds:
            self.conds[w.cond].waiters.discard(t.index)
        if w is not None and w.kind == "sem" and w.sem is not None and w.sem in self.sems:
            self.sems[w.sem].waiters.discard(t.index)
        self._rw_waiters.discard(t.index)
        t.wait = None
        t.state = RUNNING

    def _on_lock_acquired(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.obj, ev.seq, kind=ev.extra)
        self._ann_lock = lock.id
        is_try = bool(ev.flags & Flag.TRY)
        timed = bool(ev.flags & Flag.TIMED)
        w = t.wait
        matched = None
        if not is_try:
            if w is None or w.kind != "lock" or w.lock != lock.id:
                self._anomaly("acquire_without_request", ev,
                              f"{t.label} acquired {lock.label} without a recorded request")
            else:
                matched = w
                self._ann_corr = w.seq
            self._clear_wait(t)

        if lock.owner is not None and lock.owner != t.index:
            prev = self.threads.get(lock.owner)
            if ev.result == errno.EOWNERDEAD or (prev is not None and not prev.alive and lock.kind & MUTEX_ROBUST_BIT):
                pass  # robust mutex: previous owner died, ownership passes to us
            else:
                self._anomaly("acquired_while_owned", ev,
                              f"{t.label} acquired {lock.label} still owned by {prev.label if prev else '?'} "
                              "(a release was not observed)")
            self._release_fully(None, lock, ev.ts)

        if lock.owner == t.index:
            lock.count += 1
            if self._base_kind(lock.kind) != 1:
                self._anomaly("reacquired_nonrecursive", ev, f"{t.label} re-acquired non-recursive {lock.label}")
        else:
            if not is_try:
                self._record_lock_order(t, lock, ev, timed)
            lock.owner = t.index
            lock.count = 1
            lock.owner_since_seq = ev.seq
            lock.owner_since_ts = ev.ts
            lock.owner_caller = ev.caller
            lock.owner_confirmed = True
            t.held.append(Held(lock.id, ev.seq, ev.ts, ev.caller))

        lock.acquisitions += 1
        t.acquisitions += 1
        if matched is not None:
            wait_ns = ev.aux
            lock.wait_ns_total += wait_ns
            t.wait_ns_total += wait_ns
            if wait_ns > lock.wait_ns_max:
                lock.wait_ns_max = wait_ns
            if (matched.tracked_owner is not None and matched.tracked_owner != t.index) or \
                    (matched.glibc_owner not in (0, t.tid)):
                lock.contended += 1
        if ev.result == errno.EOWNERDEAD:
            prev_label = self.tlabel(lock.last_owner) if lock.last_owner is not None else "a thread"
            self._misuse(("owner_dead", lock.id), "misuse", "info",
                         f"Robust mutex {lock.label} recovered from a dead owner",
                         f"{self.tlabel(t.index)} acquired robust mutex {self.llabel(lock.id)} with EOWNERDEAD: "
                         f"{prev_label} died while holding it. The new owner must repair the protected data "
                         "and call pthread_mutex_consistent().",
                         threads=[t.index], locks=[lock.id],
                         evidence=[Evidence(ev.seq, ev.ts, t.index, "LOCK_ACQUIRED", "lock returned EOWNERDEAD",
                                            "trigger", lock.id, ev.caller)],
                         remedies=["Make sure the dying thread's exit path unlocks the mutex.",
                                   "After EOWNERDEAD, restore invariants and call pthread_mutex_consistent()."])
        # threads still waiting for this lock now wait for the new owner
        for wi in sorted(lock.waiters):
            self._check_cycle_from(wi, ev)
        self._maybe_contention(lock)

    def _record_lock_order(self, t: ThreadInfo, lock: LockInfo, ev: Ev, timed: bool, via_cond: bool = False,
                           mode: str = "x") -> None:
        held = [h for h in t.held if h.lock != lock.id]
        if not held:
            return
        # only locks held exclusively can act as gate locks: two readers of a gate run concurrently
        exclusive = frozenset(h.lock for h in held if h.mode != "r")
        for h in held:
            edge, new = self.lock_order.observe(h.lock, lock.id, t.index, h.seq, ev.seq, ev.ts,
                                                h.caller, ev.caller, exclusive - {h.lock}, timed, via_cond,
                                                src_mode=h.mode, dst_mode=mode)
            if new:
                cyc = self.lock_order.cycle_through(h.lock, lock.id)
                if cyc is not None:
                    self._on_lock_order_cycle(cyc)
            else:
                for fid in self._lo_edge_findings.get((h.lock, lock.id), ()):
                    self._reevaluate_lock_order(fid)

    def _on_lock_failed(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.obj, ev.seq, kind=ev.extra)
        self._ann_lock = lock.id
        is_try = bool(ev.flags & Flag.TRY)
        if not is_try:
            w = t.wait
            if w is not None and w.kind == "lock" and w.lock == lock.id:
                self._ann_corr = w.seq
            self._clear_wait(t)
        rc = ev.result
        if is_try and rc == errno.EBUSY:
            lock.try_failures += 1
            return
        if rc == errno.ETIMEDOUT:
            lock.timeouts += 1
            return
        if rc == errno.EDEADLK:
            self._misuse(("relock", t.index, lock.id), "misuse", "warning",
                         f"{t.label} tried to re-lock {lock.label} it already holds",
                         f"{self.tlabel(t.index)} called lock on error-checking mutex {self.llabel(lock.id)} "
                         "while already owning it. glibc returned EDEADLK instead of blocking — with a "
                         "normal mutex this would have been a self-deadlock.",
                         threads=[t.index], locks=[lock.id],
                         evidence=self._owner_evidence(lock) + [
                             Evidence(ev.seq, ev.ts, t.index, "LOCK_FAILED", "lock returned EDEADLK",
                                      "trigger", lock.id, ev.caller)],
                         remedies=["Do not lock a mutex the thread already holds; use a *_locked() helper "
                                   "for code that runs with the lock held.",
                                   "Use PTHREAD_MUTEX_RECURSIVE only if re-entrancy is intended."])
            return
        self._misuse(("lock_error", t.index, lock.id, rc), "misuse", "warning",
                     f"Locking {lock.label} failed with {errno_name(rc)}",
                     f"{self.tlabel(t.index)} could not lock {self.llabel(lock.id)}: {errno_name(rc)}.",
                     threads=[t.index], locks=[lock.id],
                     evidence=[Evidence(ev.seq, ev.ts, t.index, "LOCK_FAILED", f"lock returned {errno_name(rc)}",
                                        "trigger", lock.id, ev.caller)],
                     remedies=["Check pthread return codes and handle errors explicitly."])

    def _on_lock_release(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.obj, ev.seq, kind=ev.extra or None)
        self._ann_lock = lock.id
        if lock.owner == t.index:
            self._pending_release[t.index] = (lock.id, lock.count, lock.owner_since_seq, lock.owner_since_ts,
                                              lock.owner_caller, list(t.held))
            lock.count -= 1
            if lock.count <= 0:
                self._release_fully(t, lock, ev.ts)
            return
        self._pending_release[t.index] = (lock.id, None, None, None, None, None)
        if lock.owner is None:
            if not self._refuses_foreign_unlock(lock):
                self._misuse(("unlock_unlocked", t.index, lock.id), "misuse", "warning",
                             f"{t.label} unlocked {lock.label}, which was not locked",
                             f"{self.tlabel(t.index)} called pthread_mutex_unlock on {self.llabel(lock.id)} "
                             "although no thread held it. For a normal mutex this is undefined behaviour "
                             "and can corrupt the mutex state.",
                             threads=[t.index], locks=[lock.id],
                             evidence=[Evidence(ev.seq, ev.ts, t.index, "LOCK_RELEASE", "unlock of unlocked mutex",
                                                "trigger", lock.id, ev.caller)],
                             remedies=["Make every unlock match exactly one successful lock.",
                                       "Use PTHREAD_MUTEX_ERRORCHECK during development to catch this."])
            return
        # released by a thread that does not own it
        owner = lock.owner
        if self._refuses_foreign_unlock(lock):
            return  # glibc returns EPERM; reported on UNLOCK_FAILED
        self._misuse(("unlock_not_owner", t.index, lock.id), "misuse", "warning",
                     f"{t.label} unlocked {lock.label} owned by another thread",
                     f"{self.tlabel(t.index)} unlocked {self.llabel(lock.id)} while {self.tlabel(owner)} owned it. "
                     "glibc does not check ownership for normal mutexes, so the unlock took effect: "
                     "the owner's critical section is no longer protected.",
                     threads=[t.index, owner], locks=[lock.id],
                     evidence=self._owner_evidence(lock) + [
                         Evidence(ev.seq, ev.ts, t.index, "LOCK_RELEASE", "unlock by non-owner", "trigger",
                                  lock.id, ev.caller)],
                     remedies=["Only the owning thread may unlock a mutex.",
                               "Use PTHREAD_MUTEX_ERRORCHECK so such unlocks fail with EPERM.",
                               "If another thread must signal completion, use a condition variable or semaphore."])
        self._release_fully(None, lock, ev.ts)

    def _on_unlock_failed(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.obj, ev.seq)
        self._ann_lock = lock.id
        pr = self._pending_release.pop(t.index, None)
        was_owner = False
        if pr is not None and pr[0] == lock.id and pr[1] is not None:
            # the release we applied did not really happen: restore ownership
            was_owner = True
            _, count, since_seq, since_ts, caller, held = pr
            lock.owner = t.index
            lock.count = count
            lock.owner_since_seq = since_seq
            lock.owner_since_ts = since_ts
            lock.owner_caller = caller
            t.held = held
        if was_owner:
            what = f"{t.label} owns {lock.label} but unlocking it failed"
        elif lock.owner is None:
            what = f"{t.label} unlocked {lock.label}, which was not locked"
        else:
            what = f"{t.label} tried to unlock {lock.label} owned by {self.tlabel(lock.owner)}"
        threads = [t.index] + ([lock.owner] if lock.owner is not None and lock.owner != t.index else [])
        self._misuse(("unlock_failed", t.index, lock.id, ev.result), "misuse", "warning",
                     f"{what} ({errno_name(ev.result)})",
                     f"{what}; glibc returned {errno_name(ev.result)} because the mutex type "
                     f"({mutex_kind_name(lock.kind)}) checks ownership. The mutex state is unchanged.",
                     threads=threads, locks=[lock.id],
                     evidence=self._owner_evidence(lock) + [
                         Evidence(ev.seq, ev.ts, t.index, "UNLOCK_FAILED", f"unlock returned {errno_name(ev.result)}",
                                  "trigger", lock.id, ev.caller)],
                     remedies=["Only the owning thread may unlock a mutex, exactly once per lock.",
                               "Check the return value of pthread_mutex_unlock."])

    # -------------------------------------------------------- read-write locks

    def _rwlock(self, addr: int, seq: int, kind: int | None = None) -> LockInfo:
        lock = self._lock(addr, seq, kind)
        lock.rw = True
        return lock

    def _rw_blockers(self, idx: int, lock: LockInfo, mode: str | None) -> list[tuple[int, bool]]:
        """Threads that a request by ``idx`` for ``mode`` on rwlock ``lock`` has to wait for,
        each with an 'inferred' flag. This is the rwlock part of the wait-for graph:

        * a writer owner blocks every request (glibc fails a request by the writer
          itself with EDEADLK instead of blocking, so that adds no edge);
        * a write request waits for EVERY current reader — including the requester
          itself if it already reads the lock (an in-place upgrade, which glibc never
          grants: a self-loop). The self-loop is listed first because it alone
          explains the hang;
        * a read request with no writer owner proceeds immediately (glibc prefers
          readers), except on writer-preferring rwlocks
          (PTHREAD_RWLOCK_PREFER_WRITER_NONRECURSIVE_NP), where it queues behind the
          waiting writers. Those edges are inferred: glibc's queue is not observed.
        """
        if lock.owner is not None:
            return [] if lock.owner == idx else [(lock.owner, False)]
        if mode == "w":
            readers = sorted(lock.readers)
            if idx in lock.readers:
                readers.remove(idx)
                readers.insert(0, idx)
            return [(r, False) for r in readers]
        out = []
        if lock.kind == RW_PREFER_WRITER_NONRECURSIVE:
            for wi in sorted(lock.waiters):
                other = self.threads.get(wi)
                if wi != idx and other is not None and other.wait is not None and other.wait.kind == "rwlock" \
                        and other.wait.lock == lock.id and other.wait.mode == "w":
                    out.append((wi, True))
        return out

    def _drop_reader(self, lock: LockInfo, idx: int, ts: int) -> None:
        rh = lock.readers.pop(idx, None)
        if rh is None:
            return
        hold = ts - rh.ts
        if hold > 0:
            lock.hold_ns_total += hold
            lock.holds += 1
            if hold > lock.hold_ns_max:
                lock.hold_ns_max = hold
        th = self.threads.get(idx)
        if th is not None:
            th.held = [h for h in th.held if h.lock != lock.id]

    def _on_rwlock_init(self, ev: Ev, t: ThreadInfo | None) -> None:
        lid = self.lock_by_addr.get(ev.obj)
        if lid is not None:
            old = self.locks[lid]
            if old.held and not old.destroyed:
                self._misuse(("reinit_locked", lid), "misuse", "warning",
                             f"{old.label} re-initialised while locked",
                             f"pthread_rwlock_init was called on {self.llabel(lid)} while {self._holders_text(old)} "
                             "held it; this is undefined behaviour.",
                             threads=old.holders() + ([t.index] if t else []), locks=[lid],
                             evidence=[Evidence(ev.seq, ev.ts, t.index if t else None, "RWLOCK_INIT",
                                                "rwlock re-initialised", "trigger", lid, ev.caller)],
                             remedies=["Initialise each read-write lock once before any thread uses it."])
            # a re-initialised address is a new lock instance
            del self.lock_by_addr[ev.obj]
        lock = self._rwlock(ev.obj, ev.seq, kind=ev.extra if ev.result == 0 else None)
        self._ann_lock = lock.id

    def _on_rwlock_destroy(self, ev: Ev, t: ThreadInfo | None) -> None:
        lock = self._rwlock(ev.obj, ev.seq)
        self._ann_lock = lock.id
        if ev.result != 0:
            self._misuse(("rw_destroy_error", lock.id, ev.result), "misuse", "warning",
                         f"Destroying {lock.label} failed with {errno_name(ev.result)}",
                         f"pthread_rwlock_destroy({self.llabel(lock.id)}) returned {errno_name(ev.result)}.",
                         threads=[t.index] if t else [], locks=[lock.id],
                         evidence=[Evidence(ev.seq, ev.ts, t.index if t else None, "RWLOCK_DESTROY",
                                            f"destroy returned {errno_name(ev.result)}", "trigger", lock.id, ev.caller)],
                         remedies=["Destroy a read-write lock only after every thread has stopped using it."])
            return
        if lock.held or lock.waiters:
            # glibc does not check: destroying a locked or awaited rwlock is undefined behaviour
            waiting = f" and {len(lock.waiters)} thread(s) were waiting for it" if lock.waiters else ""
            self._misuse(("destroy_locked", lock.id), "misuse", "warning",
                         f"{lock.label} destroyed while in use",
                         f"pthread_rwlock_destroy({self.llabel(lock.id)}) was called while {self._holders_text(lock)} "
                         f"held it{waiting}. glibc does not detect this (it returns 0); the behaviour is undefined.",
                         threads=list(dict.fromkeys(([t.index] if t else []) + lock.holders() + sorted(lock.waiters))),
                         locks=[lock.id],
                         evidence=self._owner_evidence(lock) + [
                             Evidence(ev.seq, ev.ts, t.index if t else None, "RWLOCK_DESTROY",
                                      "rwlock destroyed while in use", "trigger", lock.id, ev.caller)],
                         remedies=["Destroy a read-write lock only after every thread has stopped using it "
                                   "(e.g. after joining them)."])
        lock.destroyed = True
        del self.lock_by_addr[ev.obj]  # address may be reused for a new lock

    def _on_rw_request(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._rwlock(ev.obj, ev.seq, kind=rw_kind(ev.extra))
        self._ann_lock = lock.id
        mode = rw_mode(ev.extra)
        if t.wait is not None:
            self._anomaly("request_while_waiting", ev, f"{t.label} requested a lock while already waiting")
            self._clear_wait(t)
        blockers = self._rw_blockers(t.index, lock, mode)
        t.wait = Wait("rwlock", ev.seq, ev.ts, caller=ev.caller, timed=bool(ev.flags & Flag.TIMED), lock=lock.id,
                      mode=mode, glibc_owner=ev.aux, tracked_owner=lock.owner,
                      contended=any(not inferred and b != t.index for b, inferred in blockers))
        t.state = BLOCKED_LOCK
        lock.waiters.add(t.index)
        self._rw_waiters.add(t.index)
        # cross-check the writer we track with the writer TID stored in glibc's rwlock
        if ev.aux or lock.owner is not None:
            self.coverage.owner_checks += 1
            owner_tid = self.threads[lock.owner].tid if lock.owner in self.threads else 0
            if owner_tid != ev.aux:
                self.coverage.owner_mismatches += 1
        for b, inferred in blockers:
            holder = self.threads.get(b)
            if not inferred and holder is not None and not holder.alive:
                self._abandoned_wait(t, lock, ev, holder=b)
                break
        self._check_cycle_from(t.index, ev)

    def _on_rw_acquired(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._rwlock(ev.obj, ev.seq, kind=rw_kind(ev.extra))
        self._ann_lock = lock.id
        mode = rw_mode(ev.extra)
        is_try = bool(ev.flags & Flag.TRY)
        timed = bool(ev.flags & Flag.TIMED)
        matched = None
        if not is_try:
            w = t.wait
            if w is None or w.kind != "rwlock" or w.lock != lock.id:
                self._anomaly("acquire_without_request", ev,
                              f"{t.label} acquired {lock.label} without a recorded request")
            else:
                matched = w
                self._ann_corr = w.seq
            self._clear_wait(t)

        # With complete, ordered events none of these conflicts can occur (release
        # is recorded before the real unlock, acquisition after the real lock).
        if lock.owner is not None and (lock.owner != t.index or mode == "r"):
            self._anomaly("acquired_while_owned", ev,
                          f"{t.label} acquired {lock.label} for {RW_MODE_WORDS[mode]} while "
                          f"{self.tlabel(lock.owner)} held it for writing (a release was not observed)")
            self._release_fully(None, lock, ev.ts)
        if mode == "w":
            if lock.owner == t.index or lock.readers:
                self._anomaly("acquired_while_owned", ev,
                              f"{t.label} acquired {lock.label} for writing while {self._holders_text(lock)} "
                              "held it (a release was not observed)")
                for r in list(lock.readers):
                    self._drop_reader(lock, r, ev.ts)
                self._release_fully(None, lock, ev.ts)
            if not is_try:
                self._record_lock_order(t, lock, ev, timed, mode="w")
            lock.owner = t.index
            lock.count = 1
            lock.owner_since_seq = ev.seq
            lock.owner_since_ts = ev.ts
            lock.owner_caller = ev.caller
            t.held.append(Held(lock.id, ev.seq, ev.ts, ev.caller, "w"))
        else:
            rh = lock.readers.get(t.index)
            if rh is not None:
                rh.count += 1  # recursive read lock
            else:
                if not is_try:
                    self._record_lock_order(t, lock, ev, timed, mode="r")
                lock.readers[t.index] = ReadHold(1, ev.seq, ev.ts, ev.caller)
                t.held.append(Held(lock.id, ev.seq, ev.ts, ev.caller, "r"))

        lock.acquisitions += 1
        t.acquisitions += 1
        if matched is not None:
            wait_ns = ev.aux
            lock.wait_ns_total += wait_ns
            t.wait_ns_total += wait_ns
            if wait_ns > lock.wait_ns_max:
                lock.wait_ns_max = wait_ns
            if matched.contended or matched.glibc_owner not in (0, t.tid):
                lock.contended += 1
        # threads still waiting for this lock may now wait for the new holder
        for wi in sorted(lock.waiters):
            self._check_cycle_from(wi, ev)
        self._maybe_contention(lock)

    def _on_rw_failed(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._rwlock(ev.obj, ev.seq, kind=rw_kind(ev.extra))
        self._ann_lock = lock.id
        mode = rw_mode(ev.extra)
        is_try = bool(ev.flags & Flag.TRY)
        if not is_try:
            w = t.wait
            if w is not None and w.kind == "rwlock" and w.lock == lock.id:
                self._ann_corr = w.seq
            self._clear_wait(t)
        rc = ev.result
        how = RW_MODE_WORDS[mode]
        if is_try and rc == errno.EBUSY:
            lock.try_failures += 1
            return
        if rc == errno.ETIMEDOUT:
            lock.timeouts += 1
            return
        if rc == errno.EDEADLK:
            self._misuse(("rw_relock", t.index, lock.id), "misuse", "warning",
                         f"{t.label} tried to lock {lock.label} for {how} while holding it for writing",
                         f"{self.tlabel(t.index)} already holds {self.llabel(lock.id)} for writing and called "
                         f"pthread_rwlock_{'wrlock' if mode == 'w' else 'rdlock'} on it again. glibc detected this "
                         "and returned EDEADLK instead of blocking forever.",
                         threads=[t.index], locks=[lock.id],
                         evidence=self._owner_evidence(lock) + [
                             Evidence(ev.seq, ev.ts, t.index, "RW_FAILED", "lock returned EDEADLK", "trigger",
                                      lock.id, ev.caller)],
                         remedies=["Do not lock an rwlock again while holding it for writing; pass the 'already "
                                   "locked' state down instead (a *_locked() helper).",
                                   "Check the return value of pthread_rwlock_rdlock / pthread_rwlock_wrlock."])
            return
        what = ("the maximum number of read locks was exceeded" if rc == errno.EAGAIN else
                "the rwlock is not valid (uninitialised or destroyed)" if rc == errno.EINVAL else "the call failed")
        self._misuse(("rw_lock_error", t.index, lock.id, rc), "misuse", "warning",
                     f"Locking {lock.label} for {how} failed with {errno_name(rc)}",
                     f"{self.tlabel(t.index)} could not lock {self.llabel(lock.id)} for {how}: {errno_name(rc)} — "
                     f"{what}.",
                     threads=[t.index], locks=[lock.id],
                     evidence=[Evidence(ev.seq, ev.ts, t.index, "RW_FAILED", f"lock returned {errno_name(rc)}",
                                        "trigger", lock.id, ev.caller)],
                     remedies=["Check pthread return codes and handle errors explicitly."])

    def _on_rw_release(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._rwlock(ev.obj, ev.seq, kind=rw_kind(ev.extra))
        self._ann_lock = lock.id
        glibc_mode = rw_mode(ev.extra)  # derived from glibc's __cur_writer: a cross-check only
        if lock.owner == t.index:
            self.coverage.owner_checks += 1
            if glibc_mode != "w":
                self.coverage.owner_mismatches += 1
            self._release_fully(t, lock, ev.ts)
        elif t.index in lock.readers:
            self.coverage.owner_checks += 1
            if glibc_mode != "r":
                self.coverage.owner_mismatches += 1
            rh = lock.readers[t.index]
            rh.count -= 1
            if rh.count <= 0:
                self._drop_reader(lock, t.index, ev.ts)
        else:
            self._rw_unlock_not_held(t, lock, ev)
            return
        if lock.kind == RW_PREFER_WRITER_NONRECURSIVE:
            # queued readers now wait for the queued writers instead of the writer that left
            for wi in sorted(lock.waiters):
                self._check_cycle_from(wi, ev)

    def _rw_unlock_not_held(self, t: ThreadInfo, lock: LockInfo, ev: Ev) -> None:
        holders = lock.holders()
        if not holders:
            title = f"{t.label} unlocked {lock.label}, which was not locked"
            text = (f"{self.tlabel(t.index)} called pthread_rwlock_unlock on {self.llabel(lock.id)} although no thread "
                    "held it. glibc does not check this; the unlock corrupts the lock's reader count (undefined "
                    "behaviour).")
        else:
            title = f"{t.label} unlocked {lock.label}, which it does not hold"
            text = (f"{self.tlabel(t.index)} called pthread_rwlock_unlock on {self.llabel(lock.id)} while "
                    f"{self._holders_text(lock)} held it. glibc does not check ownership and treats the call as a read "
                    "unlock (undefined behaviour), so the real lock state no longer matches the holders ThreadLens "
                    "tracks.")
        # the holder state of this rwlock can no longer be trusted: no cycle through it is confirmed
        lock.owner_confirmed = False
        self._misuse(("rw_unlock_not_held", t.index, lock.id), "misuse", "warning", title, text,
                     threads=[t.index] + holders, locks=[lock.id],
                     evidence=[Evidence(ev.seq, ev.ts, t.index, "RW_RELEASE", "unlock without holding the rwlock",
                                        "trigger", lock.id, ev.caller)],
                     remedies=["Make every pthread_rwlock_unlock match exactly one successful rdlock/wrlock by the "
                               "same thread.",
                               "Keep lock and unlock in the same function, or use RAII guards such as "
                               "std::shared_lock / std::unique_lock in C++."])

    # ----------------------------------------------------- condition variables

    def _on_cond_begin(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.aux, ev.seq, kind=ev.extra or None)
        self._ann_lock = lock.id
        cond = self._cond(ev.obj)
        saved = None
        if lock.owner != t.index:
            self._misuse(("cond_no_mutex", t.index, lock.id), "misuse", "warning",
                         f"{t.label} waited on a condition variable without holding {lock.label}",
                         f"pthread_cond_wait requires the calling thread to hold the mutex; "
                         f"{self.tlabel(t.index)} did not own {self.llabel(lock.id)}.",
                         threads=[t.index], locks=[lock.id],
                         evidence=[Evidence(ev.seq, ev.ts, t.index, "COND_WAIT_BEGIN", "wait without mutex",
                                            "trigger", lock.id, ev.caller)],
                         remedies=["Lock the mutex, check the predicate in a loop, then wait."])
        else:
            if lock.count > 1:
                self._misuse(("cond_recursive", t.index, lock.id), "misuse", "warning",
                             f"{t.label} waited with recursive {lock.label} locked {lock.count} times",
                             "pthread_cond_wait releases a recursive mutex only once; other threads still "
                             "cannot acquire it, so the signaller may block forever.",
                             threads=[t.index], locks=[lock.id],
                             evidence=[Evidence(ev.seq, ev.ts, t.index, "COND_WAIT_BEGIN",
                                                f"recursion depth {lock.count}", "trigger", lock.id, ev.caller)],
                             remedies=["Do not wait on a condition variable while a recursive mutex is "
                                       "locked more than once."])
            saved = (lock.id, lock.count)
            self._release_fully(t, lock, ev.ts)
        t.cond_saved = saved
        if t.wait is not None:
            self._clear_wait(t)
        t.wait = Wait("cond", ev.seq, ev.ts, caller=ev.caller, timed=bool(ev.flags & Flag.TIMED),
                      lock=lock.id, cond=cond.addr)
        t.state = WAITING_COND
        cond.waiters.add(t.index)

    def _on_cond_end(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        lock = self._lock(ev.aux, ev.seq)
        self._ann_lock = lock.id
        cond = self._cond(ev.obj)
        w = t.wait
        if w is not None and w.kind == "cond":
            self._ann_corr = w.seq
        else:
            self._anomaly("cond_end_without_begin", ev, f"{t.label} ended a cond wait that never began")
        cond.waiters.discard(t.index)
        t.wait = None
        t.state = RUNNING
        if lock.owner is not None and lock.owner != t.index:
            self._anomaly("cond_reacquire_while_owned", ev,
                          f"{t.label} re-acquired {lock.label} still owned by {self.tlabel(lock.owner)}")
            self._release_fully(None, lock, ev.ts)
        count = t.cond_saved[1] if t.cond_saved else 1
        if lock.owner != t.index:
            self._record_lock_order(t, lock, ev, timed=False, via_cond=True)
            lock.owner = t.index
            lock.count = count
            lock.owner_since_seq = ev.seq
            lock.owner_since_ts = ev.ts
            lock.owner_caller = ev.caller
            lock.owner_confirmed = True
            t.held.append(Held(lock.id, ev.seq, ev.ts, ev.caller))
        t.cond_saved = None
        for wi in sorted(lock.waiters):
            self._check_cycle_from(wi, ev)

    def _on_cond_signal(self, ev: Ev, t: ThreadInfo | None) -> None:
        cond = self._cond(ev.obj)
        broadcast = ev.type == E.COND_BROADCAST
        if broadcast:
            cond.broadcasts += 1
        else:
            cond.signals += 1
        cond.last_signal_seq = ev.seq
        if not cond.waiters:
            cond.signals_without_waiters += 1
            cond.last_lost_signal_seq = ev.seq
            return
        for wi in sorted(cond.waiters):
            waiter = self.threads.get(wi)
            if waiter is None or waiter.wait is None or waiter.wait.kind != "cond":
                continue
            waiter.wait.signaled = "broadcast" if broadcast else (waiter.wait.signaled or "signal")
            waiter.wait.signal_seq = ev.seq
            self._check_cycle_from(wi, ev)

    # ---------------------------------------------------------------- barriers

    def _on_barrier_begin(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        t.wait = Wait("barrier", ev.seq, ev.ts, caller=ev.caller, barrier=ev.obj)
        t.state = BARRIER

    def _on_barrier_end(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        if t.wait is not None and t.wait.kind == "barrier":
            self._ann_corr = t.wait.seq
        t.wait = None
        t.state = RUNNING

    # -------------------------------------------------------------- semaphores
    # A semaphore has no owner: any thread may post it. A thread blocked in
    # sem_wait therefore waits for "somebody", not for a particular thread, and
    # gets NO wait-for edge (like a condition-variable wait). Semaphore waits
    # only contribute to stall detection.

    def _sem(self, addr: int, seq: int) -> SemInfo:
        s = self.sems.get(addr)
        if s is None:
            s = self.sems[addr] = SemInfo(addr, first_seq=seq)
            if self.resolver is not None:
                try:
                    s.symbol = self.resolver.data_symbol(addr)
                except Exception:  # symbolization must never break analysis
                    s.symbol = None
        return s

    def _sem_misuse(self, key: tuple, title: str, text: str, ev: Ev, t: ThreadInfo | None, type_: str,
                    what: str, remedies: list[str], threads: list[int] | None = None) -> None:
        self._misuse(key, "misuse", "warning", title, text,
                     threads=([t.index] if t else []) + (threads or []), locks=[],
                     evidence=[Evidence(ev.seq, ev.ts, t.index if t else None, type_, what, "trigger", None, ev.caller)],
                     remedies=remedies)

    def _on_sem_init(self, ev: Ev, t: ThreadInfo | None) -> None:
        old = self.sems.get(ev.obj)
        if ev.result != 0:
            self._sem_misuse(("sem_init_error", ev.obj, ev.result), f"sem_init failed with {errno_name(ev.result)}",
                             f"sem_init({ev.obj:#x}, value {ev.aux}) failed with {errno_name(ev.result)}"
                             + (" (value above SEM_VALUE_MAX)." if ev.result == errno.EINVAL else "."),
                             ev, t, "SEM_INIT", f"sem_init returned {errno_name(ev.result)}",
                             ["Check the return value of sem_init."])
            return
        if old is not None and not old.destroyed and old.waiters:
            self._sem_misuse(("sem_reinit", ev.obj), f"{old.label} re-initialised while threads wait on it",
                             f"sem_init was called on {old.label} while {len(old.waiters)} thread(s) were blocked in "
                             "sem_wait on it; this is undefined behaviour.",
                             ev, t, "SEM_INIT", "semaphore re-initialised",
                             ["Initialise each semaphore once before any thread uses it."], sorted(old.waiters))
        s = SemInfo(ev.obj, first_seq=ev.seq)
        if old is not None:
            s.annotation, s.symbol = old.annotation, old.symbol
            s.waiters = old.waiters
        elif self.resolver is not None:
            try:
                s.symbol = self.resolver.data_symbol(ev.obj)
            except Exception:
                s.symbol = None
        s.pshared = bool(ev.extra)
        # another process may post a process-shared semaphore: its count cannot be tracked
        s.init_value = None if s.pshared else ev.aux
        self.sems[ev.obj] = s

    def _on_sem_destroy(self, ev: Ev, t: ThreadInfo | None) -> None:
        s = self._sem(ev.obj, ev.seq)
        if ev.result != 0:
            self._sem_misuse(("sem_destroy_error", ev.obj, ev.result),
                             f"Destroying semaphore {s.label} failed with {errno_name(ev.result)}",
                             f"sem_destroy({s.label}) returned {errno_name(ev.result)}.", ev, t, "SEM_DESTROY",
                             f"sem_destroy returned {errno_name(ev.result)}", ["Check the return value of sem_destroy."])
            return
        if s.waiters:
            self._sem_misuse(("sem_destroy_waiters", ev.obj), f"Semaphore {s.label} destroyed while threads wait on it",
                             f"sem_destroy({s.label}) was called while {len(s.waiters)} thread(s) were blocked in "
                             "sem_wait on it. POSIX leaves this undefined; glibc does not detect it.",
                             ev, t, "SEM_DESTROY", "semaphore destroyed with waiters",
                             ["Destroy a semaphore only after every thread has stopped waiting on it."],
                             sorted(s.waiters))
        s.destroyed = True

    def _on_sem_wait_begin(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        s = self._sem(ev.obj, ev.seq)
        if t.wait is not None:
            self._anomaly("request_while_waiting", ev, f"{t.label} waited on a semaphore while already waiting")
            self._clear_wait(t)
        t.wait = Wait("sem", ev.seq, ev.ts, caller=ev.caller, timed=bool(ev.flags & Flag.TIMED), sem=s.addr)
        t.state = WAITING_SEM
        s.waiters.add(t.index)

    def _on_sem_wait_end(self, ev: Ev, t: ThreadInfo | None) -> None:
        if t is None:
            return
        s = self._sem(ev.obj, ev.seq)
        is_try = bool(ev.flags & Flag.TRY)
        if not is_try:
            w = t.wait
            if w is None or w.kind != "sem" or w.sem != s.addr:
                self._anomaly("sem_end_without_begin", ev, f"{t.label} finished a semaphore wait that never began")
            else:
                self._ann_corr = w.seq
            self._clear_wait(t)
        rc = ev.result
        if rc == 0:
            s.waits += 1
            if not is_try:
                s.wait_ns_total += ev.aux
                if ev.aux > s.wait_ns_max:
                    s.wait_ns_max = ev.aux
            if s.value is not None and s.value < 0:
                # SEM_POST is recorded before the real post, so this needs a post we never saw
                self._anomaly("sem_wait_without_post", ev,
                              f"{t.label} passed semaphore {s.label} although the trace had no post left for it")
                s.init_value = None  # count no longer known
            return
        if rc == errno.EAGAIN and is_try:
            s.try_failures += 1
        elif rc == errno.ETIMEDOUT:
            s.timeouts += 1
        elif rc == errno.EINTR:
            s.interrupted += 1
        else:
            what = "not a valid semaphore (uninitialised or destroyed)" if rc == errno.EINVAL else "the call failed"
            self._sem_misuse(("sem_wait_error", t.index, s.addr, rc), f"Waiting on {s.label} failed with {errno_name(rc)}",
                             f"{self.tlabel(t.index)} called sem_wait on {s.label} and got {errno_name(rc)}: {what}.",
                             ev, t, "SEM_WAIT_END", f"sem_wait returned {errno_name(rc)}",
                             ["Initialise a semaphore with sem_init before use and do not use it after sem_destroy.",
                              "Check the return value of sem_wait (and retry on EINTR)."])

    def _on_sem_post(self, ev: Ev, t: ThreadInfo | None) -> None:
        s = self._sem(ev.obj, ev.seq)
        s.posts += 1
        if t is not None:
            self._pending_post[t.index] = s.addr
        if not s.waiters:
            # nobody waits: the post is banked in the count (unlike a condition-variable
            # signal it is not lost), but it is worth showing on a stall
            s.posts_without_waiters += 1
            s.last_unwaited_post_seq = ev.seq

    def _on_sem_post_failed(self, ev: Ev, t: ThreadInfo | None) -> None:
        s = self._sem(ev.obj, ev.seq)
        if t is not None and self._pending_post.pop(t.index, None) == s.addr:
            s.posts -= 1  # the post recorded just before did not happen
        rc = ev.result
        what = ("the count would exceed SEM_VALUE_MAX" if rc == errno.EOVERFLOW else
                "not a valid semaphore (uninitialised or destroyed)" if rc == errno.EINVAL else "the call failed")
        self._sem_misuse(("sem_post_error", t.index if t else None, s.addr, rc),
                         f"Posting {s.label} failed with {errno_name(rc)}",
                         f"{self.tlabel(t.index) if t else 'A thread'} called sem_post on {s.label} and got "
                         f"{errno_name(rc)}: {what}.", ev, t, "SEM_POST_FAILED", f"sem_post returned {errno_name(rc)}",
                         ["Check the return value of sem_post.",
                          "A count that keeps growing means posts and waits are unbalanced."])

    def _on_mark(self, ev: Ev, t: ThreadInfo | None) -> None:
        self.marks.append({"seq": ev.seq, "ts": ev.ts, "thread": t.index if t else None, "text": ev.text})

    # ======================================================= wait-for graph

    def wait_edge(self, idx: int) -> WaitEdge | None:
        """The (single) outgoing wait-for edge of thread ``idx`` for a mutex, join or
        condition-variable wait, derived from state. rwlock waits can have several
        edges: see ``wait_edges``."""
        t = self.threads.get(idx)
        if t is None or not t.alive or t.wait is None:
            return None
        w = t.wait
        if w.kind == "lock":
            lock = self.locks.get(w.lock)
            if lock is None or lock.owner is None:
                return None
            if lock.owner == idx:
                if self._base_kind(lock.kind) in (1, 2):
                    return None  # recursive: succeeds; error-checking: returns EDEADLK
                return WaitEdge(idx, idx, "lock", lock.id, w.seq, w.ts, w.timed)
            return WaitEdge(idx, lock.owner, "lock", lock.id, w.seq, w.ts, w.timed)
        if w.kind == "join":
            target = self.by_handle.get(w.target_handle)
            if target is None or not target.alive or target is t:
                return None
            return WaitEdge(idx, target.index, "join", None, w.seq, w.ts)
        if w.kind == "cond" and w.signaled:
            lock = self.locks.get(w.lock)
            if lock is None or lock.owner is None or lock.owner == idx:
                return None
            return WaitEdge(idx, lock.owner, "cond_reacquire", lock.id, w.signal_seq or w.seq, w.ts,
                            w.timed, inferred=True)
        return None

    def wait_edges(self, idx: int) -> list[WaitEdge]:
        """All outgoing wait-for edges of thread ``idx``. A writer waiting on an rwlock
        has one edge per current reader (it needs all of them to release); every
        other wait has at most one edge."""
        t = self.threads.get(idx)
        if t is None or not t.alive or t.wait is None:
            return []
        w = t.wait
        if w.kind == "rwlock":
            lock = self.locks.get(w.lock)
            if lock is None:
                return []
            return [WaitEdge(idx, to, "rwlock", lock.id, w.seq, w.ts, w.timed, inferred, w.mode)
                    for to, inferred in self._rw_blockers(idx, lock, w.mode)]
        e = self.wait_edge(idx)
        return [e] if e is not None else []

    def _successor(self, idx: int) -> int | None:
        e = self.wait_edge(idx)
        return e.to if e else None

    def _successors(self, idx: int) -> list[int]:
        return [e.to for e in self.wait_edges(idx)]

    def _edge_between(self, frm: int, to: int) -> WaitEdge | None:
        for e in self.wait_edges(frm):
            if e.to == to:
                return e
        return None

    def wait_for_edges(self) -> list[WaitEdge]:
        return [e for i in self.threads for e in self.wait_edges(i)]

    def _check_cycle_from(self, idx: int, ev: Ev) -> None:
        if self._rw_waiters:
            # some thread waits for a SET of threads (an rwlock writer waits for every
            # reader): the graph is no longer functional, use depth-first search
            cyc = find_cycle_dfs(idx, self._successors)
        else:
            # fast path: every blocked thread waits for exactly one thread
            cyc = find_cycle_from(idx, self._successor)
        if cyc:
            self._on_wait_cycle(cyc, ev)

    def _cycle_edges(self, nodes: list[int]) -> list[WaitEdge] | None:
        """The edges nodes[0] → nodes[1] → … → nodes[0], or None if one no longer exists."""
        edges = []
        for i, frm in enumerate(nodes):
            e = self._edge_between(frm, nodes[(i + 1) % len(nodes)])
            if e is None:
                return None
            edges.append(e)
        return edges

    def _cycle_key(self, nodes: list[int]) -> frozenset | None:
        edges = self._cycle_edges(nodes)
        if edges is None:
            return None
        return frozenset((e.frm, e.to, e.kind, e.lock) for e in edges)

    def _on_wait_cycle(self, nodes: list[int], ev: Ev) -> None:
        nodes = list(canonical_cycle(nodes))
        edges = self._cycle_edges(nodes)
        if edges is None:
            return
        key = frozenset((e.frm, e.to, e.kind, e.lock) for e in edges)
        existing = self._cycle_keys.get(key)
        if existing is not None and self.findings[existing].status != "resolved":
            return
        inferred_kinds = {e.kind for e in edges if e.inferred}
        inferred = bool(inferred_kinds)
        timed = any(e.timed for e in edges)
        lock_ids = [e.lock for e in edges if e.lock is not None]
        ownership_ok = all(self.locks[lid].owner_confirmed for lid in lock_ids)
        self_loop = len(nodes) == 1
        has_join = any(e.kind == "join" for e in edges)
        has_rwlock = any(e.kind == "rwlock" for e in edges)
        rw_upgrade = self_loop and edges[0].kind == "rwlock"

        path = " → ".join(self.threads[n].label for n in nodes + [nodes[0]])
        if not self.coverage.complete or not ownership_ok:
            classification, severity, status = INCONCLUSIVE, "warning", "active"
            title = f"Wait-for cycle {path} observed, but the trace is incomplete"
        elif inferred:
            classification, severity, status = POTENTIAL_DEADLOCK, "warning", "active"
            why = ("condition-variable re-acquisition" if "cond_reacquire" in inferred_kinds else
                   "readers queued behind a waiting writer")
            title = f"Likely deadlock {path} (involves {why})"
        elif timed:
            classification, severity, status = POTENTIAL_DEADLOCK, "warning", "active"
            title = f"Cycle of timed waits {path} (will break by timeout)"
        else:
            confirmed_now = not self.cfg.confirm_requires_heartbeat
            classification = CONFIRMED_DEADLOCK if confirmed_now else POTENTIAL_DEADLOCK
            severity = "critical"
            status = "confirmed" if confirmed_now else "suspected"
            if rw_upgrade:
                title = (f"Self-deadlock: {self.threads[nodes[0]].label} tried to upgrade its read lock on "
                         f"{self.locks[edges[0].lock].label} to a write lock")
            elif self_loop:
                title = f"Self-deadlock: {self.threads[nodes[0]].label} re-locked a mutex it holds"
            else:
                title = f"Deadlock: {path}"

        evidence = self._cycle_evidence(edges, ev)
        explanation = self._cycle_explanation(nodes, edges, classification, timed, inferred)
        lock_labels = [self.locks[l].label for l in dict.fromkeys(lock_ids)]
        f = self._new_finding(
            kind="deadlock", classification=classification, severity=severity, title=title,
            summary=explanation[0] if explanation else title, status=status,
            threads=list(nodes), locks=list(dict.fromkeys(lock_ids)),
            cycle={"type": "wait_for", "nodes": [{"kind": "thread", "id": n} for n in nodes],
                   "edges": [e.as_dict() for e in edges], "path": path},
            evidence=evidence, explanation=explanation,
            remedies=deadlock_remedies(lock_labels, has_join, self_loop, has_rwlock=has_rwlock, rw_upgrade=rw_upgrade),
            limitations=self._deadlock_limitations(timed, inferred_kinds),
        )
        f.metrics["formed_seq"] = ev.seq
        f.metrics["formed_ts"] = ev.ts
        f.metrics["cycle_length"] = len(nodes)
        if status == "confirmed":
            f.confirmed_seq = self.seq
            f.confirmed_ts = self.now_ts
        self._cycle_keys[key] = f.id

    def _deadlock_limitations(self, timed: bool, inferred_kinds: set[str]) -> list[str]:
        out = []
        if timed:
            out.append("At least one wait in the cycle has a timeout (pthread_mutex_timedlock / "
                       "cond_timedwait / a timed rwlock call); the cycle resolves when it expires, so this is a "
                       "potential deadlock, not a permanent one.")
        if "cond_reacquire" in inferred_kinds:
            out.append("One edge is inferred: a thread woken from pthread_cond_wait must re-acquire its "
                       "mutex inside glibc, which ThreadLens cannot observe directly.")
        if "rwlock" in inferred_kinds:
            out.append("One edge is inferred: on a writer-preferring rwlock a new reader queues behind waiting "
                       "writers. That queue lives inside glibc and is not observed, so the edge is derived from "
                       "the documented policy rather than from an observed owner.")
        out.append("Confirmation means: every edge was an observed blocking wait, ownership came from "
                   "observed acquisitions, no events were lost, and the cycle persisted after the "
                   "collector proved the event stream was drained. It is confirmed under the observed "
                   "synchronization model, not by inspecting kernel state.")
        return out + list(COMMON_LIMITATIONS)

    def _owner_evidence(self, lock: LockInfo) -> list[Evidence]:
        if lock.owner is None or lock.owner_since_seq < 0:
            return []
        if lock.rw:
            return [Evidence(lock.owner_since_seq, lock.owner_since_ts, lock.owner, "RW_ACQUIRED",
                             f"{self.threads[lock.owner].label} acquired {lock.label} for writing", "cause", lock.id,
                             lock.owner_caller)]
        return [Evidence(lock.owner_since_seq, lock.owner_since_ts, lock.owner, "LOCK_ACQUIRED",
                         f"{self.threads[lock.owner].label} acquired {lock.label}", "cause", lock.id,
                         lock.owner_caller)]

    def _holder_evidence(self, lock: LockInfo, idx: int) -> list[Evidence]:
        """Acquisition that made thread ``idx`` a holder of ``lock`` (owner/writer or reader)."""
        if lock.owner == idx:
            return self._owner_evidence(lock)
        rh = lock.readers.get(idx)
        if rh is None:
            return []
        return [Evidence(rh.seq, rh.ts, idx, "RW_ACQUIRED", f"{self.threads[idx].label} acquired {lock.label} for reading",
                         "cause", lock.id, rh.caller)]

    def _holders_text(self, lock: LockInfo) -> str:
        if lock.owner is not None:
            return self.tlabel(lock.owner) + (" (for writing)" if lock.rw else "")
        if lock.readers:
            return "reader" + ("s " if len(lock.readers) > 1 else " ") + ", ".join(
                self.tlabel(r) for r in sorted(lock.readers))
        return "nobody"

    def _held_text(self, t: ThreadInfo) -> str:
        parts = []
        for h in t.held:
            lock = self.locks[h.lock]
            parts.append(lock.label + {"r": " (read)", "w": " (write)"}.get(h.mode, ""))
        return ", ".join(parts) or "no locks"

    def _cycle_evidence(self, edges: list[WaitEdge], ev: Ev) -> list[Evidence]:
        items: dict[int, Evidence] = {}
        for e in edges:
            waiter = self.threads[e.frm]
            w = waiter.wait
            if e.kind == "lock":
                lock = self.locks[e.lock]
                timed = " (timed)" if e.timed else ""
                items.setdefault(e.since_seq, Evidence(e.since_seq, e.since_ts, e.frm, "LOCK_REQUEST",
                                                       f"{waiter.label} requests {lock.label}{timed} and blocks",
                                                       "cause", lock.id, w.caller if w else 0))
                for ov in self._owner_evidence(lock):
                    items.setdefault(ov.seq, ov)
                if e.frm == e.to:
                    items[lock.owner_since_seq].description += " (first time)"
            elif e.kind == "join":
                target = self.threads[e.to]
                items.setdefault(e.since_seq, Evidence(e.since_seq, e.since_ts, e.frm, "JOIN_BEGIN",
                                                       f"{waiter.label} calls pthread_join({target.label})",
                                                       "cause", None, w.caller if w else 0))
            elif e.kind == "rwlock":
                lock = self.locks[e.lock]
                timed = " (timed)" if e.timed else ""
                items.setdefault(e.since_seq, Evidence(e.since_seq, e.since_ts, e.frm, "RW_REQUEST",
                                                       f"{waiter.label} requests {lock.label} for "
                                                       f"{RW_MODE_WORDS.get(e.mode, '?')}{timed} and blocks",
                                                       "cause", lock.id, w.caller if w else 0))
                for ov in self._holder_evidence(lock, e.to):
                    items.setdefault(ov.seq, ov)
            else:  # cond_reacquire
                lock = self.locks[e.lock]
                items.setdefault(e.since_seq, Evidence(e.since_seq, e.since_ts, e.to, "COND_SIGNAL",
                                                       f"signal wakes {waiter.label}, which must re-acquire {lock.label}",
                                                       "cause", lock.id))
                for ov in self._owner_evidence(lock):
                    items.setdefault(ov.seq, ov)
        if ev.seq not in items:
            items[ev.seq] = Evidence(ev.seq, ev.ts, self.by_tid[ev.tid].index if ev.tid in self.by_tid else None,
                                     E(ev.type).name, self.describe(ev), "trigger", self._ann_lock, ev.caller)
        else:
            items[ev.seq].role = "trigger"
        return [items[k] for k in sorted(items)]

    def _cycle_explanation(self, nodes: list[int], edges: list[WaitEdge], classification: str,
                           timed: bool, inferred: bool) -> list[str]:
        out = []
        if len(nodes) == 1:
            e = edges[0]
            t = self.threads[e.frm]
            lock = self.locks[e.lock]
            if e.kind == "rwlock":
                rh = lock.readers.get(e.frm)
                out.append(f"{t.label} holds {lock.label} for reading (acquired at seq {rh.seq if rh else '?'}) and "
                           f"requested it for writing at seq {e.since_seq}. A writer must wait until EVERY reader "
                           f"has released the lock — including {t.label} itself, which cannot release while it "
                           "waits. glibc rwlocks cannot be upgraded in place, so the thread waits for itself forever.")
                others = [r for r in sorted(lock.readers) if r != e.frm]
                if others:
                    out.append(f"Other readers ({', '.join(self.threads[r].label for r in others)}) also hold "
                               f"{lock.label}; even after they release it, {t.label}'s own read lock keeps it blocked.")
                return out
            out.append(f"{t.label} already owns {lock.label} (a {mutex_kind_name(lock.kind)} mutex, acquired at "
                       f"seq {lock.owner_since_seq}) and requested it again at seq {e.since_seq}. A normal glibc "
                       "mutex is not re-entrant, so the thread waits for itself and can never continue.")
            return out
        for e in edges:
            waiter = self.threads[e.frm]
            owner = self.threads[e.to]
            held = self._held_text(waiter)
            if e.kind == "lock":
                lock = self.locks[e.lock]
                out.append(f"{waiter.label} holds {held} and is blocked requesting {lock.label} "
                           f"(since seq {e.since_seq}); {lock.label} is held by {owner.label} "
                           f"(acquired at seq {lock.owner_since_seq}).")
            elif e.kind == "rwlock":
                lock = self.locks[e.lock]
                if lock.owner == e.to:
                    holder = f"{owner.label} holds it for writing (acquired at seq {lock.owner_since_seq})"
                elif e.inferred:
                    holder = f"{owner.label} is a writer queued ahead of it on this writer-preferring rwlock"
                else:
                    rh = lock.readers.get(e.to)
                    holder = f"{owner.label} holds it for reading (acquired at seq {rh.seq if rh else '?'})"
                line = (f"{waiter.label} holds {held} and is blocked requesting {lock.label} for "
                        f"{RW_MODE_WORDS.get(e.mode, '?')} (since seq {e.since_seq}); {holder}.")
                if e.mode == "w" and len(lock.readers) > 1:
                    line += (f" A writer needs every reader to release: {len(lock.readers)} threads read "
                             f"{lock.label} right now, and {owner.label} is one that never will.")
                out.append(line)
            elif e.kind == "join":
                out.append(f"{waiter.label} holds {held} and is blocked in pthread_join() waiting for "
                           f"{owner.label} to exit (since seq {e.since_seq}).")
            else:
                lock = self.locks[e.lock]
                out.append(f"{waiter.label} was signalled (seq {e.since_seq}) and must re-acquire {lock.label} "
                           f"before pthread_cond_wait returns; {lock.label} is held by {owner.label}.")
        path = " → ".join(self.threads[n].label for n in nodes + [nodes[0]])
        out.append(f"Each thread waits for the next one around the cycle {path}, so none of them can proceed: "
                   "a circular wait.")
        if classification in (CONFIRMED_DEADLOCK, POTENTIAL_DEADLOCK) and not timed and not inferred:
            if any(e.kind == "rwlock" for e in edges):
                out.append("All four Coffman conditions hold: mutual exclusion (a write lock excludes every other "
                           "holder, so a writer waits for all readers), hold and wait (each thread keeps what it "
                           "holds while requesting more), no preemption (a lock cannot be taken away from its "
                           "holders) and circular wait (the cycle above).")
            else:
                out.append("All four Coffman conditions hold: mutual exclusion (a mutex has one owner), hold and "
                           "wait (each thread keeps its lock while requesting another), no preemption (a pthread "
                           "mutex cannot be taken away from its owner) and circular wait (the cycle above).")
        return out

    def _review_cycles(self, heartbeat_seq: int | None, end_of_stream: bool = False) -> None:
        for key, fid in list(self._cycle_keys.items()):
            f = self.findings[fid]
            if f.status in ("resolved",) or f.status == "confirmed":
                continue
            present = self._cycle_key(f.threads) == key
            if f.status == "suspected":
                if not self.coverage.complete:
                    self._coverage_downgrade("incomplete coverage")
                    continue
                drained = end_of_stream or (heartbeat_seq is not None and heartbeat_seq > f.metrics["formed_seq"])
                if present and drained:
                    f.status = "confirmed"
                    f.classification = CONFIRMED_DEADLOCK
                    f.confirmed_seq = self.seq
                    f.confirmed_ts = self.now_ts
                    f.metrics["confirmation_delay_ns"] = self.now_ts - f.metrics["formed_ts"]
                    f.metrics["confirmed_by"] = "end of event stream" if end_of_stream else "collector heartbeat"
                    self._touch(f)
                elif not present:
                    f.status = "resolved"
                    f.severity = "warning"
                    f.resolved_seq = self.seq
                    f.title = "Transient wait-for cycle (resolved before confirmation)"
                    self._touch(f)
            elif not present:
                f.status = "resolved"
                f.resolved_seq = self.seq
                self._touch(f)

    def active_cycle_threads(self) -> set[int]:
        out: set[int] = set()
        for fid in self._cycle_keys.values():
            f = self.findings[fid]
            if f.status in ("confirmed", "suspected", "active"):
                out.update(f.threads)
        return out

    # ===================================================== lock-order graph

    def _on_lock_order_cycle(self, cyc: list[int]) -> None:
        key = canonical_cycle(cyc)
        if key in self._lo_keys:
            return
        cyc = list(key)
        edges = self.lock_order.cycle_edges(cyc)
        names = " → ".join(self.locks[l].label for l in cyc + [cyc[0]])
        threads = sorted(set().union(*(e.threads for e in edges)))
        evidence = []
        for e in edges:
            t = self.threads.get(e.first_thread)
            tl = t.label if t else f"T{e.first_thread}"
            evidence.append(Evidence(e.first_seq_src, 0, e.first_thread, "LOCK_ACQUIRED",
                                     f"{tl} acquired {self.locks[e.src].label}", "context", e.src, e.first_caller_src))
            evidence.append(Evidence(e.first_seq_dst, e.first_ts, e.first_thread, "LOCK_ACQUIRED",
                                     f"{tl} acquired {self.locks[e.dst].label} while holding {self.locks[e.src].label}",
                                     "cause", e.dst, e.first_caller_dst))
        evidence.sort(key=lambda x: x.seq)
        explanation = []
        for e in edges:
            who = ", ".join(self.threads[i].label for i in sorted(e.threads) if i in self.threads)
            explanation.append(f"Order {self.locks[e.src].label} → {self.locks[e.dst].label} was observed "
                               f"{e.count}× (in {who}; first at seq {e.first_seq_src} → {e.first_seq_dst}).")
        explanation.append(f"Together these orders form the cycle {names} in the lock-order graph. If the "
                           "code paths run concurrently, each thread can hold its first lock while waiting for "
                           "the other's, which is a deadlock.")
        explanation.append("In THIS execution the critical sections did not overlap in the fatal way, so no "
                           "thread actually blocked forever: this is a predicted (potential) deadlock, not an "
                           "observed one.")
        complete = self.coverage.complete
        if not complete:
            explanation.append("The trace is incomplete (events were lost or missing), so an order may have been "
                               "inferred from a release that was never seen; this cycle is reported as inconclusive.")
        f = self._new_finding(
            kind="lock_order", classification=POTENTIAL_DEADLOCK if complete else INCONCLUSIVE, severity="warning",
            title=(f"Lock-order inversion: {names}" if complete else f"Possible lock-order cycle (incomplete trace): {names}"),
            summary=explanation[-2] if complete else explanation[-1], status="active",
            threads=threads, locks=cyc,
            cycle={"type": "lock_order", "nodes": [{"kind": "lock", "id": l} for l in cyc],
                   "edges": [e.as_dict() for e in edges], "path": names},
            evidence=evidence, explanation=explanation, remedies=list(LOCK_ORDER_REMEDIES),
            limitations=["The lock-order graph records orders that happened in this execution; a cycle "
                         "means a deadlock is possible, not that it must happen.",
                         "ThreadLens applies two classic false-positive filters (single-thread cycles and "
                         "gate locks) but does not prove the conflicting code paths can run concurrently "
                         "(happens-before is not analysed)."] + list(COMMON_LIMITATIONS))
        self._lo_keys[key] = f.id
        for e in edges:
            self._lo_edge_findings.setdefault((e.src, e.dst), set()).add(f.id)
        self._reevaluate_lock_order(f.id)

    def _reevaluate_lock_order(self, fid: str) -> None:
        f = self.findings[fid]
        cyc = [n["id"] for n in f.cycle["nodes"]]
        edges = self.lock_order.cycle_edges(cyc)
        reason = None
        threads = set().union(*(e.threads for e in edges))
        if len(threads) == 1:
            only = self.threads.get(next(iter(threads)))
            reason = (f"single-thread cycle: every order in the cycle was observed only in "
                      f"{only.label if only else 'one thread'}, and one thread cannot deadlock with itself "
                      "through lock order alone")
        else:
            guards = None
            for e in edges:
                g = e.guards or frozenset()
                guards = g if guards is None else guards & g
            if guards:
                names = ", ".join(self.locks[g].label for g in sorted(guards))
                reason = (f"gate lock: {names} was held during every observed acquisition in the cycle, "
                          "which serializes these code paths")
            else:
                shared = self._shared_mode_link(edges)
                if shared is not None:
                    reason = (f"shared-mode cycle: {self.locks[shared].label} is only ever held and requested for "
                              "reading along this cycle, and read locks do not exclude each other, so the circular "
                              "wait cannot form")
        timed = any(e.timed_only for e in edges)
        changed = False
        if reason and f.status != "filtered":
            f.status, f.severity, f.filter_reason, changed = "filtered", "info", reason, True
        elif not reason and f.status == "filtered":
            f.status, f.severity, f.filter_reason, changed = "active", "warning", None, True
        f.metrics["timed_edges"] = timed
        f.threads = sorted(threads)
        f.cycle["edges"] = [e.as_dict() for e in edges]
        if changed:
            self._touch(f)

    def _shared_mode_link(self, edges: list) -> int | None:
        """A lock on the cycle at which no wait can occur: edges[i] requests it only for
        reading and edges[i+1] holds it only for reading. Two read holds never block
        each other on a reader-preferring rwlock (glibc's default), so a lock-order cycle
        through such a link cannot become a circular wait. Writer-preferring rwlocks are
        excluded: there a read request can queue behind a third, waiting writer."""
        for i, e in enumerate(edges):
            nxt = edges[(i + 1) % len(edges)]
            lock = self.locks.get(e.dst)
            if lock is not None and lock.rw and lock.kind != RW_PREFER_WRITER_NONRECURSIVE \
                    and e.dst_modes == {"r"} and nxt.src_modes == {"r"}:
                return e.dst
        return None

    # ========================================================= contention

    def _maybe_contention(self, lock: LockInfo) -> None:
        if lock.id in self._contention_keys or lock.acquisitions < self.cfg.contention_min_acquisitions:
            return
        ratio = lock.contended / lock.acquisitions
        if ratio >= self.cfg.contention_ratio and lock.wait_ns_total >= self.cfg.contention_total_wait_ms * 1e6:
            self._contention_finding(lock, "ratio")

    def _contention_finding(self, lock: LockInfo, reason: str, waiter: ThreadInfo | None = None) -> Finding:
        fid = self._contention_keys.get(lock.id)
        if fid is not None:
            return self.findings[fid]
        if reason == "long_wait" and waiter is not None:
            summary = (f"{waiter.label} has been waiting for {lock.label} for a long time while "
                       f"{self._holders_text(lock)} holds it; no wait-for cycle is involved.")
        else:
            summary = (f"{lock.label}: {lock.contended} of {lock.acquisitions} acquisitions had to wait for another "
                       f"thread; total waiting time {fmt_ms(lock.wait_ns_total)}.")
        f = self._new_finding(
            kind="contention", classification=EXCESSIVE_CONTENTION, severity="warning",
            title=f"Excessive waiting on {lock.label}", summary=summary, status="active",
            threads=[], locks=[lock.id], evidence=[], explanation=[summary],
            remedies=list(CONTENTION_REMEDIES),
            limitations=["Contention is a performance problem, not a deadlock: every waiting thread "
                         "eventually acquired the lock (or is still able to).",
                         "Wait times are measured inside the target process (request → acquired) and "
                         "include the collector's own small overhead."] + list(COMMON_LIMITATIONS))
        f.metrics["trigger"] = reason
        self._contention_keys[lock.id] = f.id
        self._update_contention(f, lock)
        return f

    def _update_contention(self, f: Finding, lock: LockInfo) -> None:
        ratio = lock.contended / lock.acquisitions if lock.acquisitions else 0.0
        waiters_now = sorted(lock.waiters)
        f.metrics.update(
            acquisitions=lock.acquisitions, contended=lock.contended, contended_ratio=round(ratio, 3),
            total_wait_ns=lock.wait_ns_total,
            avg_wait_ns=int(lock.wait_ns_total / lock.acquisitions) if lock.acquisitions else 0,
            max_wait_ns=lock.wait_ns_max, max_hold_ns=lock.hold_ns_max,
            avg_hold_ns=int(lock.hold_ns_total / lock.holds) if lock.holds else 0,
            try_failures=lock.try_failures, timeouts=lock.timeouts, waiting_now=len(waiters_now))
        threads = {t.index for t in self.threads.values() if any(h.lock == lock.id for h in t.held)}
        threads.update(waiters_now)
        f.threads = sorted(set(f.threads) | threads)
        f.explanation = [
            f"{lock.label} was acquired {lock.acquisitions} times; {lock.contended} acquisitions "
            f"({ratio:.0%}) found it held by another thread.",
            f"Total waiting time {fmt_ms(lock.wait_ns_total)}, average {fmt_ms(f.metrics['avg_wait_ns'])}, "
            f"longest single wait {fmt_ms(lock.wait_ns_max)}.",
            f"The lock is held for {fmt_ms(f.metrics['avg_hold_ns'])} on average (longest "
            f"{fmt_ms(lock.hold_ns_max)}): long critical sections serialize the threads.",
        ]
        if lock.owner is not None and lock.owner_since_seq >= 0 and not f.evidence:
            f.evidence = self._owner_evidence(lock)
        self._touch(f)

    def _refresh_contention(self, force: bool = False) -> None:
        for lid, fid in self._contention_keys.items():
            self._update_contention(self.findings[fid], self.locks[lid])
        if force:
            for lock in self.locks.values():
                self._maybe_contention(lock)

    def _check_long_waits(self) -> None:
        limit = self.cfg.long_wait_ms * 1e6
        explained = self.active_cycle_threads() | self._stalled_threads()
        for t in self.threads.values():
            w = t.wait
            if w is None or w.kind not in ("lock", "rwlock") or not t.alive or t.index in explained:
                continue
            lock = self.locks.get(w.lock)
            if lock is None:
                continue
            if w.kind == "lock":
                if lock.owner is None or lock.owner == t.index:
                    continue
                blockers = [lock.owner]
            else:
                blockers = [b for b, inferred in self._rw_blockers(t.index, lock, w.mode)
                            if not inferred and b != t.index]
                if not blockers:
                    continue
            if self.now_ts - w.ts >= limit:
                if any(b in self.threads and not self.threads[b].alive for b in blockers):
                    continue  # reported as an abandoned-lock wait
                f = self._contention_finding(lock, "long_wait", t)
                if t.index not in f.threads:
                    f.threads.append(t.index)
                f.metrics["longest_current_wait_ns"] = max(f.metrics.get("longest_current_wait_ns", 0),
                                                           self.now_ts - w.ts)
                self._touch(f)

    # ============================================================ stall

    def _blocked_untimed(self, t: ThreadInfo) -> bool:
        w = t.wait
        if w is None or w.timed:
            return False
        if w.kind == "lock":
            return self.wait_edge(t.index) is not None or (
                self.locks[w.lock].owner is not None)
        if w.kind == "rwlock":
            lock = self.locks.get(w.lock)
            return lock is not None and bool(self._rw_blockers(t.index, lock, w.mode))
        if w.kind == "join":
            target = self.by_handle.get(w.target_handle)
            return target is not None and target.alive
        if w.kind == "cond":
            return not w.signaled
        return w.kind in ("sem", "barrier")

    def _check_stall(self) -> None:
        # a stall that ended (progress resumed) is downgraded
        for key, fid in list(self._stall_keys.items()):
            f = self.findings[fid]
            if f.status == "active" and self.last_inband_ts > f.detected_ts:
                f.status, f.severity, f.resolved_seq = "resolved", "info", self.seq
                f.title = "Temporary stall (progress resumed)"
                self._touch(f)
                # the long waits it explained turned out to end: they were real contention
                for cfid in self._contention_keys.values():
                    cf = self.findings[cfid]
                    if cf.status == "filtered" and cf.metrics.get("filtered_by_stall") == fid:
                        cf.status, cf.severity, cf.filter_reason = "active", "warning", None
                        cf.metrics.pop("filtered_by_stall", None)
                        self._touch(cf)
        live = [t for t in self.threads.values() if t.alive]
        if not live or self.ended:
            return
        if self.now_ts - self.last_inband_ts < self.cfg.stall_ms * 1e6:
            return
        if not all(self._blocked_untimed(t) for t in live):
            return
        if self.active_cycle_threads() & {t.index for t in live}:
            return  # explained by a wait-for cycle
        key = frozenset(t.index for t in live)
        if key in self._stall_keys and self.findings[self._stall_keys[key]].status == "active":
            return
        evidence = []
        explanation = [f"For {fmt_ms(self.now_ts - self.last_inband_ts)} no thread made progress and every live "
                       "thread is blocked in an untimed wait:"]
        for t in live:
            w = t.wait
            if w.kind == "cond":
                cond = self.conds.get(w.cond)
                lock = self.locks.get(w.lock)
                evidence.append(Evidence(w.seq, w.ts, t.index, "COND_WAIT_BEGIN",
                                         f"{t.label} waits on condition variable {w.cond:#x} "
                                         f"(mutex {lock.label if lock else '?'})", "cause", w.lock, w.caller))
                line = f"{t.label} waits on condition variable {w.cond:#x} and no signal has arrived since."
                if cond is not None and cond.last_lost_signal_seq is not None and cond.last_lost_signal_seq < w.seq:
                    evidence.append(Evidence(cond.last_lost_signal_seq, 0, None, "COND_SIGNAL",
                                             "signal sent when no thread was waiting (lost wake-up?)", "cause"))
                    line += (f" A signal on the same condition variable at seq {cond.last_lost_signal_seq} "
                             "happened BEFORE this wait began and found no waiter — the classic lost "
                             "wake-up pattern (waiting without re-checking the predicate).")
                explanation.append(line)
            elif w.kind == "join":
                target = self.by_handle.get(w.target_handle)
                evidence.append(Evidence(w.seq, w.ts, t.index, "JOIN_BEGIN",
                                         f"{t.label} joins {target.label if target else '?'}", "context", None, w.caller))
                explanation.append(f"{t.label} is blocked joining {target.label if target else '?'}.")
            elif w.kind == "lock":
                lock = self.locks[w.lock]
                evidence.append(Evidence(w.seq, w.ts, t.index, "LOCK_REQUEST",
                                         f"{t.label} waits for {lock.label}", "cause", lock.id, w.caller))
                explanation.append(f"{t.label} waits for {lock.label} held by {self.tlabel(lock.owner)}.")
            elif w.kind == "rwlock":
                lock = self.locks[w.lock]
                how = RW_MODE_WORDS.get(w.mode, "?")
                evidence.append(Evidence(w.seq, w.ts, t.index, "RW_REQUEST",
                                         f"{t.label} waits for {lock.label} ({how})", "cause", lock.id, w.caller))
                explanation.append(f"{t.label} waits to lock {lock.label} for {how}; it is held by "
                                   f"{self._holders_text(lock)}.")
            elif w.kind == "sem":
                sem = self.sems.get(w.sem)
                label = sem.label if sem else f"sem@{w.sem:#x}"
                evidence.append(Evidence(w.seq, w.ts, t.index, "SEM_WAIT_BEGIN",
                                         f"{t.label} waits on semaphore {label}", "cause", None, w.caller))
                line = f"{t.label} waits on semaphore {label} and no post has let it through."
                if sem is not None and sem.value is not None:
                    line += (f" The count implied by the trace is {sem.value}: initial value {sem.init_value} + "
                             f"{sem.posts} post(s) − {sem.waits} successful wait(s).")
                explanation.append(line)
            else:
                evidence.append(Evidence(w.seq, w.ts, t.index, "BARRIER_WAIT_BEGIN",
                                         f"{t.label} waits at barrier {w.barrier:#x}", "context", None, w.caller))
                explanation.append(f"{t.label} waits at a barrier that not enough threads reached.")
        explanation.extend(self._ownerless_chain_notes(live))
        sems = [t.wait.sem for t in live if t.wait.kind == "sem"]
        explanation.append("There is no cycle in the wait-for graph, so this is not a lock deadlock. ThreadLens "
                           "cannot know which thread was supposed to "
                           + ("post a semaphore or signal a condition variable" if sems else
                              "signal a condition variable")
                           + ", so the root cause is reported as inconclusive.")
        evidence.sort(key=lambda e: e.seq)
        f = self._new_finding(
            kind="stall", classification=STALL, severity="critical",
            title="Program stalled: every thread is blocked (no lock cycle)",
            summary=explanation[0], status="active", threads=sorted(key),
            locks=list(dict.fromkeys(t.wait.lock for t in live if t.wait.kind in ("lock", "rwlock"))),
            evidence=evidence, explanation=explanation,
            remedies=(list(SEM_STALL_REMEDIES) if sems else []) + list(STALL_REMEDIES),
            limitations=["A stall is inferred from the absence of events; a thread busy in code that makes "
                         "no pthread calls would also look silent, but such a thread would not be in a wait "
                         "state.",
                         "Condition-variable and semaphore waits have no owner relationship (any thread may "
                         "signal or post), so they cannot be proven with a wait-for graph."] + list(COMMON_LIMITATIONS))
        f.metrics["silent_ns"] = self.now_ts - self.last_inband_ts
        if sems:
            f.metrics["semaphores"] = [self.sems[s].label if s in self.sems else f"sem@{s:#x}"
                                       for s in dict.fromkeys(sems)]
        self._stall_keys[key] = f.id
        # a long lock wait inside the stall is a symptom of the hang, not a performance problem
        for lid in f.locks:
            cfid = self._contention_keys.get(lid)
            cf = self.findings.get(cfid) if cfid else None
            if cf is not None and cf.metrics.get("trigger") == "long_wait" and cf.status == "active":
                cf.status, cf.severity = "filtered", "info"
                cf.filter_reason = (f"part of stall {f.id}: the lock's holder is itself blocked, so this wait is a "
                                    "symptom of the hang rather than a performance problem")
                cf.metrics["filtered_by_stall"] = f.id
                self._touch(cf)

    def _stalled_threads(self) -> set[int]:
        out: set[int] = set()
        for fid in self._stall_keys.values():
            f = self.findings[fid]
            if f.status == "active":
                out.update(f.threads)
        return out

    def _ownerless_chain_notes(self, live: list[ThreadInfo]) -> list[str]:
        """Explain wait-for chains that END at a thread waiting on a semaphore or condition
        variable: the lock holder is itself waiting for a post/signal, which has no owner,
        so the chain cannot close into a cycle."""
        out = []
        for t in live:
            if not self.wait_edges(t.index):
                continue
            seen = {t.index}
            node = t.index
            while True:
                nxt = next(iter(self._successors(node)), None)
                if nxt is None or nxt in seen:
                    break
                seen.add(nxt)
                node = nxt
            end = self.threads.get(node)
            if end is None or end is t or end.wait is None or end.wait.kind not in ("sem", "cond"):
                continue
            if end.wait.kind == "sem":
                sem = self.sems.get(end.wait.sem)
                noun, name, act = "semaphore", sem.label if sem else hex(end.wait.sem), "a post"
            else:
                noun, name, act = "condition variable", hex(end.wait.cond), "a signal"
            out.append(f"{t.label} ultimately waits for {end.label}, which holds {self._held_text(end)} and is "
                       f"itself waiting on {noun} {name}. A {noun} has no owner, so the wait-for graph ends at "
                       f"{end.label} instead of closing a cycle: {end.label} needs {act} that no running thread "
                       "can make (every live thread is blocked).")
        return out

    # ========================================================== misuse

    def _misuse(self, key: tuple, kind: str, severity: str, title: str, text: str, *, threads: list[int],
                locks: list[int], evidence: list[Evidence], remedies: list[str]) -> Finding:
        fid = self._misuse_keys.get(key)
        if fid is not None:
            f = self.findings[fid]
            f.occurrences += 1
            if len(f.evidence) < 12:
                f.evidence.extend(e for e in evidence if e.role == "trigger")
            self._touch(f)
            return f
        f = self._new_finding(kind=kind, classification=API_MISUSE, severity=severity, title=title,
                              summary=text, status="active", threads=list(dict.fromkeys(threads)),
                              locks=list(locks), evidence=sorted(evidence, key=lambda e: e.seq),
                              explanation=[text], remedies=remedies)
        self._misuse_keys[key] = f.id
        return f

    def _abandoned_wait(self, t: ThreadInfo, lock: LockInfo, ev: Ev, holder: int | None = None) -> None:
        if not lock.rw and lock.kind & MUTEX_ROBUST_BIT:
            return  # glibc hands a robust mutex over with EOWNERDEAD: not a permanent block
        holder = lock.owner if holder is None else holder
        owner = self.threads.get(holder) if holder is not None else None
        timed = bool(t.wait and t.wait.timed)
        if lock.rw:
            how = " for writing" if lock.owner == holder else " for reading"
            permanent = "A read-write lock held by a dead thread is never released: this thread is blocked permanently."
            request_type = "RW_REQUEST"
            remedies = ["Unlock every read-write lock on every thread exit path (RAII / cleanup handlers)."]
        else:
            how = ""
            permanent = "A normal mutex can never be released now: this thread is blocked permanently."
            request_type = "LOCK_REQUEST"
            remedies = ["Unlock every mutex on every thread exit path (RAII / cleanup handlers).",
                        "Use a robust mutex if threads may die while holding it."]
        self._misuse(("abandoned_wait", t.index, lock.id), "abandoned", "warning" if timed else "critical",
                     f"{t.label} waits for {lock.label}, held by exited {owner.label if owner else 'thread'}",
                     f"{self.tlabel(t.index)} requested {self.llabel(lock.id)}, but its "
                     f"{'holder' if lock.rw else 'owner'} {self.tlabel(holder)} has already exited without "
                     f"unlocking it{how}. "
                     + ("The timed request will fail with ETIMEDOUT." if timed else permanent),
                     threads=[t.index] + ([owner.index] if owner else []), locks=[lock.id],
                     evidence=(self._holder_evidence(lock, holder) if holder is not None else []) + ([
                         Evidence(owner.ended_seq, owner.ended_ts or 0, owner.index, "THREAD_EXIT",
                                  f"{owner.label} exited holding {lock.label}", "cause")] if owner and owner.ended_seq is not None else []) + [
                         Evidence(t.wait.seq if t.wait else ev.seq, t.wait.ts if t.wait else ev.ts, t.index,
                                  request_type, f"{t.label} requests {lock.label}", "trigger", lock.id,
                                  t.wait.caller if t.wait else 0)],
                     remedies=remedies)

    # ======================================================== descriptions

    def describe(self, ev: Ev) -> str:
        t = self.by_tid.get(ev.tid)
        who = t.label if t else (f"tid {ev.tid}" if ev.tid else "collector")
        lid = self.lock_by_addr.get(ev.obj)
        lname = self.locks[lid].label if lid is not None else f"{ev.obj:#x}"
        typ = ev.type
        timed = " (timed)" if ev.flags & Flag.TIMED else ""
        if typ == E.LOCK_REQUEST:
            return f"{who} requests {lname}{timed}"
        if typ == E.LOCK_ACQUIRED:
            if ev.flags & Flag.TRY:
                return f"{who} acquired {lname} with trylock"
            return f"{who} acquired {lname} after {fmt_ms(ev.aux)}"
        if typ == E.LOCK_FAILED:
            how = "trylock" if ev.flags & Flag.TRY else "lock"
            return f"{who}: {how} on {lname} failed ({errno_name(ev.result)})"
        if typ == E.LOCK_RELEASE:
            return f"{who} releases {lname}"
        if typ == E.UNLOCK_FAILED:
            return f"{who}: unlock of {lname} failed ({errno_name(ev.result)})"
        if typ in (E.COND_WAIT_BEGIN, E.COND_WAIT_END):
            mid = self.lock_by_addr.get(ev.aux)
            mname = self.locks[mid].label if mid is not None else f"{ev.aux:#x}"
            if typ == E.COND_WAIT_BEGIN:
                return f"{who} waits on cond {ev.obj:#x}{timed}, releasing {mname}"
            extra = " (inferred: cancelled)" if ev.flags & Flag.INFERRED else ""
            return f"{who} woke from cond {ev.obj:#x} ({errno_name(ev.result)}), re-acquired {mname}{extra}"
        if typ == E.COND_SIGNAL:
            return f"{who} signals cond {ev.obj:#x}"
        if typ == E.COND_BROADCAST:
            return f"{who} broadcasts cond {ev.obj:#x}"
        if typ in (E.JOIN_BEGIN, E.JOIN_END, E.THREAD_CREATE, E.THREAD_DETACH, E.CANCEL_REQUEST):
            target = self.by_handle.get(ev.obj)
            tname = target.label if target else f"thread {ev.obj:#x}"
            return {E.JOIN_BEGIN: f"{who} joins {tname}",
                    E.JOIN_END: f"{who} finished joining {tname} ({errno_name(ev.result)})",
                    E.THREAD_CREATE: f"{who} created {tname}",
                    E.THREAD_DETACH: f"{who} detached {tname}",
                    E.CANCEL_REQUEST: f"{who} requested cancellation of {tname}"}[E(typ)]
        if typ == E.THREAD_START:
            return f"{who} started"
        if typ == E.THREAD_EXIT:
            kinds = {0: "returned", 1: "called pthread_exit", 2: "exited (cancelled or unknown path)",
                     3: "ended with the process"}
            return f"{who} {kinds.get(ev.extra, 'exited')}"
        if typ == E.THREAD_NAME:
            return f"thread named \"{ev.text}\""
        if typ == E.LOCK_NAME:
            return f"lock {ev.obj:#x} named \"{ev.text}\""
        if typ == E.USER_MARK:
            return f"marker: {ev.text}"
        if typ == E.MUTEX_INIT:
            return f"{who} initialised {lname} ({mutex_kind_name(ev.extra)})"
        if typ == E.MUTEX_DESTROY:
            return f"{who} destroyed {lname} ({errno_name(ev.result)})"
        if typ == E.BARRIER_WAIT_BEGIN:
            return f"{who} waits at barrier {ev.obj:#x}"
        if typ == E.BARRIER_WAIT_END:
            return f"{who} passed barrier {ev.obj:#x}"
        if typ in (E.RW_REQUEST, E.RW_ACQUIRED, E.RW_FAILED):
            how = RW_MODE_WORDS[rw_mode(ev.extra)]
            if typ == E.RW_REQUEST:
                return f"{who} requests {lname} for {how}{timed}"
            if typ == E.RW_ACQUIRED:
                if ev.flags & Flag.TRY:
                    return f"{who} acquired {lname} for {how} with a try-lock"
                return f"{who} acquired {lname} for {how} after {fmt_ms(ev.aux)}"
            kind = "try-lock" if ev.flags & Flag.TRY else "lock"
            return f"{who}: {kind} on {lname} for {how} failed ({errno_name(ev.result)})"
        if typ == E.RW_RELEASE:
            return f"{who} releases {lname}"
        if typ == E.RWLOCK_INIT:
            return f"{who} initialised {lname} ({rwlock_kind_name(ev.extra)})"
        if typ == E.RWLOCK_DESTROY:
            return f"{who} destroyed {lname} ({errno_name(ev.result)})"
        if typ in (E.SEM_INIT, E.SEM_DESTROY, E.SEM_WAIT_BEGIN, E.SEM_WAIT_END, E.SEM_POST, E.SEM_POST_FAILED):
            sem = self.sems.get(ev.obj)
            sname = sem.label if sem else f"sem@{ev.obj:#x}"
            if typ == E.SEM_INIT:
                return f"{who} initialised semaphore {sname} with value {ev.aux}" + (" (process-shared)" if ev.extra else "")
            if typ == E.SEM_DESTROY:
                return f"{who} destroyed semaphore {sname} ({errno_name(ev.result)})"
            if typ == E.SEM_WAIT_BEGIN:
                return f"{who} waits on semaphore {sname}{timed} (count was {ev.aux})"
            if typ == E.SEM_WAIT_END:
                if ev.flags & Flag.TRY:
                    return f"{who}: sem_trywait on {sname} " + ("succeeded" if ev.result == 0 else
                                                                 f"failed ({errno_name(ev.result)})")
                if ev.result == 0:
                    return f"{who} passed semaphore {sname} after {fmt_ms(ev.aux)}"
                return f"{who}: wait on semaphore {sname} ended with {errno_name(ev.result)}"
            if typ == E.SEM_POST:
                return f"{who} posts semaphore {sname}"
            return f"{who}: sem_post on {sname} failed ({errno_name(ev.result)})"
        if typ == E.PROCESS_START:
            return f"process started (parent pid {ev.obj})"
        if typ == E.PROCESS_EXIT:
            return f"process exiting ({ev.obj} events dropped in total)"
        return E(typ).name

    # ============================================================ outputs

    def thread_dict(self, t: ThreadInfo, in_cycle: set[int]) -> dict:
        w = t.wait
        wait = None
        if w is not None:
            sem = self.sems.get(w.sem) if w.sem is not None else None
            wait = {"kind": w.kind, "since_seq": w.seq, "since_ts": w.ts, "timed": w.timed, "lock": w.lock,
                    "cond": f"{w.cond:#x}" if w.cond else None,
                    "target": (self.by_handle[w.target_handle].index
                               if w.kind == "join" and w.target_handle in self.by_handle else None),
                    "signaled": w.signaled, "mode": w.mode,
                    "sem": f"{w.sem:#x}" if w.sem is not None else None,
                    "sem_label": sem.label if sem else None}
        return {
            "index": t.index, "tid": t.tid, "handle": t.handle, "name": t.name, "label": t.label,
            "state": t.state if t.alive else EXITED,
            "parent": t.parent, "wait": wait,
            "held": [{"lock": h.lock, "since_seq": h.seq, "since_ts": h.ts, "mode": h.mode} for h in t.held],
            "started_seq": t.started_seq, "started_ts": t.started_ts, "ended_seq": t.ended_seq,
            "ended_ts": t.ended_ts, "exit_kind": t.exit_kind, "late": t.late, "detached": t.detached,
            "terminated_by_process_exit": t.terminated_by_process_exit,
            "last_seq": t.last_seq, "last_ts": t.last_ts,
            "last_type": E(t.last_type).name if t.last_type else None, "events": t.events,
            "acquisitions": t.acquisitions, "wait_ns_total": t.wait_ns_total,
            "in_cycle": t.index in in_cycle,
        }

    def lock_dict(self, lock: LockInfo) -> dict:
        return {
            "id": lock.id, "label": lock.label, "annotation": lock.annotation, "symbol": lock.symbol,
            "stable_key": lock.stable_key, "addr": f"{lock.addr:#x}",
            "kind": rwlock_kind_name(lock.kind) if lock.rw else mutex_kind_name(lock.kind),
            "primitive": "rwlock" if lock.rw else "mutex",
            "owner": lock.owner, "count": lock.count, "owner_since_seq": lock.owner_since_seq,
            "owner_since_ts": lock.owner_since_ts,
            "readers": [{"thread": i, "count": rh.count, "since_seq": rh.seq, "since_ts": rh.ts}
                        for i, rh in sorted(lock.readers.items())],
            "waiters": sorted(lock.waiters),
            "destroyed": lock.destroyed, "first_seq": lock.first_seq, "acquisitions": lock.acquisitions,
            "contended": lock.contended, "try_failures": lock.try_failures, "timeouts": lock.timeouts,
            "wait_ns_total": lock.wait_ns_total, "wait_ns_max": lock.wait_ns_max,
            "hold_ns_total": lock.hold_ns_total, "hold_ns_max": lock.hold_ns_max, "holds": lock.holds,
        }

    def sem_dict(self, s: SemInfo) -> dict:
        return {
            "addr": f"{s.addr:#x}", "label": s.label, "annotation": s.annotation, "symbol": s.symbol,
            "value": s.value, "init_value": s.init_value, "pshared": s.pshared, "posts": s.posts, "waits": s.waits,
            "posts_without_waiters": s.posts_without_waiters, "try_failures": s.try_failures,
            "timeouts": s.timeouts, "interrupted": s.interrupted, "waiters": sorted(s.waiters),
            "destroyed": s.destroyed, "first_seq": s.first_seq, "wait_ns_total": s.wait_ns_total,
            "wait_ns_max": s.wait_ns_max,
        }

    def snapshot(self) -> dict:
        in_cycle = self.active_cycle_threads()
        wfg_edges = self.wait_for_edges()
        if self._rw_waiters:
            cycles = cycles_multi(sorted(self.threads), self._successors)
        else:
            cycles = all_cycles(sorted(self.threads), self._successor)
        lo_cycles = [list(canonical_cycle([n["id"] for n in f.cycle["nodes"]]))
                     for f in self.findings.values() if f.kind == "lock_order"]
        live = sum(1 for t in self.threads.values() if t.alive)
        blocked = sum(1 for t in self.threads.values() if t.alive and t.state in (BLOCKED_LOCK, BLOCKED_JOIN))
        waiting = sum(1 for t in self.threads.values() if t.alive and t.state in (WAITING_COND, WAITING_SEM, BARRIER))
        return {
            "seq": self.seq,
            "first_ts": self.first_ts or 0,
            "now_ts": self.now_ts,
            "ended": self.ended,
            "threads": [self.thread_dict(t, in_cycle) for t in sorted(self.threads.values(), key=lambda x: x.index)],
            "locks": [self.lock_dict(l) for l in sorted(self.locks.values(), key=lambda x: x.id)],
            "semaphores": [self.sem_dict(s) for s in sorted(self.sems.values(), key=lambda x: x.first_seq)],
            "wait_for": {"edges": [e.as_dict() for e in wfg_edges], "cycles": cycles},
            "lock_order": {"edges": [e.as_dict() for e in self.lock_order.edges.values()], "cycles": lo_cycles},
            "findings": [f.as_dict() for f in self.findings.values()],
            "coverage": self.coverage.as_dict(),
            "counts": {E(k).name: v for k, v in sorted(self.counts.items())},
            "marks": self.marks[-200:],
            "process": dict(self.process),
            "stats": {
                "threads_total": len(self.threads), "threads_live": live, "threads_blocked": blocked,
                "threads_waiting": waiting, "locks_total": len(self.locks),
                "locks_held": sum(1 for l in self.locks.values() if l.held),
                "rwlocks_total": sum(1 for l in self.locks.values() if l.rw),
                "semaphores_total": len(self.sems),
                "events": sum(self.counts.values()),
                "lock_acquisitions": sum(l.acquisitions for l in self.locks.values()),
                "contended_acquisitions": sum(l.contended for l in self.locks.values()),
                "total_wait_ns": sum(l.wait_ns_total for l in self.locks.values()),
            },
            "verdict": self.verdict(),
        }

    def verdict(self) -> dict:
        counted = [f for f in self.findings.values()
                   if f.status != "filtered" and SEVERITY_RANK.get(f.severity, 0) >= 2
                   and not (f.kind == "deadlock" and f.status == "suspected")]
        classes = {f.classification for f in counted}
        suspected = any(f.kind == "deadlock" and f.status == "suspected" for f in self.findings.values())
        verdict = None
        if not self.coverage.complete:
            # only a deadlock confirmed while the trace was still complete survives lost data
            verdict = CONFIRMED_DEADLOCK if CONFIRMED_DEADLOCK in classes else INCONCLUSIVE
        for c in VERDICT_ORDER:
            if verdict is not None:
                break
            if c in classes:
                verdict = c
        if verdict is None:
            if suspected:
                verdict = INCONCLUSIVE
            elif not self.coverage.complete:
                verdict = INCONCLUSIVE
            else:
                verdict = NO_ISSUE
        notes = []
        if verdict == NO_ISSUE:
            notes.append("No issue was detected in this observed execution. This does not prove the program "
                         "is free of concurrency bugs: other interleavings, inputs or primitives that "
                         "ThreadLens does not intercept may still fail.")
        if not self.coverage.complete:
            notes.append(f"Event coverage is incomplete ({self.coverage.lost_events} lost, "
                         f"{self.coverage.missing_events} missing, {self.coverage.anomalies} anomalies). "
                         "Findings other than confirmed deadlocks are tentative; re-run with a larger ring "
                         "buffer or back-pressure budget.")
        return {"classification": verdict, "label": CLASSIFICATION_LABELS[verdict],
                "findings": len(counted), "notes": notes}
