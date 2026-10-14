"""Comparison of two sessions (typically original vs. corrected program),
and cross-run merging of lock-order graphs."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from app.detection.graphs import canonical_cycle, tarjan_scc
from app.diagnostics.findings import CLASSIFICATION_LABELS

DISCLAIMER = ("A successful rerun shows only that the tested scenario did not reproduce the original problem in "
              "this execution. It does not prove the absence of every concurrency bug.")


def _summary(row: dict) -> dict:
    snap = json.loads(row["summary"]) if row.get("summary") else {}
    metrics = json.loads(row["metrics"]) if row.get("metrics") else {}
    result = json.loads(row["result"]) if row.get("result") else {}
    stats = snap.get("stats", {})
    findings = [f for f in snap.get("findings", []) if f.get("status") != "filtered"]
    by_class: dict[str, int] = defaultdict(int)
    for f in findings:
        if f.get("severity") in ("critical", "warning"):
            by_class[f["classification"]] += 1
    acq = stats.get("lock_acquisitions") or 0
    contended = stats.get("contended_acquisitions") or 0
    max_wait = max((l.get("wait_ns_max", 0) for l in snap.get("locks", [])), default=0)
    return {
        "id": row["id"], "label": row.get("label"), "scenario_id": row.get("scenario_id"),
        "status": row.get("status"), "end_reason": row.get("end_reason"), "exit_code": row.get("exit_code"),
        "completed": row.get("status") == "completed" and row.get("exit_code") == 0,
        "verdict": row.get("verdict"), "verdict_label": CLASSIFICATION_LABELS.get(row.get("verdict") or "", ""),
        "findings": dict(by_class),
        "confirmed_deadlocks": by_class.get("confirmed_deadlock", 0),
        "potential_deadlocks": by_class.get("potential_deadlock", 0),
        "events": row.get("event_count") or 0, "lost_events": row.get("lost_events") or 0,
        "threads": stats.get("threads_total", 0), "locks": stats.get("locks_total", 0),
        "lock_acquisitions": acq, "contended_acquisitions": contended,
        "contended_ratio": round(contended / acq, 3) if acq else 0.0,
        "total_wait_ms": round((stats.get("total_wait_ns") or 0) / 1e6, 3),
        "max_wait_ms": round(max_wait / 1e6, 3),
        "wall_ms": row.get("wall_ms"), "program_elapsed_ms": result.get("elapsed_ms"),
        "process_ms": metrics.get("process_ms"),
        "wait_for_cycles": len(snap.get("wait_for", {}).get("cycles", [])),
        "lock_order_cycles": len(snap.get("lock_order", {}).get("cycles", [])),
        "snapshot": {k: snap.get(k) for k in ("threads", "locks", "wait_for", "lock_order")},
        "findings_list": [{k: f.get(k) for k in ("id", "title", "classification", "severity", "status", "cycle")}
                          for f in findings],
    }


ROWS = [
    ("completed", "Program completed normally", "higher"),
    ("verdict_label", "Verdict", None),
    ("confirmed_deadlocks", "Confirmed deadlocks", "lower"),
    ("potential_deadlocks", "Potential deadlocks", "lower"),
    ("wait_for_cycles", "Wait-for cycles at end", "lower"),
    ("lock_order_cycles", "Lock-order cycles", "lower"),
    ("events", "Events observed", None),
    ("lost_events", "Events lost", "lower"),
    ("lock_acquisitions", "Lock acquisitions", None),
    ("contended_acquisitions", "Contended acquisitions", "lower"),
    ("contended_ratio", "Contended ratio", "lower"),
    ("total_wait_ms", "Total lock waiting (ms)", "lower"),
    ("max_wait_ms", "Longest single wait (ms)", "lower"),
    ("wall_ms", "Wall-clock time (ms)", "lower"),
    ("program_elapsed_ms", "Program-reported time (ms)", "lower"),
]


# metrics that only describe a run that ran to completion (a terminated run stopped early)
RUN_LENGTH_METRICS = {"events", "lock_acquisitions", "contended_acquisitions", "contended_ratio", "total_wait_ms",
                      "max_wait_ms", "wall_ms", "program_elapsed_ms"}


def compare_sessions(a_row: dict, b_row: dict) -> dict:
    a, b = _summary(a_row), _summary(b_row)
    comparable = a["completed"] == b["completed"]
    rows = []
    for key, label, better in ROWS:
        va, vb = a.get(key), b.get(key)
        delta = None
        verdict = None
        if key in RUN_LENGTH_METRICS and not comparable:
            rows.append({"key": key, "label": label, "a": va, "b": vb, "delta": None, "change": None,
                         "note": "not comparable: only one run completed"})
            continue
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
            delta = round(vb - va, 3)
            if better and va != vb:
                verdict = "improved" if (vb < va) == (better == "lower") else "regressed"
        elif isinstance(va, bool) and isinstance(vb, bool) and va != vb and better:
            verdict = "improved" if vb else "regressed"
        rows.append({"key": key, "label": label, "a": va, "b": vb, "delta": delta, "change": verdict})

    obs = []
    if not comparable:
        obs.append("Only one of the two runs completed, so event counts, contention and timing are not compared: "
                   "the terminated run stopped early.")
    if a["confirmed_deadlocks"] and not b["confirmed_deadlocks"]:
        obs.append("The deadlock observed in A did not occur in B.")
    if not a["completed"] and b["completed"]:
        obs.append("B ran to completion; A had to be terminated.")
    if a["potential_deadlocks"] and not b["potential_deadlocks"]:
        obs.append("No lock-order inversion was observed in B.")
    if b["potential_deadlocks"]:
        obs.append("B still contains a lock-order inversion (potential deadlock).")
    if b["confirmed_deadlocks"]:
        obs.append("B still deadlocks.")
    if comparable and a["contended_ratio"] and b["contended_ratio"] < a["contended_ratio"]:
        obs.append(f"Contention dropped from {a['contended_ratio']:.0%} to {b['contended_ratio']:.0%} of acquisitions.")
    if b["lost_events"]:
        obs.append("B lost events; its verdict is less certain.")
    return {"a": a, "b": b, "rows": rows, "observations": obs, "disclaimer": DISCLAIMER}


def merge_lock_order(rows: list[dict]) -> dict:
    """Merge lock-order graphs of several sessions using stable lock names.

    Addresses differ between runs (ASLR), so locks are matched by their
    annotation or global symbol name. Locks without a stable name stay
    session-local and cannot connect different runs.
    """
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        snap = json.loads(row["summary"]) if row.get("summary") else {}
        keymap = {}
        for l in snap.get("locks", []):
            key = l.get("stable_key") or f"{row['id']}:L{l['id']}"
            keymap[l["id"]] = key
            nodes.setdefault(key, {"key": key, "label": l.get("label"), "stable": bool(l.get("stable_key")),
                                   "primitive": l.get("primitive", "mutex"), "sessions": []})
            if "writer-preferring" in (l.get("kind") or ""):
                nodes[key]["writer_preferring"] = True
            if row["id"] not in nodes[key]["sessions"]:
                nodes[key]["sessions"].append(row["id"])
        for e in snap.get("lock_order", {}).get("edges", []):
            s, d = keymap.get(e["from"]), keymap.get(e["to"])
            if s is None or d is None:
                continue
            m = edges.setdefault((s, d), {"from": s, "to": d, "count": 0, "sessions": [], "src_modes": [],
                                          "dst_modes": []})
            m["count"] += e.get("count", 1)
            for k in ("src_modes", "dst_modes"):
                m[k] = sorted(set(m[k]) | set(e.get(k) or ["x"]))
            if row["id"] not in m["sessions"]:
                m["sessions"].append(row["id"])
    adj: dict[str, set[str]] = defaultdict(set)
    for (s, d) in edges:
        adj[s].add(d)
    cycles = []
    for comp in tarjan_scc(list(nodes), lambda n: adj.get(n, ())):
        if len(comp) < 2:
            continue
        comp_set = set(comp)
        # shortest cycle through the component's first node (BFS restricted to the component)
        start = sorted(comp)[0]
        prev = {start: None}
        queue = [start]
        found = None
        while queue and found is None:
            nxt_q = []
            for n in queue:
                for m in adj.get(n, ()):
                    if m == start:
                        found = n
                        break
                    if m in comp_set and m not in prev:
                        prev[m] = n
                        nxt_q.append(m)
                if found:
                    break
            queue = nxt_q
        path = []
        n = found
        while n is not None:
            path.append(n)
            n = prev[n]
        path.reverse()
        cyc = list(canonical_cycle(path))
        cyc_edges = [edges[(cyc[i], cyc[(i + 1) % len(cyc)])] for i in range(len(cyc))]
        sessions = sorted({s for e in cyc_edges for s in e["sessions"]})
        single = [s for s in sessions if all(s in e["sessions"] for e in cyc_edges)]
        filter_reason = None
        for i, e in enumerate(cyc_edges):
            # same shared-mode filter as the live analysis (Analyzer._shared_mode_link)
            nxt, node = cyc_edges[(i + 1) % len(cyc_edges)], nodes.get(e["to"], {})
            if node.get("primitive") == "rwlock" and not node.get("writer_preferring") \
                    and e["dst_modes"] == ["r"] and nxt["src_modes"] == ["r"]:
                filter_reason = (f"shared-mode cycle: {node.get('label') or e['to']} is only ever held and requested "
                                 "for reading along this cycle; read locks do not exclude each other")
                break
        cycles.append({
            "nodes": cyc, "edges": cyc_edges, "sessions": sessions, "component": sorted(comp),
            "cross_session": not single, "filter_reason": filter_reason,
            "classification": "potential_deadlock",
            "explanation": (
                "The orders in this cycle were observed in different executions; no single run contained "
                "the whole cycle. Merging the runs predicts a potential deadlock if these code paths ever "
                "execute concurrently." if not single else
                "This cycle already appears within a single execution."),
        })
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "cycles": cycles,
            "sessions": [r["id"] for r in rows]}
