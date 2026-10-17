"""Read-write locks (shared ownership, multi-edge wait-for graph) and semaphores (no owner)."""

import errno

from app.detection.engine import AnalysisConfig
from app.diagnostics.findings import (API_MISUSE, CONFIRMED_DEADLOCK, EXCESSIVE_CONTENTION, INCONCLUSIVE,
                                      POTENTIAL_DEADLOCK, STALL)
from app.models.wire import RW_PREFER_WRITER_NONRECURSIVE, EventType as E
from conftest import Trace, findings, run

RW1, RW2, RW3, M = 0x5000, 0x6000, 0x8000, 0x1000
S = 0x7000


def edges(an):
    return {(e.frm, e.to, e.kind, e.mode, e.inferred) for e in an.wait_for_edges()}


# ------------------------------------------------------------ shared ownership

def test_readers_share_and_writer_waits_for_every_reader():
    t = Trace()
    for tid, idx in ((100, 0), (101, 1), (102, 2), (103, 3)):
        t.start(tid, idx)
    t.rdlock(101, RW1)
    t.rdlock(102, RW1)                     # second reader: no wait, no anomaly
    t.rw_request(103, RW1, "w")
    an = run(t)
    lock = an.locks[an.lock_by_addr[RW1]]
    assert lock.rw and sorted(lock.readers) == [1, 2] and lock.owner is None
    assert an.threads[3].state == "blocked_lock" and an.threads[3].wait.kind == "rwlock"
    assert edges(an) == {(3, 1, "rwlock", "w", False), (3, 2, "rwlock", "w", False)}
    assert an.coverage.complete and not findings(an, kind="deadlock")
    # one reader leaves: the writer still waits for the other one
    t.rw_unlock(101, RW1)
    an = run(t)
    assert edges(an) == {(3, 2, "rwlock", "w", False)}
    t.rw_unlock(102, RW1)
    t.rw_acquired(103, RW1, "w")
    an = run(t)
    assert lock.id in an.locks and an.locks[lock.id].owner == 3 and not an.wait_for_edges()
    assert an.threads[3].held[0].mode == "w"


def test_reader_waits_for_writer_and_recursive_read_does_not_wait():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.wrlock(101, RW1)
    t.rw_request(102, RW1, "r")
    an = run(t)
    assert edges(an) == {(2, 1, "rwlock", "r", False)}
    t2 = Trace()
    t2.start(100, 0)
    t2.start(101, 1)
    t2.rdlock(101, RW1)
    t2.rw_request(101, RW1, "r")           # recursive read on a reader-preferring rwlock proceeds
    an = run(t2)
    assert not an.wait_for_edges()
    t2.rw_acquired(101, RW1, "r")
    an = run(t2)
    assert an.locks[an.lock_by_addr[RW1]].readers[1].count == 2 and an.coverage.complete


def two_rwlock_deadlock(extra_reader: bool = True) -> Trace:
    """lookup-1: table(r) -> stats(w); rebalancer: stats(r) -> table(w); lookup-2 also reads table."""
    t = Trace()
    t.start(100, 0)
    t.start(101, 1, "lookup-1")
    t.start(102, 2, "lookup-2")
    t.start(103, 3, "rebalancer")
    t.rdlock(101, RW1)
    if extra_reader:
        t.rdlock(102, RW1)
    t.rdlock(103, RW2)
    t.rw_request(101, RW2, "w")            # 1 -> 3
    t.rw_request(103, RW1, "w")            # 3 -> {1, 2}: out-degree 2 closes 1 <-> 3
    return t


def test_rwlock_cycle_through_multi_edge_node_is_found_and_confirmed():
    t = two_rwlock_deadlock()
    an = run(t)
    assert an._rw_waiters == {1, 3}
    (f,) = findings(an, kind="deadlock")
    assert f.status == "suspected" and sorted(f.threads) == [1, 3]
    assert {e["kind"] for e in f.cycle["edges"]} == {"rwlock"}
    assert an.snapshot()["wait_for"]["cycles"] == [[1, 3]]
    t.heartbeat()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.classification == CONFIRMED_DEADLOCK and f.metrics["cycle_length"] == 2
    types = [e.type for e in f.evidence]
    assert types.count("RW_REQUEST") == 2 and types.count("RW_ACQUIRED") == 2
    assert any("every reader" in line for line in f.explanation)
    assert any("write lock excludes" in line for line in f.explanation)
    assert any("Read locks take part" in r for r in f.remedies)


def test_rwlock_cycle_persists_after_other_reader_leaves():
    t = two_rwlock_deadlock()
    t.rw_unlock(102, RW1)                  # lookup-2 releases; the cycle 1 <-> 3 remains
    t.exit(102)
    t.heartbeat()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.status == "confirmed"


def test_writer_blocked_by_running_reader_is_not_a_deadlock():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1)
    t.rdlock(102, RW2)
    t.rw_request(102, RW1, "w")            # 2 -> 1, but 1 is running
    t.heartbeat()
    an = run(t)
    assert not findings(an, kind="deadlock")


def test_read_to_write_upgrade_is_a_self_deadlock():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1, "updater")
    t.start(102, 2, "cache-reader")
    t.rdlock(101, RW1)
    t.rdlock(102, RW1)
    t.rw_request(101, RW1, "w")            # waits for itself and for 2
    t.heartbeat()
    an = run(t)
    (f,) = findings(an, kind="deadlock")
    assert f.classification == CONFIRMED_DEADLOCK and f.threads == [1]
    assert f.metrics["cycle_length"] == 1 and "upgrade" in f.title
    assert any("including updater itself" in line for line in f.explanation)
    assert any("upgrade" in r for r in f.remedies)


def test_writer_relock_returns_edeadlk_without_an_edge():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.wrlock(101, RW1)
    t.rw_request(101, RW1, "r")
    an = run(t)
    assert not an.wait_for_edges()                       # glibc fails instead of blocking
    t.ev(101, E.RW_FAILED, obj=RW1, extra=Trace.rw_extra("r"), result=errno.EDEADLK)
    an = run(t)
    (f,) = findings(an, classification=API_MISUSE)
    assert "EDEADLK" in f.summary and an.threads[1].wait is None


def test_timed_rwlock_cycle_is_only_potential():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1)
    t.rdlock(102, RW2)
    t.rw_request(101, RW2, "w", timed=True)
    t.rw_request(102, RW1, "w")
    t.heartbeat()
    (f,) = findings(run(t), kind="deadlock")
    assert f.classification == POTENTIAL_DEADLOCK and "timed" in f.title


def test_writer_preferring_rwlock_reader_queues_behind_waiting_writer():
    kind = RW_PREFER_WRITER_NONRECURSIVE
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1, kind)
    t.rw_request(102, RW1, "w", kind)      # writer waits for reader 1
    t.rw_request(101, RW1, "r", kind)      # recursive read queues behind the writer (inferred)
    t.heartbeat()
    an = run(t)
    assert (1, 2, "rwlock", "r", True) in edges(an)
    (f,) = findings(an, kind="deadlock")
    assert f.classification == POTENTIAL_DEADLOCK and "queued behind a waiting writer" in f.title
    assert any("writer-preferring" in line for line in f.limitations)


def test_foreign_rwlock_unlock_is_misuse_and_makes_cycles_inconclusive():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1)
    t.rw_unlock(102, RW1)                  # 2 never held RW1
    an = run(t)
    (f,) = findings(an, classification=API_MISUSE)
    assert "does not hold" in f.title
    assert not an.locks[an.lock_by_addr[RW1]].owner_confirmed
    t.rdlock(102, RW2)
    t.rw_request(101, RW2, "w")
    t.rw_request(102, RW1, "w")
    t.heartbeat()
    (d,) = findings(run(t), kind="deadlock")
    assert d.classification == INCONCLUSIVE


def test_exit_holding_read_lock_abandons_writers():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1)
    t.rw_request(102, RW1, "w")
    t.exit(101)
    an = run(t)
    titles = [f.title for f in findings(an, kind="abandoned")]
    assert any("exited while holding" in x and "for reading" in x for x in titles)
    assert any("held by exited" in x for x in titles)


def test_acquire_while_write_held_is_an_anomaly():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.wrlock(101, RW1)
    t.rw_acquired(102, RW1, "r", try_=True)  # impossible with complete, ordered events
    an = run(t)
    assert not an.coverage.complete and an.coverage.anomaly_examples[0]["kind"] == "acquired_while_owned"


# ------------------------------------------------------------ lock-order graph

def test_rwlock_acquisitions_record_modes_and_try_variants_add_no_edges():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.rdlock(101, RW1)
    t.wrlock(101, RW2)
    t.rw_unlock(101, RW2, "w")
    t.rw_acquired(101, RW3, "r", try_=True)  # try variant: no edge
    an = run(t)
    (e,) = an.lock_order.edges.values()
    assert e.src_modes == {"r"} and e.dst_modes == {"w"}


def test_read_only_lock_order_cycle_is_filtered():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1); t.rdlock(101, RW2); t.rw_unlock(101, RW2); t.rw_unlock(101, RW1)
    t.rdlock(102, RW2); t.rdlock(102, RW1); t.rw_unlock(102, RW1); t.rw_unlock(102, RW2)
    (f,) = findings(run(t), kind="lock_order")
    assert f.status == "filtered" and "shared-mode" in f.filter_reason


def test_read_write_lock_order_inversion_is_potential():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.rdlock(101, RW1); t.wrlock(101, RW2); t.rw_unlock(101, RW2, "w"); t.rw_unlock(101, RW1)
    t.rdlock(102, RW2); t.wrlock(102, RW1); t.rw_unlock(102, RW1, "w"); t.rw_unlock(102, RW2)
    an = run(t, finalize=True)
    (f,) = findings(an, kind="lock_order")
    assert f.status == "active" and f.classification == POTENTIAL_DEADLOCK
    assert an.verdict()["classification"] == POTENTIAL_DEADLOCK


def test_mixed_mutex_rwlock_order_cycle():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    t.lock(101, M); t.rdlock(101, RW1); t.rw_unlock(101, RW1); t.unlock(101, M)
    t.wrlock(102, RW1); t.lock(102, M); t.unlock(102, M); t.rw_unlock(102, RW1, "w")
    (f,) = findings(run(t), kind="lock_order")
    assert f.status == "active"


def test_gate_held_for_reading_does_not_filter():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.start(102, 2)
    A, B = 0x1000, 0x2000
    t.rdlock(101, RW1); t.lock(101, A); t.lock(101, B); t.unlock(101, B); t.unlock(101, A); t.rw_unlock(101, RW1)
    t.rdlock(102, RW1); t.lock(102, B); t.lock(102, A); t.unlock(102, A); t.unlock(102, B); t.rw_unlock(102, RW1)
    (f,) = findings(run(t), kind="lock_order")
    assert f.status == "active"          # two readers of the "gate" can run concurrently
    t2 = Trace()
    t2.start(100, 0)
    t2.start(101, 1)
    t2.start(102, 2)
    t2.wrlock(101, RW1); t2.lock(101, A); t2.lock(101, B); t2.unlock(101, B); t2.unlock(101, A); t2.rw_unlock(101, RW1, "w")
    t2.wrlock(102, RW1); t2.lock(102, B); t2.lock(102, A); t2.unlock(102, A); t2.unlock(102, B); t2.rw_unlock(102, RW1, "w")
    (f,) = findings(run(t2), kind="lock_order")
    assert f.status == "filtered" and "gate lock" in f.filter_reason


# ------------------------------------------------------------ semaphores

def test_semaphore_count_and_waits_have_no_wait_for_edges():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.sem_init(100, S, 1)
    t.ev(100, E.LOCK_NAME, obj=S, text="slots")
    t.sem_wait_begin(101, S); t.sem_wait_end(101, S)          # value 1 -> 0
    t.sem_wait_begin(101, S)                                  # blocks
    an = run(t)
    sem = an.sems[S]
    assert sem.label == "slots" and sem.value == 0 and sem.waits == 1
    assert an.threads[1].state == "waiting_sem" and not an.wait_for_edges()
    t.sem_post(100, S)
    t.sem_wait_end(101, S)
    t.sem_post(100, S)                                        # nobody waits: banked
    an = run(t)
    assert an.sems[S].value == 1 and an.sems[S].posts_without_waiters == 1 and an.coverage.complete
    snap = an.snapshot()
    assert snap["semaphores"][0]["label"] == "slots" and snap["stats"]["semaphores_total"] == 1


def test_semaphore_try_and_timeout_outcomes():
    t = Trace()
    t.start(100, 0)
    t.sem_init(100, S, 0)
    t.sem_wait_end(100, S, result=errno.EAGAIN, try_=True)
    t.sem_wait_begin(100, S, timed=True)
    t.sem_wait_end(100, S, result=errno.ETIMEDOUT)
    an = run(t)
    s = an.sems[S]
    assert s.try_failures == 1 and s.timeouts == 1 and s.waits == 0 and an.coverage.complete
    assert not findings(an)


def test_wait_without_post_is_an_anomaly_unless_process_shared():
    t = Trace()
    t.start(100, 0)
    t.sem_init(100, S, 0)
    t.sem_wait_begin(100, S); t.sem_wait_end(100, S)
    assert not run(t).coverage.complete
    t2 = Trace()
    t2.start(100, 0)
    t2.sem_init(100, S, 0, pshared=True)     # another process may post it
    t2.sem_wait_begin(100, S); t2.sem_wait_end(100, S)
    an = run(t2)
    assert an.coverage.complete and an.sems[S].value is None


def test_semaphore_destroyed_with_waiters_is_misuse():
    t = Trace()
    t.start(100, 0)
    t.start(101, 1)
    t.sem_init(100, S, 0)
    t.sem_wait_begin(101, S)
    t.ev(100, E.SEM_DESTROY, obj=S)
    (f,) = findings(run(t), classification=API_MISUSE)
    assert "destroyed while threads wait" in f.title


def test_failed_post_is_undone():
    t = Trace()
    t.start(100, 0)
    t.sem_init(100, S, 0)
    t.sem_post(100, S)
    t.ev(100, E.SEM_POST_FAILED, obj=S, result=errno.EOVERFLOW)
    an = run(t)
    assert an.sems[S].posts == 0 and an.sems[S].value == 0
    assert "EOVERFLOW" in findings(an, classification=API_MISUSE)[0].title


def semaphore_stall_trace() -> Trace:
    """Producer holds buffer.mu and waits on `slots`; consumer waits for buffer.mu; main joins producer."""
    t = Trace()
    t.start(100, 0)
    t.start(101, 1, "producer")
    t.start(102, 2, "consumer")
    t.sem_init(100, S, 0)
    t.ev(100, E.LOCK_NAME, obj=S, text="slots")
    t.lock(101, M)
    t.sem_wait_begin(101, S)
    t.request(102, M)
    t.ev(100, E.JOIN_BEGIN, obj=Trace.handle(101))
    return t


def test_semaphore_stall_explains_the_ownerless_chain():
    cfg = AnalysisConfig(stall_ms=500, long_wait_ms=200)
    t = semaphore_stall_trace()
    t.advance(300_000_000)
    t.heartbeat()                          # 300 ms: long wait -> contention for now
    an = run(t, cfg)
    (c,) = findings(an, classification=EXCESSIVE_CONTENTION)
    assert c.status == "active"
    t.advance(400_000_000)
    t.heartbeat()                          # 700 ms of silence: stall
    an = run(t, cfg)
    (f,) = findings(an, classification=STALL)
    assert not findings(an, kind="deadlock") and an.verdict()["classification"] == STALL
    assert any("semaphore slots" in line and "has no owner" in line for line in f.explanation)
    assert any("initial value 0 + 0 post(s)" in line for line in f.explanation)
    assert f.metrics["semaphores"] == ["slots"]
    assert any("sem_wait" in r for r in f.remedies)
    (c,) = findings(an, classification=EXCESSIVE_CONTENTION)
    assert c.status == "filtered" and f.id in c.filter_reason
    # progress resumes: the stall resolves and the long wait counts as contention again
    t.advance(1_000_000)
    t.start(103, 3)
    t.heartbeat()
    an = run(t, cfg)
    assert findings(an, classification=STALL)[0].status == "resolved"
    assert findings(an, classification=EXCESSIVE_CONTENTION)[0].status == "active"


def test_timed_semaphore_wait_is_not_a_stall():
    cfg = AnalysisConfig(stall_ms=100)
    t = Trace()
    t.start(100, 0)
    t.sem_init(100, S, 0)
    t.sem_wait_begin(100, S, timed=True)
    t.advance(500_000_000)
    t.heartbeat()
    assert not findings(run(t, cfg), classification=STALL)


def test_rwlock_and_semaphore_descriptions():
    t = semaphore_stall_trace()
    t.rdlock(100, RW1)
    an = run(t)
    by_type = {}
    for ev in t.items:
        by_type.setdefault(E(ev.type).name, an.describe(ev))
    assert by_type["SEM_WAIT_BEGIN"].startswith("producer waits on semaphore slots")
    assert "for reading" in by_type["RW_REQUEST"]
    assert "initialised semaphore slots with value 0" in by_type["SEM_INIT"]
