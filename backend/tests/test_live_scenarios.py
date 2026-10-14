"""Integration tests against REAL pthread programs (requires the Linux build).

Each test launches an actual scenario binary through the API, lets the
collector stream real events over the Unix socket, and checks the verdict
against the ground truth in scenarios/catalog.json.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from conftest import BIN, PROJECT, needs_build

pytestmark = needs_build
CATALOG = {s["id"]: s for s in json.loads((PROJECT / "scenarios" / "catalog.json").read_text())["scenarios"]}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    s = Settings(data_dir=tmp / "data", socket_dir=tmp / "sock")
    with TestClient(create_app(s)) as c:
        yield c


def run_scenario(client, scenario_id: str, **extra) -> dict:
    r = client.post("/api/sessions", json={"scenario_id": scenario_id, **extra})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    deadline = time.time() + CATALOG[scenario_id]["timeout_s"] + 20
    while time.time() < deadline:
        s = client.get(f"/api/sessions/{sid}").json()
        if s["status"] in ("completed", "failed", "killed", "error"):
            return s
        time.sleep(0.1)
    raise AssertionError(f"session {sid} did not finish")


@pytest.mark.parametrize("scenario_id", [
    "bank-deadlock", "bank-ordered", "bank-deadlock-cpp", "bank-scoped-cpp", "healthy", "contention",
    "producer-consumer", "lock-order-both", "lifecycle", "misuse", "self-deadlock", "philosophers",
    "philosophers-fixed", "join-deadlock", "lost-wakeup",
    "rwlock-deadlock", "rwlock-ordered", "rwlock-upgrade", "sem-stall", "sem-ordered",
])
def test_scenario_verdict_matches_ground_truth(client, scenario_id):
    expect = CATALOG[scenario_id]["expect"]
    s = run_scenario(client, scenario_id)
    findings = client.get(f"/api/sessions/{s['id']}/findings").json()
    classes = {f["classification"] for f in findings if f["status"] != "filtered" and f["severity"] != "info"}
    assert s["verdict"] == expect["verdict"], (s["verdict"], [f["title"] for f in findings])
    for c in expect["must"]:
        assert c in classes
    for c in expect["must_not"]:
        assert c not in classes
    assert (s["status"] == "completed") == expect["completes"]
    assert s["lost_events"] == 0
    if "cycle_length" in expect:
        dl = [f for f in findings if f["classification"] == "confirmed_deadlock"]
        assert dl and dl[0]["metrics"]["cycle_length"] == expect["cycle_length"]
        assert s["end_reason"] == "deadlock_kill"
        assert s["detection_latency_ms"] is not None and s["detection_latency_ms"] < 1000


def test_deadlock_evidence_is_symbolized(client):
    s = run_scenario(client, "bank-deadlock")
    f = client.get(f"/api/sessions/{s['id']}/findings").json()[0]
    sites = [e["site"] for e in f["evidence"] if e["site"]]
    assert sites and all("bank_transfer.c" in x for x in sites)
    labels = {l["label"] for l in client.get(f"/api/sessions/{s['id']}/state").json()["locks"]}
    assert {"account[A]", "account[B]"} <= labels


def test_cross_run_lock_order_from_real_runs(client):
    a = run_scenario(client, "lock-order-ab")
    b = run_scenario(client, "lock-order-ba")
    merged = client.get("/api/analysis/lock-order", params={"sessions": f"{a['id']},{b['id']}"}).json()
    cyc = [c for c in merged["cycles"] if set(c["nodes"]) == {"lock_a", "lock_b"}]
    assert cyc and cyc[0]["cross_session"]


def test_stop_running_session(client):
    r = client.post("/api/sessions", json={"scenario_id": "lost-wakeup", "kill_on_deadlock": False,
                                           "timeout_s": 30})
    sid = r.json()["id"]
    time.sleep(0.5)
    s = client.post(f"/api/sessions/{sid}/stop").json()
    assert s["status"] == "killed" and s["end_reason"] == "user_stop"


def test_event_loss_is_reported_and_downgrades_verdict(client):
    # a deliberately tiny ring and no blocking budget forces drops under load
    s = run_scenario(client, "stress", ring_capacity=1024, block_us=0, args=["8", "20000", "16", "30", "0"])
    assert s["status"] == "completed"
    if s["lost_events"] > 0:
        assert s["verdict"] == "inconclusive"
    else:  # the reader kept up; nothing to downgrade
        assert s["verdict"] in ("no_issue", "excessive_contention")


def test_websocket_streams_live_updates(client):
    r = client.post("/api/sessions", json={"scenario_id": "contention"})
    sid = r.json()["id"]
    kinds = []
    with client.websocket_connect(f"/ws/sessions/{sid}") as ws:
        for _ in range(200):
            msg = ws.receive_json()
            kinds.append(msg["type"])
            if msg["type"] in ("end", "final"):
                break
    assert kinds[0] in ("hello", "final")
    if kinds[0] == "hello":
        assert "update" in kinds and kinds[-1] == "end"


def test_monitoring_disabled_baseline_runs_without_collector():
    import subprocess
    out = subprocess.run([str(BIN / "stress"), "4", "2000"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and '"monitored":false' in out.stdout
