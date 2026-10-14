"""Thread/lock state tracking, lock-order graph, contention, stall and misuse."""

from app.detection.engine import AnalysisConfig
from app.diagnostics.findings import API_MISUSE, EXCESSIVE_CONTENTION, NO_ISSUE, POTENTIAL_DEADLOCK, STALL
from app.models.wire import EventType as E, ExitKind, Flag
from conftest import ERRORCHECK, NORMAL, Trace, findings, run

A, B, C, G = 0x1000, 0x2000, 0x3000, 0x4000


def test_thread_lifecycle_states():
    t = Trace()
    t.start(100, 0)
    t.ev(100, E.THREAD_CREATE, obj=Trace.handle(101))
    t.start(101, 1, "worker")
    t.ev(100, E.JOIN_BEGIN, obj=Trace.handle(101))
    an = run(t)
    assert an.threads[0].state == "blocked_join"
    assert an.threads[1].parent == 0 and an.threads[1].name == "worker"
    t.exit(101, ExitKind.PTHREAD_EXIT)
    t.ev(100, E.JOIN_END, obj=Trace.handle(101))
    an = run(t)
    assert not an.threads[1].alive and an.threads[1].exit_kind == ExitKind.PTHREAD_EXIT
    assert an.threads[0].state == "running"


def test_ownership_and_recursion():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A, kind=1)
    t.lock(100, A, kind=1)
    an = run(t)
    lock = an.locks[1]
    assert lock.owner == 0 and lock.count == 2
    t.unlock(100, A, kind=1)
    an = run(t)
    assert an.locks[1].owner == 0 and an.locks[1].count == 1
    t.unlock(100, A, kind=1)
    an = run(t)
    assert an.locks[1].owner is None


def test_cond_wait_releases_and_reacquires_mutex():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.lock(100, A)
    t.ev(100, E.COND_WAIT_BEGIN, obj=0x9000, aux=A)
    an = run(t)
    assert an.locks[1].owner is None and an.threads[0].state == "waiting_cond"
    t.lock(101, A)             # another thread can take the mutex meanwhile
    t.ev(101, E.COND_SIGNAL, obj=0x9000)
    t.unlock(101, A)
    t.ev(100, E.COND_WAIT_END, obj=0x9000, aux=A)
    an = run(t)
    assert an.locks[1].owner == 0 and an.threads[0].state == "running"
    assert an.coverage.anomalies == 0


def test_lock_order_inversion_is_potential():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.lock(101, A); t.lock(101, B); t.unlock(101, B); t.unlock(101, A)
    t.lock(102, B); t.lock(102, A); t.unlock(102, A); t.unlock(102, B)
    an = run(t, finalize=True)
    (f,) = findings(an, kind="lock_order")
    assert f.classification == POTENTIAL_DEADLOCK and f.status == "active" and f.severity == "warning"
    assert an.verdict()["classification"] == POTENTIAL_DEADLOCK
    assert not findings(an, kind="deadlock")


def test_trylock_creates_no_lock_order_edge():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.lock(100, A); t.lock(100, B); t.unlock(100, B); t.unlock(100, A)
    t.lock(101, B); t.trylock(101, A); t.unlock(101, A); t.unlock(101, B)
    an = run(t, finalize=True)
    assert (2, 1) not in an.lock_order.edges
    assert not findings(an, kind="lock_order")
    assert an.verdict()["classification"] == NO_ISSUE


def test_single_thread_cycle_is_filtered():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A); t.lock(100, B); t.unlock(100, B); t.unlock(100, A)
    t.lock(100, B); t.lock(100, A); t.unlock(100, A); t.unlock(100, B)
    an = run(t, finalize=True)
    (f,) = findings(an, kind="lock_order")
    assert f.status == "filtered" and "single-thread" in f.filter_reason
    assert an.verdict()["classification"] == NO_ISSUE


def test_gate_lock_filters_cycle():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    for tid, (x, y) in ((101, (A, B)), (102, (B, A))):
        t.lock(tid, G)
        t.lock(tid, x); t.lock(tid, y); t.unlock(tid, y); t.unlock(tid, x)
        t.unlock(tid, G)
    an = run(t, finalize=True)
    lo = [f for f in findings(an, kind="lock_order") if set(f.locks) == {2, 3}]
    assert lo and lo[0].status == "filtered" and "gate lock" in lo[0].filter_reason


def test_three_lock_order_cycle():
    t = Trace()
    for i in range(4):
        t.start(100 + i, i)
    for tid, (x, y) in ((101, (A, B)), (102, (B, C)), (103, (C, A))):
        t.lock(tid, x); t.lock(tid, y); t.unlock(tid, y); t.unlock(tid, x)
    an = run(t, finalize=True)
    (f,) = findings(an, kind="lock_order")
    assert len(f.locks) == 3


def test_contention_classification():
    cfg = AnalysisConfig(contention_min_acquisitions=10, contention_ratio=0.5, contention_total_wait_ms=10)
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    for _ in range(10):
        t.lock(101, A)
        t.request(102, A)
        t.unlock(101, A)
        t.acquired(102, A, wait=2_000_000)
        t.unlock(102, A)
    an = run(t, cfg, finalize=True)
    (f,) = findings(an, classification=EXCESSIVE_CONTENTION)
    assert f.metrics["contended"] == 10 and f.metrics["acquisitions"] == 20
    assert an.verdict()["classification"] == EXCESSIVE_CONTENTION
    assert not findings(an, kind="deadlock")


def test_stall_detection_lost_wakeup():
    cfg = AnalysisConfig(stall_ms=500)
    t = Trace()
    CV = 0x9000
    t.start(100, 0)
    t.start(101, 1)
    t.lock(101, A); t.ev(101, E.COND_SIGNAL, obj=CV); t.unlock(101, A)   # lost signal
    t.exit(101)
    t.start(102, 2)
    t.lock(102, A); t.ev(102, E.COND_WAIT_BEGIN, obj=CV, aux=A)
    t.ev(100, E.JOIN_BEGIN, obj=Trace.handle(102))
    t.heartbeat()
    an = run(t, cfg)
    assert not findings(an, classification=STALL)       # not silent long enough
    t.advance(800_000_000)
    t.heartbeat()
    an = run(t, cfg)
    (f,) = findings(an, classification=STALL)
    assert f.severity == "critical"
    assert any("lost wake-up" in line for line in f.explanation)
    assert not findings(an, kind="deadlock")


def test_timed_waits_are_not_stalls():
    cfg = AnalysisConfig(stall_ms=100)
    t = Trace()
    t.start(100, 0)
    t.lock(100, A)
    t.ev(100, E.COND_WAIT_BEGIN, obj=0x9000, aux=A, flags=Flag.TIMED)
    t.advance(500_000_000)
    t.heartbeat()
    assert not findings(run(t, cfg), classification=STALL)


def test_unlock_by_non_owner_errorcheck_restores_owner():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.lock(100, A, kind=ERRORCHECK)
    t.unlock(101, A, kind=ERRORCHECK)
    t.ev(101, E.UNLOCK_FAILED, obj=A, result=1)  # EPERM
    an = run(t)
    assert an.locks[1].owner == 0
    assert findings(an, classification=API_MISUSE)


def test_unlock_by_owner_failed_restores_ownership():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A, kind=ERRORCHECK)
    t.unlock(100, A, kind=ERRORCHECK)
    t.ev(100, E.UNLOCK_FAILED, obj=A, result=1)
    an = run(t)
    assert an.locks[1].owner == 0 and an.threads[0].held


def test_unlock_by_non_owner_normal_releases():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.lock(100, A, kind=NORMAL)
    t.unlock(101, A, kind=NORMAL)
    an = run(t)
    assert an.locks[1].owner is None
    assert any("non-owner" in f.title or "owned by another" in f.title for f in findings(an, classification=API_MISUSE))


def test_exit_holding_lock_and_abandoned_wait():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.lock(101, A)
    t.exit(101)
    t.request(100, A)
    an = run(t)
    kinds = {f.kind for f in findings(an, classification=API_MISUSE)}
    assert "abandoned" in kinds
    crit = [f for f in an.findings.values() if f.severity == "critical"]
    assert crit and "exited" in crit[0].title


def test_acquire_while_owned_is_an_anomaly():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.lock(100, A)
    t.lock(101, A)   # release of 100 was never observed
    an = run(t)
    assert an.coverage.anomalies == 1 and not an.coverage.complete


def test_process_exit_terminates_threads():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.ev(100, E.PROCESS_EXIT)
    an = run(t)
    assert all(not th.alive for th in an.threads.values())
    assert an.threads[1].terminated_by_process_exit
    assert an.coverage.clean_exit


def test_snapshot_is_json_serializable():
    import json
    t = Trace()
    t.start(100, 0, "main")
    t.lock(100, A)
    t.ev(100, E.LOCK_NAME, obj=A, text="account[A]")
    snap = run(t).snapshot()
    json.dumps(snap)
    assert snap["locks"][0]["label"] == "account[A]"


def test_loss_makes_lock_order_and_verdict_inconclusive():
    """Regression: under heavy loss a spurious lock-order cycle must not become the verdict."""
    from app.diagnostics.findings import INCONCLUSIVE
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.lost(500)
    t.lock(101, A); t.lock(101, B); t.unlock(101, B); t.unlock(101, A)
    t.lock(102, B); t.lock(102, A); t.unlock(102, A); t.unlock(102, B)
    an = run(t, finalize=True)
    (f,) = findings(an, kind="lock_order")
    assert f.classification == INCONCLUSIVE
    assert an.verdict()["classification"] == INCONCLUSIVE


def test_loss_after_finding_downgrades_verdict_but_keeps_finding():
    from app.diagnostics.findings import INCONCLUSIVE
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.lock(101, A); t.lock(101, B); t.unlock(101, B); t.unlock(101, A)
    t.lock(102, B); t.lock(102, A); t.unlock(102, A); t.unlock(102, B)
    t.lost(10)
    an = run(t, finalize=True)
    (f,) = findings(an, kind="lock_order")
    assert f.classification == POTENTIAL_DEADLOCK and "Coverage became incomplete" in f.limitations[0]
    assert an.verdict()["classification"] == INCONCLUSIVE
