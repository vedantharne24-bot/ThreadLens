"""State objects maintained by the analysis engine.

Identity rules (see docs/event-schema.md):
  * A thread is identified by the collector-assigned *index* (T0 = main,
    T1, T2, ... in registration order). The kernel TID and the pthread_t
    handle are attributes; both can be reused by the OS after a thread ends.
  * A lock is identified by a session-local id (L1, L2, ...). A new id is
    assigned the first time an address is seen and again after the mutex at
    that address has been destroyed (address reuse).
"""

from __future__ import annotations

from dataclasses import dataclass, field

RUNNING = "running"
BLOCKED_LOCK = "blocked_lock"
WAITING_COND = "waiting_cond"
BLOCKED_JOIN = "blocked_join"
BARRIER = "barrier"
EXITED = "exited"


@dataclass(slots=True)
class Wait:
    """What a thread is currently waiting for (at most one thing)."""

    kind: str                       # 'lock' | 'join' | 'cond' | 'barrier'
    seq: int                        # event that started the wait
    ts: int
    caller: int = 0
    timed: bool = False
    lock: int | None = None         # lock id (lock wait, or the cond's mutex)
    cond: int | None = None         # condition variable address
    barrier: int | None = None
    target_handle: int | None = None  # pthread_t for joins
    glibc_owner: int = 0            # owner TID read from the glibc mutex at request time
    tracked_owner: int | None = None  # owner thread index according to our state at request time
    signaled: str | None = None     # cond waits: None | 'signal' | 'broadcast'
    signal_seq: int | None = None


@dataclass(slots=True)
class Held:
    lock: int
    seq: int
    ts: int
    caller: int


@dataclass(slots=True)
class ThreadInfo:
    index: int
    tid: int
    handle: int = 0
    name: str | None = None
    parent: int | None = None
    start_routine: int = 0
    state: str = RUNNING
    wait: Wait | None = None
    held: list[Held] = field(default_factory=list)
    started_seq: int = -1
    started_ts: int = 0
    ended_seq: int | None = None
    ended_ts: int | None = None
    exit_kind: int | None = None
    late: bool = False
    detached: bool = False
    cancel_requested: bool = False
    terminated_by_process_exit: bool = False
    last_seq: int = -1
    last_ts: int = 0
    last_type: int = 0
    events: int = 0
    acquisitions: int = 0
    wait_ns_total: int = 0
    cond_saved: tuple[int, int] | None = None  # (lock id, recursion count) released by a cond wait

    @property
    def alive(self) -> bool:
        # based on the exit record, not on `state`: a few late events (e.g. from
        # other TLS destructors) may still arrive after THREAD_EXIT
        return self.ended_seq is None

    @property
    def label(self) -> str:
        return self.name or ("main" if self.index == 0 else f"thread-{self.index}")


@dataclass(slots=True)
class LockInfo:
    id: int
    addr: int
    kind: int = 0
    annotation: str | None = None
    symbol: str | None = None
    owner: int | None = None        # thread index
    count: int = 0                  # recursion depth
    owner_since_seq: int = -1
    owner_since_ts: int = 0
    owner_caller: int = 0
    owner_confirmed: bool = True    # ownership established by an observed event
    destroyed: bool = False
    first_seq: int = -1
    last_owner: int | None = None
    acquisitions: int = 0
    contended: int = 0
    try_failures: int = 0
    timeouts: int = 0
    wait_ns_total: int = 0
    wait_ns_max: int = 0
    hold_ns_total: int = 0
    hold_ns_max: int = 0
    holds: int = 0
    waiters: set[int] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.annotation or self.symbol or f"mutex@{self.addr:#x}"

    @property
    def stable_key(self) -> str | None:
        """Name that identifies the same lock across executions (ASLR-safe)."""
        return self.annotation or self.symbol


@dataclass(slots=True)
class CondInfo:
    addr: int
    waiters: set[int] = field(default_factory=set)
    signals: int = 0
    broadcasts: int = 0
    signals_without_waiters: int = 0
    last_signal_seq: int | None = None
    last_lost_signal_seq: int | None = None


@dataclass(slots=True)
class WaitEdge:
    """Edge of the wait-for graph: thread `frm` waits for thread `to`."""

    frm: int
    to: int
    kind: str                       # 'lock' | 'join' | 'cond_reacquire'
    lock: int | None
    since_seq: int
    since_ts: int
    timed: bool = False
    inferred: bool = False

    def as_dict(self) -> dict:
        return {
            "from": self.frm, "to": self.to, "kind": self.kind, "lock": self.lock,
            "since_seq": self.since_seq, "since_ts": self.since_ts,
            "timed": self.timed, "inferred": self.inferred,
        }


@dataclass(slots=True)
class Coverage:
    """How complete the observed trace is. Any problem here downgrades verdicts."""

    lost_events: int = 0
    seq_gaps: int = 0
    missing_events: int = 0
    duplicates: int = 0
    reordered: int = 0
    anomalies: int = 0
    anomaly_examples: list[dict] = field(default_factory=list)
    late_threads: int = 0
    heartbeats: int = 0
    last_heartbeat_ts: int = 0
    last_heartbeat_seq: int = 0
    ring_depth: int = 0
    ring_capacity: int = 0
    owner_checks: int = 0
    owner_mismatches: int = 0
    clean_exit: bool = False
    stream_ended: bool = False

    @property
    def complete(self) -> bool:
        return self.lost_events == 0 and self.seq_gaps == 0 and self.anomalies == 0

    def as_dict(self) -> dict:
        return {
            "complete": self.complete,
            "lost_events": self.lost_events,
            "seq_gaps": self.seq_gaps,
            "missing_events": self.missing_events,
            "duplicates": self.duplicates,
            "reordered": self.reordered,
            "anomalies": self.anomalies,
            "anomaly_examples": self.anomaly_examples[:10],
            "late_threads": self.late_threads,
            "heartbeats": self.heartbeats,
            "last_heartbeat_ts": self.last_heartbeat_ts,
            "ring_depth": self.ring_depth,
            "ring_capacity": self.ring_capacity,
            "owner_checks": self.owner_checks,
            "owner_mismatches": self.owner_mismatches,
            "clean_exit": self.clean_exit,
            "stream_ended": self.stream_ended,
        }
