"""Findings: classified, evidence-backed diagnostic results.

Classification vocabulary (deliberately conservative):

  confirmed_deadlock    a cycle in the CURRENT wait-for graph whose every edge
                        is an observed, untimed, blocking wait on a supported
                        primitive, with complete event coverage, that persisted
                        across a collector heartbeat (or until the trace ended).
                        "Confirmed under the observed supported synchronization
                        model" — not a proof about code we cannot see.
  potential_deadlock    a cycle in the HISTORICAL lock-order graph (the program
                        acquired locks in conflicting orders), or a wait-for
                        cycle that contains timed or inferred edges.
  excessive_contention  threads spend a large share of time waiting for a lock.
                        Never reported as a deadlock.
  api_misuse            pthread API errors (EDEADLK, EPERM, EBUSY, abandoned
                        locks, ...).
  stall                 every live thread is blocked and no progress is seen,
                        but there is no lock cycle (e.g. lost condition-variable
                        wake-up). Cause is inconclusive by design.
  inconclusive          the evidence would indicate a problem but event
                        coverage is incomplete (lost events, gaps, anomalies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONFIRMED_DEADLOCK = "confirmed_deadlock"
POTENTIAL_DEADLOCK = "potential_deadlock"
EXCESSIVE_CONTENTION = "excessive_contention"
API_MISUSE = "api_misuse"
STALL = "stall"
INCONCLUSIVE = "inconclusive"
NO_ISSUE = "no_issue"

CLASSIFICATION_LABELS = {
    CONFIRMED_DEADLOCK: "Confirmed deadlock (under the observed synchronization model)",
    POTENTIAL_DEADLOCK: "Potential deadlock",
    EXCESSIVE_CONTENTION: "Excessive lock waiting / contention",
    API_MISUSE: "pthread API misuse",
    STALL: "Stall — all observed threads blocked, no lock cycle",
    INCONCLUSIVE: "Inconclusive — insufficient event coverage",
    NO_ISSUE: "No issue detected in the observed execution",
}

# verdict precedence (most serious first)
VERDICT_ORDER = [CONFIRMED_DEADLOCK, STALL, INCONCLUSIVE, POTENTIAL_DEADLOCK, API_MISUSE,
                 EXCESSIVE_CONTENTION]

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}

COMMON_LIMITATIONS = [
    "ThreadLens observes only intercepted pthread operations (mutexes, condition variables, "
    "thread create/join/exit, barriers). Synchronization built from atomics, futexes, "
    "semaphores, read-write locks or spinlocks is invisible to it.",
    "Locks used internally by glibc (stdio, malloc) are not intercepted.",
]


@dataclass(slots=True)
class Evidence:
    seq: int
    ts: int
    thread: int | None
    type: str
    description: str
    role: str = "context"       # 'cause' | 'context' | 'trigger'
    lock: int | None = None
    caller: int = 0
    site: str | None = None     # filled by the symbolizer (function, file:line)

    def as_dict(self) -> dict:
        return {
            "seq": self.seq, "ts": self.ts, "thread": self.thread, "type": self.type,
            "description": self.description, "role": self.role, "lock": self.lock,
            "caller": f"{self.caller:#x}" if self.caller else None, "site": self.site,
        }


@dataclass(slots=True)
class Finding:
    id: str
    kind: str                       # deadlock | lock_order | contention | misuse | stall | abandoned
    classification: str
    severity: str                   # critical | warning | info
    title: str
    summary: str
    status: str = "active"          # suspected | confirmed | active | resolved | filtered
    threads: list[int] = field(default_factory=list)
    locks: list[int] = field(default_factory=list)
    cycle: dict | None = None       # {"type": "wait_for"|"lock_order", "nodes": [...], "edges": [...]}
    evidence: list[Evidence] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    remedies: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    detected_seq: int = -1
    detected_ts: int = 0
    confirmed_seq: int | None = None
    confirmed_ts: int | None = None
    resolved_seq: int | None = None
    updated_seq: int = -1
    occurrences: int = 1
    metrics: dict[str, Any] = field(default_factory=dict)
    filter_reason: str | None = None
    coverage_at_detection: dict | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "classification": self.classification,
            "classification_label": CLASSIFICATION_LABELS.get(self.classification, self.classification),
            "severity": self.severity, "title": self.title, "summary": self.summary,
            "status": self.status, "threads": list(self.threads), "locks": list(self.locks),
            "cycle": self.cycle, "evidence": [e.as_dict() for e in self.evidence],
            "explanation": list(self.explanation), "remedies": list(self.remedies),
            "limitations": list(self.limitations), "detected_seq": self.detected_seq,
            "detected_ts": self.detected_ts, "confirmed_seq": self.confirmed_seq,
            "confirmed_ts": self.confirmed_ts, "resolved_seq": self.resolved_seq,
            "updated_seq": self.updated_seq, "occurrences": self.occurrences,
            "metrics": dict(self.metrics), "filter_reason": self.filter_reason,
            "coverage_at_detection": self.coverage_at_detection,
        }


def fmt_ms(ns: int | float) -> str:
    ms = ns / 1e6
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    if ms >= 1:
        return f"{ms:.1f} ms"
    return f"{ns / 1e3:.0f} µs"


# --------------------------------------------------------------------------
# Remedy catalogues (suggestions to verify by testing — not guaranteed fixes)
# --------------------------------------------------------------------------

def deadlock_remedies(lock_labels: list[str], has_join: bool, self_loop: bool) -> list[str]:
    if self_loop:
        return [
            "Do not lock a mutex the thread already holds. Restructure the code so the inner "
            "function expects the lock to be held (e.g. a *_locked() helper) instead of locking again.",
            "If re-entrant locking is genuinely required, create the mutex with "
            "PTHREAD_MUTEX_RECURSIVE — and review why the design needs it.",
            "During development use PTHREAD_MUTEX_ERRORCHECK so a relock fails with EDEADLK "
            "instead of hanging.",
        ]
    out = []
    if lock_labels:
        names = ", ".join(lock_labels)
        out.append(
            f"Impose one global acquisition order for {names} (for example by id or address) and "
            "acquire them in that order on every code path. This removes the circular-wait condition.")
        out.append(
            "Acquire the locks together with a deadlock-avoiding primitive: std::scoped_lock / "
            "std::lock in C++, or pthread_mutex_trylock with release-and-retry back-off in C.")
        out.append("Reduce nested locking: copy the data needed under the first lock, release it, "
                   "then take the second lock.")
    if has_join:
        out.append("Never call pthread_join() (or wait for another thread's progress) while holding "
                   "a lock that thread may need. Release the lock before joining.")
    out.append("pthread_mutex_timedlock can turn a hang into a recoverable error, but it only "
               "mitigates the symptom; fix the ordering.")
    return out


LOCK_ORDER_REMEDIES = [
    "Choose a single global order for these locks and document it next to their declarations.",
    "Where two locks must be held together, acquire them through one helper that enforces the "
    "order (or use std::scoped_lock / std::lock in C++).",
    "Consider merging the locks (coarser locking) or splitting the critical sections so that "
    "they are never held at the same time.",
    "Re-run the scenario with different timings or more threads to check whether the inversion "
    "can actually deadlock; a successful run does not prove it cannot.",
]

CONTENTION_REMEDIES = [
    "Shorten the critical section: move computation, I/O and allocation outside the lock.",
    "Split the lock (lock striping / per-bucket locks) so independent data does not share one mutex.",
    "Batch updates per thread and publish them under the lock less often.",
    "If reads dominate, consider a reader-writer lock (pthread_rwlock_t) — note ThreadLens does "
    "not yet track rwlocks.",
    "Use atomics for simple counters instead of a mutex.",
]

STALL_REMEDIES = [
    "Always wait on a condition variable inside a loop that re-checks the predicate: "
    "while (!ready) pthread_cond_wait(&cv, &mu);",
    "Set the predicate and signal while holding the same mutex the waiter uses.",
    "Check that every waiter has a matching signal/broadcast on all code paths "
    "(including error paths and shutdown).",
    "Consider pthread_cond_timedwait with a diagnostic timeout during debugging.",
]
