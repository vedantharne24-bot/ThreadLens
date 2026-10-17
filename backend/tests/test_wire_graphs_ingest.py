"""Wire format, ordering guard and graph algorithms."""

import pytest

from app.detection.graphs import (LockOrderGraph, all_cycles, canonical_cycle, cycles_multi, find_cycle_dfs,
                                  find_cycle_from, tarjan_scc)
from app.detection.ingest import SequenceGuard
from app.detection.model import Coverage
from app.models.events import EventIn
from app.models.wire import (WIRE_SIZE, EventType as E, Ev, Flag, StreamDecoder, WireError, encode, iter_trace,
                             rw_kind, rw_mode, rwlock_kind_name)


def sample_events():
    return [
        Ev(0, 10, 7, int(E.THREAD_START), 0, 0xABC, 0, 0, 0, 0),
        Ev(1, 11, 7, int(E.THREAD_NAME), 0, 0xABC, 0, 0, 0, 0, "worker-1"),
        Ev(2, 12, 7, int(E.LOCK_REQUEST), int(Flag.TIMED), 0x1000, 5, 0x401000, 0, 2),
        Ev(3, 13, 7, int(E.LOCK_FAILED), 0, 0x1000, 77, 0x401000, 110, 2),
        Ev(4, 14, 0, int(E.HEARTBEAT), int(Flag.OOB), 0, 3, 0, 0, 1),
    ]


def test_roundtrip_and_chunked_decoding():
    evs = sample_events()
    blob = b"".join(encode(e) for e in evs)
    assert len(blob) == WIRE_SIZE * len(evs)
    dec = StreamDecoder()
    out = []
    for i in range(0, len(blob), 7):  # arbitrary chunk boundaries
        out.extend(dec.feed(blob[i:i + 7]))
    assert [e.to_row() for e in out] == [e.to_row() for e in evs]
    assert out[1].text == "worker-1"
    assert out[3].result == 110 and out[4].oob


def test_bad_magic_rejected():
    blob = bytearray(encode(sample_events()[0]))
    blob[0] = 0
    with pytest.raises(WireError):
        list(iter_trace(bytes(blob)))


def test_truncated_trace_rejected():
    with pytest.raises(WireError):
        list(iter_trace(b"\x00" * 65))


def test_unknown_type_rejected():
    blob = bytearray(encode(sample_events()[0]))
    blob[3] = 250
    with pytest.raises(WireError):
        list(iter_trace(bytes(blob)))


def test_event_in_schema_validation():
    ok = EventIn.model_validate({"seq": 1, "ts": 2, "tid": 3, "type": "LOCK_REQUEST", "flags": ["timed"]})
    assert ok.flag_bits() == Flag.TIMED
    for bad in ({"seq": -1, "ts": 0, "tid": 0, "type": "LOCK_REQUEST"},
                {"seq": 1, "ts": 0, "tid": 0, "type": "NOPE"},
                {"seq": 1, "ts": 0, "tid": 0, "type": "LOCK_REQUEST", "flags": ["bogus"]},
                {"seq": 1, "ts": 0, "tid": 0, "type": "LOCK_REQUEST", "evil": 1},
                {"seq": 1, "ts": 0, "tid": 0, "type": "THREAD_NAME", "text": "x" * 40}):
        with pytest.raises(Exception):
            EventIn.model_validate(bad)


def ev(seq):
    return Ev(seq, seq, 1, int(E.USER_MARK), 0, 0, 0, 0, 0, 0)


def test_guard_in_order_passthrough():
    cov = Coverage()
    g = SequenceGuard(cov)
    out = [e.seq for s in range(5) for e in g.push(ev(s))]
    assert out == [0, 1, 2, 3, 4] and cov.complete


def test_guard_reorders_and_drops_duplicates():
    cov = Coverage()
    g = SequenceGuard(cov)
    seqs = []
    for s in (0, 2, 1, 1, 3):
        seqs += [e.seq for e in g.push(ev(s))]
    assert seqs == [0, 1, 2, 3]
    assert cov.duplicates == 1 and cov.reordered == 1 and cov.complete


def test_guard_declares_gap():
    cov = Coverage()
    g = SequenceGuard(cov, window=4)
    seqs = []
    for s in (0, 1, 5, 6, 7, 8, 9):
        seqs += [e.seq for e in g.push(ev(s))]
    seqs += [e.seq for e in g.flush()]
    assert seqs == [0, 1, 5, 6, 7, 8, 9]
    assert cov.seq_gaps == 1 and cov.missing_events == 3 and not cov.complete


def test_functional_graph_cycles():
    succ = {1: 2, 2: 3, 3: 1, 4: 1, 5: None, 6: 6}
    assert find_cycle_from(4, succ.get) == [1, 2, 3]
    assert find_cycle_from(5, succ.get) is None
    cycles = [canonical_cycle(c) for c in all_cycles(succ, succ.get)]
    assert sorted(cycles) == [(1, 2, 3), (6,)]


def test_lock_order_graph_cycle_through_new_edge():
    g = LockOrderGraph()
    g.observe(1, 2, 0, 0, 1, 0, 0, 0, frozenset(), False)
    g.observe(2, 3, 0, 2, 3, 0, 0, 0, frozenset(), False)
    assert g.cycle_through(2, 3) is None
    g.observe(3, 1, 1, 4, 5, 0, 0, 0, frozenset(), False)
    assert canonical_cycle(g.cycle_through(3, 1)) == (1, 2, 3)


def test_tarjan_scc():
    adj = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": [], "e": ["e"]}
    comps = sorted(sorted(c) for c in tarjan_scc(adj, lambda n: adj[n]))
    assert ["a", "b", "c"] in comps and ["d"] in comps and ["e"] in comps


def test_rwlock_and_semaphore_records_roundtrip():
    evs = [
        Ev(0, 10, 7, int(E.RW_REQUEST), int(Flag.TIMED), 0x5000, 1234, 0x401000, 0, 1 | (2 << 8)),
        Ev(1, 11, 7, int(E.RW_ACQUIRED), int(Flag.TRY), 0x5000, 50, 0x401000, 0, 0),
        Ev(2, 12, 8, int(E.SEM_INIT), 0, 0x7000, 4, 0x401100, 0, 1),
        Ev(3, 13, 8, int(E.SEM_WAIT_END), int(Flag.TRY), 0x7000, 0, 0x401100, 11, 0),
        Ev(4, 14, 8, int(E.SEM_POST_FAILED), 0, 0x7000, 0, 0x401100, 75, 0),
    ]
    out = list(iter_trace(b"".join(encode(e) for e in evs)))
    assert [e.to_row() for e in out] == [e.to_row() for e in evs]
    assert rw_mode(evs[0].extra) == "w" and rw_kind(evs[0].extra) == 2 and rw_mode(evs[1].extra) == "r"
    assert "writer-preferring" in rwlock_kind_name(2) and rwlock_kind_name(0) == "rwlock"
    assert EventIn(seq=0, ts=0, tid=1, type="SEM_POST").type == "SEM_POST"


def test_dfs_cycle_search_with_several_successors():
    succ = {1: [2, 3], 2: [], 3: [4], 4: [1], 5: [5, 1]}
    assert find_cycle_dfs(1, succ.get) == [1, 3, 4]
    assert find_cycle_dfs(2, succ.get) is None
    assert find_cycle_dfs(5, succ.get) == [5]          # a self-loop listed first wins
    # a node reached twice through different branches is not mistaken for a cycle
    diamond = {1: [2, 3], 2: [4], 3: [4], 4: []}
    assert find_cycle_dfs(1, diamond.get) is None
    # on a functional graph the DFS agrees with the successor walk
    fun = {1: [2], 2: [3], 3: [1], 4: [1]}
    assert find_cycle_dfs(4, fun.get) == find_cycle_from(4, lambda n: fun[n][0])


def test_cycles_multi_lists_shortest_cycle_per_edge():
    # writer 3 waits for readers 1 and 2; 1 waits for 3; 2 runs. 4 upgrades (self-loop).
    succ = {1: [3], 2: [], 3: [1, 2], 4: [4, 1]}
    assert sorted(map(tuple, cycles_multi(succ, succ.get))) == [(1, 3), (4,)]
    # two overlapping cycles in one component
    succ2 = {1: [2, 3], 2: [1], 3: [1]}
    assert sorted(map(tuple, cycles_multi(succ2, succ2.get))) == [(1, 2), (1, 3)]
    assert cycles_multi(succ2, succ2.get, limit=1) == [[1, 2]]


def test_lock_order_edge_remembers_modes():
    g = LockOrderGraph()
    g.observe(1, 2, 0, 0, 1, 0, 0, 0, frozenset(), False, src_mode="r", dst_mode="w")
    e, new = g.observe(1, 2, 1, 2, 3, 0, 0, 0, frozenset(), False)
    assert not new and e.src_modes == {"r", "x"} and e.dst_modes == {"w", "x"}
    assert e.as_dict()["src_modes"] == ["r", "x"]
