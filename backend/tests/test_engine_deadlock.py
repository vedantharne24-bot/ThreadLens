"""Wait-for graph: deadlock detection, confirmation and classification."""

from app.detection.engine import AnalysisConfig
from app.diagnostics.findings import CONFIRMED_DEADLOCK, INCONCLUSIVE, POTENTIAL_DEADLOCK
from app.models.wire import EventType as E, Flag
from conftest import ERRORCHECK, RECURSIVE, Trace, findings, run

A, B, C = 0x1000, 0x2000, 0x3000


def two_thread_deadlock() -> Trace:
    t = Trace()
    t.start(100, 0)
    t.start(101, 1, "teller-1")
    t.start(102, 2, "teller-2")
    t.lock(101, A)
    t.lock(102, B)
    t.request(101, B)   # 101 -> 102
    t.request(102, A)   # 102 -> 101 closes the cycle
    return t


def test_cycle_is_suspected_until_heartbeat_then_confirmed():
    t = two_thread_deadlock()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.status == "suspected"
    assert f.severity == "critical"
    assert f.classification == POTENTIAL_DEADLOCK  # not yet confirmed
    assert sorted(f.threads) == [1, 2]
    assert an.verdict()["classification"] == INCONCLUSIVE

    t.heartbeat()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.status == "confirmed"
    assert f.classification == CONFIRMED_DEADLOCK
    assert an.verdict()["classification"] == CONFIRMED_DEADLOCK
    assert f.metrics["cycle_length"] == 2


def test_heartbeat_before_cycle_does_not_confirm():
    t = two_thread_deadlock()
    # a heartbeat whose seq does not cover the cycle-forming event
    hb = t.heartbeat()
    hb.seq = 5
    an = run(t)
    assert findings(an, kind="deadlock")[0].status == "suspected"


def test_end_of_stream_confirms():
    an = run(two_thread_deadlock(), finalize=True)
    assert findings(an, kind="deadlock")[0].status == "confirmed"


def test_evidence_contains_requests_and_acquisitions_in_order():
    t = two_thread_deadlock()
    t.heartbeat()
    f = findings(run(t), kind="deadlock")[0]
    types = [e.type for e in f.evidence]
    assert types.count("LOCK_ACQUIRED") == 2 and types.count("LOCK_REQUEST") == 2
    seqs = [e.seq for e in f.evidence]
    assert seqs == sorted(seqs)
    assert f.evidence[-1].role == "trigger"
    assert any("Coffman" in p for p in f.explanation)


def test_wait_for_graph_edges():
    an = run(two_thread_deadlock())
    edges = {(e.frm, e.to, e.kind) for e in an.wait_for_edges()}
    assert edges == {(1, 2, "lock"), (2, 1, "lock")}
    snap = an.snapshot()
    assert snap["wait_for"]["cycles"] == [[1, 2]]
    assert all(t["in_cycle"] for t in snap["threads"] if t["index"] in (1, 2))


def test_contention_is_not_deadlock():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.lock(101, A)
    t.request(102, A)          # waits, but 101 is running
    t.heartbeat()
    t.unlock(101, A)
    t.acquired(102, A, wait=5_000_000)
    t.unlock(102, A)
    an = run(t, finalize=True)
    assert not findings(an, kind="deadlock")


def test_three_thread_cycle():
    t = Trace()
    for i, tid in enumerate((100, 101, 102, 103)):
        t.start(tid, i)
    t.lock(101, A)
    t.lock(102, B)
    t.lock(103, C)
    t.request(101, B)
    t.request(102, C)
    t.request(103, A)
    t.heartbeat()
    (f,) = findings(run(t), kind="deadlock")
    assert f.status == "confirmed" and sorted(f.threads) == [1, 2, 3]


def test_self_deadlock_normal_mutex():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A)
    t.request(100, A)
    t.heartbeat()
    (f,) = findings(run(t), kind="deadlock")
    assert f.status == "confirmed" and f.threads == [0] and "Self-deadlock" in f.title


def test_recursive_relock_is_not_deadlock():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A, kind=RECURSIVE)
    t.request(100, A, kind=RECURSIVE)
    t.heartbeat()
    an = run(t)
    assert not findings(an, kind="deadlock")
    t.acquired(100, A, kind=RECURSIVE)
    an = run(t)
    assert an.locks[1].count == 2


def test_errorcheck_relock_reports_misuse_not_deadlock():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A, kind=ERRORCHECK)
    t.request(100, A, kind=ERRORCHECK)
    t.ev(100, E.LOCK_FAILED, obj=A, extra=ERRORCHECK, result=35)  # EDEADLK
    t.heartbeat()
    an = run(t)
    assert not findings(an, kind="deadlock")
    assert findings(an, classification="api_misuse")


def test_join_lock_cycle():
    t = Trace()
    t.start(100, 0)
    t.lock(100, A)
    t.ev(100, E.THREAD_CREATE, obj=Trace.handle(101))
    t.ev(100, E.JOIN_BEGIN, obj=Trace.handle(101))   # target not started yet
    t.start(101, 1)                                   # resolves the pending join edge
    t.request(101, A)
    t.heartbeat()
    (f,) = findings(run(t), kind="deadlock")
    assert f.status == "confirmed"
    kinds = {e["kind"] for e in f.cycle["edges"]}
    assert kinds == {"join", "lock"}


def test_timed_cycle_is_potential_and_resolves():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.lock(101, A)
    t.lock(102, B)
    t.request(101, B, timed=True)
    t.request(102, A)
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.classification == POTENTIAL_DEADLOCK and f.status == "active"
    t.ev(101, E.LOCK_FAILED, obj=B, result=110, flags=Flag.TIMED)  # ETIMEDOUT
    t.heartbeat()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.status == "resolved"
    assert an.verdict()["classification"] == POTENTIAL_DEADLOCK


def test_lost_events_make_cycle_inconclusive():
    t = Trace()
    t.lost(3)
    for ev in two_thread_deadlock().items:
        ev.seq += 0
        t.items.append(ev)
    t.heartbeat(lost=3)
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.classification == INCONCLUSIVE
    assert an.verdict()["classification"] == INCONCLUSIVE


def test_loss_reported_before_confirmation_downgrades():
    t = two_thread_deadlock()
    t.lost(1)
    t.heartbeat(lost=1)
    (f,) = findings(run(t), kind="deadlock")
    assert f.classification == INCONCLUSIVE


def test_cond_reacquire_edge_is_inferred_potential():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    CV = 0x9000
    # 101 waits on CV (mutex A) while holding B
    t.lock(101, B)
    t.lock(101, A)
    t.ev(101, E.COND_WAIT_BEGIN, obj=CV, aux=A)
    # 102 takes A, signals, then requests B (held by 101) while still holding A
    t.lock(102, A)
    t.ev(102, E.COND_SIGNAL, obj=CV)
    t.request(102, B)
    t.heartbeat()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.classification == POTENTIAL_DEADLOCK
    assert any(e["inferred"] for e in f.cycle["edges"])


def test_heartbeat_confirmation_disabled_confirms_immediately():
    an = run(two_thread_deadlock(), AnalysisConfig(confirm_requires_heartbeat=False))
    assert findings(an, kind="deadlock")[0].status == "confirmed"
