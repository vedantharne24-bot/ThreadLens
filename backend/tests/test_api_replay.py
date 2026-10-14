"""API, persistence, export/import round trip and replay determinism.

These tests use the NDJSON import path with synthetic traces, so they do not
need the collector to be built.
"""

import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="backend targets Linux")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.wire import EventType as E  # noqa: E402
from app.services.eventview import TYPE_NAMES  # noqa: E402
from conftest import Trace, run  # noqa: E402

A, B = 0x1000, 0x2000


def to_ndjson(trace: Trace) -> bytes:
    lines = []
    for e in trace.items:
        flags = [n for n, bit in (("timed", 1), ("try", 2), ("oob", 4), ("inferred", 8)) if e.flags & bit]
        d = {"seq": e.seq, "ts": e.ts, "tid": e.tid, "type": TYPE_NAMES[e.type], "flags": flags, "obj": e.obj,
             "aux": e.aux, "caller": e.caller, "result": e.result, "extra": e.extra}
        if e.text is not None:
            d["text"] = e.text
        lines.append(json.dumps(d))
    return ("\n".join(lines) + "\n").encode()


def deadlock_trace(heartbeat=True) -> Trace:
    t = Trace()
    t.start(100, 0)
    t.start(101, 1, "teller-1")
    t.start(102, 2, "teller-2")
    t.ev(100, E.LOCK_NAME, obj=A, text="account[A]")
    t.lock(101, A)
    t.lock(102, B)
    t.request(101, B)
    t.request(102, A)
    if heartbeat:
        t.heartbeat()
    return t


@pytest.fixture
def client(tmp_path):
    s = Settings(data_dir=tmp_path / "data", socket_dir=tmp_path / "sock")
    with TestClient(create_app(s)) as c:
        yield c


def import_trace(client, trace: Trace) -> str:
    r = client.post("/api/sessions/import", content=to_ndjson(trace), params={"label": "synthetic"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and "pthread_rwlock" in h["unsupported_primitives"]


def test_import_analyze_and_query(client):
    sid = import_trace(client, deadlock_trace())
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["verdict"] == "confirmed_deadlock" and s["event_count"] == 12
    findings = client.get(f"/api/sessions/{sid}/findings").json()
    assert findings[0]["status"] == "confirmed"
    ev = client.get(f"/api/sessions/{sid}/events", params={"types": "LOCK_REQUEST"}).json()
    assert ev["total"] == 4 and all(e["type"] == "LOCK_REQUEST" for e in ev["events"])
    ev = client.get(f"/api/sessions/{sid}/events", params={"threads": "1", "limit": 50}).json()
    assert ev["events"] and all(e["thread"] == 1 for e in ev["events"])
    ev = client.get(f"/api/sessions/{sid}/events", params={"q": "account[A]"}).json()
    assert ev["events"] and all("account[A]" in e["description"] for e in ev["events"])
    page = client.get(f"/api/sessions/{sid}/events", params={"limit": 5}).json()
    nxt = client.get(f"/api/sessions/{sid}/events", params={"limit": 5, "after_seq": page["next_cursor"]}).json()
    assert nxt["events"][0]["seq"] == page["events"][-1]["seq"] + 1


def test_bad_inputs_rejected(client):
    # path traversal must never serve files outside the API / built frontend
    for path in ("/api/sessions/../../etc/passwd", "/../../../etc/passwd", "/assets/../../../../etc/passwd",
                 "/%2e%2e/%2e%2e/etc/passwd"):
        r = client.get(path)
        assert "root:" not in r.text, path
    assert client.get("/api/sessions/not-a-session").status_code == 400
    bad = b'{"seq": 0, "ts": 0, "tid": 1, "type": "DROP_TABLE"}\n'
    assert client.post("/api/sessions/import", content=bad).status_code == 422
    assert client.post("/api/sessions", json={"executable": "/bin/ls"}).status_code == 400
    assert client.post("/api/sessions", json={"scenario_id": "nope"}).status_code == 404
    assert client.post("/api/sessions", json={"scenario_id": "healthy", "args": ["x" * 1000]}).status_code in (400, 422)
    assert client.post("/api/sessions", json={"scenario_id": "healthy", "rm": "-rf"}).status_code == 422


def test_export_raw_reimport_roundtrip(client):
    sid = import_trace(client, deadlock_trace())
    raw = client.get(f"/api/sessions/{sid}/export", params={"format": "raw"}).content
    sid2 = client.post("/api/sessions/import", content=raw).json()["id"]
    f1 = client.get(f"/api/sessions/{sid}/findings").json()
    f2 = client.get(f"/api/sessions/{sid2}/findings").json()
    assert [(f["classification"], f["status"], f["threads"]) for f in f1] == \
           [(f["classification"], f["status"], f["threads"]) for f in f2]
    for fmt in ("csv", "json", "ndjson", "report"):
        r = client.get(f"/api/sessions/{sid}/export", params={"format": fmt})
        assert r.status_code == 200 and len(r.content) > 100


def test_replay_matches_incremental_analysis(client):
    trace = deadlock_trace()
    sid = import_trace(client, trace)
    # reference: run the analyzer incrementally and snapshot after every in-band event
    from app.detection.engine import Analyzer
    an = Analyzer()
    ref = {}
    for e in trace.items:
        if e.oob:
            an.apply_meta(e)
        else:
            an.apply(e)
        ref[an.seq] = an.snapshot()
    for seq, expect in ref.items():
        got = client.get(f"/api/sessions/{sid}/replay", params={"seq": seq}).json()
        assert got["wait_for"] == expect["wait_for"], seq
        assert [(t["index"], t["state"]) for t in got["threads"]] == [(t["index"], t["state"]) for t in expect["threads"]]
        assert [(l["id"], l["owner"]) for l in got["locks"]] == [(l["id"], l["owner"]) for l in expect["locks"]]
        if seq < trace.seq - 1:
            assert [(f["id"], f["status"]) for f in got["findings"]] == [(f["id"], f["status"]) for f in expect["findings"]]


def test_compare_and_cross_run_lock_order(client):
    t1 = Trace()
    t1.start(100, 0)
    t1.ev(100, E.LOCK_NAME, obj=A, text="lock_a")
    t1.ev(100, E.LOCK_NAME, obj=B, text="lock_b")
    t1.lock(100, A); t1.lock(100, B); t1.unlock(100, B); t1.unlock(100, A)
    t2 = Trace()
    t2.start(200, 0)
    # different addresses in the second run (ASLR); names are stable
    t2.ev(200, E.LOCK_NAME, obj=A + 0x50, text="lock_a")
    t2.ev(200, E.LOCK_NAME, obj=B + 0x50, text="lock_b")
    t2.lock(200, B + 0x50); t2.lock(200, A + 0x50); t2.unlock(200, A + 0x50); t2.unlock(200, B + 0x50)
    s1, s2 = import_trace(client, t1), import_trace(client, t2)
    for s in (s1, s2):
        assert client.get(f"/api/sessions/{s}").json()["verdict"] == "no_issue"
    merged = client.get("/api/analysis/lock-order", params={"sessions": f"{s1},{s2}"}).json()
    assert len(merged["cycles"]) == 1 and merged["cycles"][0]["cross_session"]
    assert sorted(merged["cycles"][0]["nodes"]) == ["lock_a", "lock_b"]
    dl = import_trace(client, deadlock_trace())
    cmp = client.get("/api/compare", params={"a": dl, "b": s1}).json()
    row = {r["key"]: r for r in cmp["rows"]}
    assert row["confirmed_deadlocks"]["change"] == "improved"
    assert "does not prove" in cmp["disclaimer"]


def test_timeline_and_settings(client):
    sid = import_trace(client, deadlock_trace())
    tl = client.get(f"/api/sessions/{sid}/timeline").json()
    states = {lane["label"]: lane["segments"][-1][2] for lane in tl["lanes"] if lane["segments"]}
    assert states["teller-1"] == "blocked_lock" and states["teller-2"] == "blocked_lock"
    cur = client.get("/api/settings").json()
    cur["analysis"]["stall_ms"] = 1500
    upd = client.put("/api/settings", json=cur).json()
    assert upd["analysis"]["stall_ms"] == 1500
    assert client.put("/api/settings", json={"analysis": {"stall_ms": -1}}).status_code == 422


def test_delete_session(client):
    sid = import_trace(client, deadlock_trace())
    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_websocket_final_for_finished_session(client):
    sid = import_trace(client, deadlock_trace())
    with client.websocket_connect(f"/ws/sessions/{sid}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "final" and msg["snapshot"]["verdict"]["classification"] == "confirmed_deadlock"


def test_reference_analysis_unaffected_by_import_path():
    an = run(deadlock_trace())
    assert an.verdict()["classification"] == "confirmed_deadlock"
