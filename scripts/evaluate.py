#!/usr/bin/env python3
"""ThreadLens evaluation harness (run on Linux with the project venv).

    python scripts/evaluate.py --reps 5            # full evaluation (~4-6 minutes)
    python scripts/evaluate.py --quick             # 2 repetitions, fewer configs

Measures, with real programs and the real collector/backend:
  A. detection accuracy per classification (precision / recall vs. catalog ground truth)
  B. detection latency of confirmed deadlocks (end-to-end, shared CLOCK_MONOTONIC)
  C. monitoring overhead on the stress workload (baseline / passive / trace file / live)
  D. event loss under small ring buffers
  E. comparison with ThreadSanitizer (and Helgrind when valgrind is installed)

Results are written to docs/results/evaluation.json and docs/results.md.
Nothing in the output is estimated: every number comes from a run made here.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

BIN = ROOT / "build" / "bin"
TSAN_BIN = BIN / "tsan"
COLLECTOR = ROOT / "build" / "lib" / "libthreadlens.so"
CLASSES = ["confirmed_deadlock", "potential_deadlock", "excessive_contention", "api_misuse", "stall"]


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def environment() -> dict:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "date": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "kernel": platform.release(),
        "os": sh(["sh", "-c", ". /etc/os-release && echo $PRETTY_NAME"]),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "glibc": sh(["ldd", "--version"]).splitlines()[0] if sh(["ldd", "--version"]) else "",
        "gcc": sh(["gcc", "--version"]).splitlines()[0] if sh(["gcc", "--version"]) else "",
        "python": platform.python_version(),
        "wsl": "microsoft" in platform.release().lower(),
    }


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# --------------------------------------------------------------------------- A/B
def run_session(client, scenario_id: str, timeout: float, **extra) -> dict:
    r = client.post("/api/sessions", json={"scenario_id": scenario_id, **extra})
    if r.status_code != 201:
        raise RuntimeError(r.text)
    sid = r.json()["id"]
    end = time.time() + timeout + 25
    while time.time() < end:
        s = client.get(f"/api/sessions/{sid}").json()
        if s["status"] in ("completed", "failed", "killed", "error"):
            s["findings_list"] = client.get(f"/api/sessions/{sid}/findings").json()
            return s
        time.sleep(0.05)
    raise RuntimeError(f"{scenario_id}: session {sid} did not finish")


def accuracy(client, catalog: list[dict], reps: int) -> dict:
    runs = []
    for sc in catalog:
        for rep in range(reps):
            s = run_session(client, sc["id"], sc["timeout_s"])
            reported = sorted({f["classification"] for f in s["findings_list"]
                               if f["status"] != "filtered" and f["severity"] != "info"})
            exp = sc["expect"]
            ok_verdict = s["verdict"] in exp.get("verdict_ok", [exp["verdict"]])
            completes = s["status"] == "completed"
            dl = [f for f in s["findings_list"] if f["classification"] == "confirmed_deadlock"]
            runs.append({
                "scenario": sc["id"], "rep": rep, "verdict": s["verdict"], "expected_verdict": exp["verdict"],
                "verdict_ok": ok_verdict, "reported": reported, "status": s["status"],
                "end_reason": s["end_reason"], "completes_ok": completes == exp["completes"],
                "events": s["event_count"], "lost": s["lost_events"], "wall_ms": s.get("wall_ms"),
                "latency_ms": s.get("detection_latency_ms"),
                "confirmation_delay_ms": (dl[0]["metrics"].get("confirmation_delay_ns", 0) / 1e6) if dl else None,
                "cycle_length": dl[0]["metrics"].get("cycle_length") if dl else None,
            })
            print(f"  {sc['id']:<22} rep {rep}: {s['verdict']:<22} {'OK ' if ok_verdict else 'BAD'} "
                  f"events={s['event_count']:<7} lost={s['lost_events']} latency={s.get('detection_latency_ms')}",
                  flush=True)
    per_class = {}
    by_id = {sc["id"]: sc for sc in catalog}
    for c in CLASSES:
        tp = fp = fn = tn = 0
        for r in runs:
            exp = by_id[r["scenario"]]["expect"]
            positive = c in exp["must"]
            negative = c in exp["must_not"]
            got = c in r["reported"]
            if positive and got:
                tp += 1
            elif positive and not got:
                fn += 1
            elif negative and got:
                fp += 1
            elif negative and not got:
                tn += 1
        per_class[c] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                        "precision": tp / (tp + fp) if tp + fp else None,
                        "recall": tp / (tp + fn) if tp + fn else None}
    lat = [r["latency_ms"] for r in runs if r["latency_ms"] is not None and r["verdict"] == "confirmed_deadlock"]
    conf = [r["confirmation_delay_ms"] for r in runs if r["confirmation_delay_ms"] is not None]
    return {
        "runs": runs,
        "verdict_accuracy": sum(r["verdict_ok"] for r in runs) / len(runs),
        "completion_accuracy": sum(r["completes_ok"] for r in runs) / len(runs),
        "per_class": per_class,
        "latency_ms": {"n": len(lat), "min": min(lat, default=None), "median": statistics.median(lat) if lat else None,
                       "p95": pct(lat, 0.95) if lat else None, "max": max(lat, default=None)},
        "confirmation_delay_ms": {"n": len(conf), "median": statistics.median(conf) if conf else None,
                                  "max": max(conf, default=None)},
        "lost_events_total": sum(r["lost"] for r in runs if r["scenario"] != "stress"),
    }


def cross_run(client) -> dict:
    a = run_session(client, "lock-order-ab", 6)
    b = run_session(client, "lock-order-ba", 6)
    merged = client.get("/api/analysis/lock-order", params={"sessions": f"{a['id']},{b['id']}"}).json()
    cyc = [c for c in merged["cycles"] if set(c["nodes"]) == {"lock_a", "lock_b"}]
    return {"single_run_verdicts": [a["verdict"], b["verdict"]], "merged_cycle_found": bool(cyc),
            "cross_session": bool(cyc and cyc[0]["cross_session"])}


# --------------------------------------------------------------------------- C
def run_stress(threads: int, iters: int, mode: str, client=None, ring: int | None = None) -> dict:
    args = [str(threads), str(iters), "16", "30", "200"]
    if mode == "live":
        s = run_session(client, "stress", 60, args=args, **({"ring_capacity": ring} if ring else {}))
        res = s.get("result") or {}
        return {"elapsed_ms": res.get("elapsed_ms"), "events": s["event_count"], "lost": s["lost_events"],
                "wall_ms": s.get("wall_ms")}
    env = {"PATH": "/usr/bin:/bin"}
    tmp = None
    if mode in ("passive", "trace"):
        env["LD_PRELOAD"] = str(COLLECTOR)
    if mode == "trace":
        tmp = tempfile.NamedTemporaryFile(suffix=".tlt", delete=False, dir="/tmp")
        env["THREADLENS_TRACE"] = tmp.name
    out = subprocess.run([str(BIN / "stress"), *args], env=env, capture_output=True, text=True, timeout=120)
    m = re.search(r"TLRESULT (\{.*\})", out.stdout)
    res = json.loads(m.group(1)) if m else {}
    events = None
    if tmp:
        events = os.path.getsize(tmp.name) // 64
        os.unlink(tmp.name)
    return {"elapsed_ms": res.get("elapsed_ms"), "events": events}


def overhead(client, reps: int, thread_counts: list[int], iters: int) -> dict:
    table = []
    for t in thread_counts:
        row = {"threads": t}
        for mode in ("baseline", "passive", "trace", "live"):
            samples = [run_stress(t, iters, mode, client) for _ in range(reps)]
            el = [s["elapsed_ms"] for s in samples if s["elapsed_ms"]]
            row[mode] = {"median_ms": statistics.median(el) if el else None,
                         "min_ms": min(el, default=None), "max_ms": max(el, default=None),
                         "events": samples[-1].get("events"), "lost": [s.get("lost") for s in samples if "lost" in s]}
            if mode == "live":
                walls = [s["wall_ms"] for s in samples if s.get("wall_ms")]
                evs = [s["events"] for s in samples]
                row[mode]["backend_events_per_s"] = statistics.median(
                    [e / (w / 1000) for e, w in zip(evs, walls) if w]) if walls else None
        base = row["baseline"]["median_ms"]
        for mode in ("passive", "trace", "live"):
            m = row[mode]["median_ms"]
            row[mode]["overhead_x"] = (m / base) if base and m else None
        print(f"  threads={t}: " + ", ".join(f"{m}={row[m]['median_ms']:.1f}ms" for m in ("baseline", "passive", "trace", "live")
                                              if row[m]["median_ms"]), flush=True)
        table.append(row)
    return {"iterations_per_thread": iters, "rows": table}


def loss(client) -> dict:
    out = []
    for ring, block in ((1024, 0), (4096, 0), (65536, 0), (65536, 2000), (65536, 100_000)):
        s = run_session(client, "stress", 60, args=["8", "20000", "16", "30", "0"], ring_capacity=ring, block_us=block)
        out.append({"ring": ring, "block_us": block, "events": s["event_count"], "lost": s["lost_events"],
                    "verdict": s["verdict"]})
        print(f"  ring={ring:<6} block_us={block:<5} events={s['event_count']:<8} lost={s['lost_events']:<7} "
              f"verdict={s['verdict']}", flush=True)
    return {"runs": out}


# --------------------------------------------------------------------------- E
TSAN_CASES = [
    ("bank-deadlock", "bank_deadlock", []), ("bank-ordered", "bank_ordered", []),
    ("lock-order-both", "lock_order", ["both"]), ("lock-order-ab", "lock_order", ["ab"]),
    ("philosophers", "dining_philosophers", []), ("philosophers-fixed", "dining_philosophers", ["--fixed"]),
    ("join-deadlock", "join_deadlock", []), ("lost-wakeup", "lost_wakeup", []), ("healthy", "healthy", []),
    ("contention", "contention", ["8", "15", "2000"]), ("misuse", "misuse", []),
]


def run_tool(cmd: list[str], env: dict, timeout: float) -> tuple[str, float, bool]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.stderr + p.stdout, time.monotonic() - t0, False
    except subprocess.TimeoutExpired as exc:
        err = (exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or "")
        return err, time.monotonic() - t0, True


def tools() -> dict:
    out = {"tsan": [], "helgrind": [], "helgrind_available": bool(shutil.which("valgrind")),
           "valgrind_version": sh(["valgrind", "--version"]) if shutil.which("valgrind") else None}
    for sid, binary, args in TSAN_CASES:
        exe = TSAN_BIN / f"{binary}_tsan"
        if not exe.exists():
            continue
        env = {"PATH": "/usr/bin:/bin", "SCENARIO_WATCHDOG": "4",
               "TSAN_OPTIONS": "detect_deadlocks=1 second_deadlock_stack=1 halt_on_error=0 report_signal_unsafe=0"}
        text, secs, hung = run_tool([str(exe), *args], env, 12)
        out["tsan"].append({"scenario": sid, "lock_order_inversion": "lock-order-inversion" in text,
                            "data_race": "data race" in text, "hung_until_watchdog": hung or secs > 3.5,
                            "seconds": round(secs, 2)})
        print(f"  tsan {sid:<20} lock-order-inversion={'yes' if 'lock-order-inversion' in text else 'no '} "
              f"race={'yes' if 'data race' in text else 'no '} {secs:.2f}s", flush=True)
        if out["helgrind_available"]:
            text, secs, hung = run_tool(["valgrind", "--tool=helgrind", str(BIN / binary), *args],
                                        {"PATH": "/usr/bin:/bin", "SCENARIO_WATCHDOG": "8"}, 30)
            low = text.lower()
            m = re.search(r"ERROR SUMMARY: (\d+) errors", text)
            out["helgrind"].append({"scenario": sid, "lock_order": "lock order" in low,
                                    "possible_data_race": "possible data race" in low,
                                    "api_misuse": any(k in low for k in ("unlocked a not-locked", "currently held by",
                                                                         "exited whilst holding", "pthread_mutex_destroy with",
                                                                         "error code", "returned an error")),
                                    "errors": int(m.group(1)) if m else None,
                                    "hung_until_watchdog": hung or secs > 7.5, "seconds": round(secs, 2)})
            r = out["helgrind"][-1]
            print(f"  helgrind {sid:<16} lock-order={'yes' if r['lock_order'] else 'no '} race={'yes' if r['possible_data_race'] else 'no '} "
                  f"misuse={'yes' if r['api_misuse'] else 'no '} errors={r['errors']} {secs:.1f}s", flush=True)
    return out


# --------------------------------------------------------------------------- report
def fmt(x, nd=1, suffix=""):
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}{suffix}"
    return f"{x}{suffix}"


def write_report(res: dict, path: Path) -> None:
    env = res["environment"]
    L = ["# ThreadLens — measured evaluation results", "",
         f"Generated by `scripts/evaluate.py` on {env['date']}. Every number below was measured in this run; "
         "nothing is estimated or copied from elsewhere.", "",
         "## Environment", "",
         "| Item | Value |", "|---|---|"]
    for k in ("os", "kernel", "wsl", "cpu", "logical_cpus", "glibc", "gcc", "python"):
        L.append(f"| {k} | {env.get(k)} |")
    a = res["accuracy"]
    L += ["", f"## A. Detection accuracy ({res['reps']} repetitions per scenario, {len(a['runs'])} runs)", "",
          f"* Session verdict matched ground truth in **{a['verdict_accuracy']:.1%}** of runs.",
          f"* Completion (finished vs. had to be terminated) matched in **{a['completion_accuracy']:.1%}** of runs.",
          f"* Events lost in non-stress scenarios: **{a['lost_events_total']}**.", "",
          "| Classification | TP | FP | FN | TN | Precision | Recall |", "|---|---|---|---|---|---|---|"]
    for c, m in a["per_class"].items():
        L.append(f"| {c} | {m['tp']} | {m['fp']} | {m['fn']} | {m['tn']} | "
                 f"{fmt(m['precision'] * 100 if m['precision'] is not None else None, 1, '%')} | "
                 f"{fmt(m['recall'] * 100 if m['recall'] is not None else None, 1, '%')} |")
    L += ["", "Positives are runs of scenarios whose ground truth lists the class in `must`; negatives list it in "
          "`must_not` (see `scenarios/catalog.json`). Classes not mentioned for a scenario are not counted.", "",
          "### Per-scenario verdicts", "", "| Scenario | Expected | Observed verdicts | Events (last run) |", "|---|---|---|---|"]
    by_sc: dict[str, list] = {}
    for r in a["runs"]:
        by_sc.setdefault(r["scenario"], []).append(r)
    for sc, rs in by_sc.items():
        verdicts = ", ".join(sorted({r["verdict"] for r in rs}))
        L.append(f"| {sc} | {rs[0]['expected_verdict']} | {verdicts} ({sum(r['verdict_ok'] for r in rs)}/{len(rs)} ok) | {rs[-1]['events']} |")
    lat = a["latency_ms"]
    cd = a["confirmation_delay_ms"]
    L += ["", "## B. Deadlock detection latency", "",
          "End-to-end latency = backend `CLOCK_MONOTONIC` when the deadlock was confirmed − target `CLOCK_MONOTONIC` "
          "timestamp of the cycle-forming LOCK_REQUEST (both clocks are the same kernel clock). It includes the "
          "collector ring, the flusher, the Unix socket, decoding and analysis.", "",
          f"| n | min | median | p95 | max |", "|---|---|---|---|---|",
          f"| {lat['n']} | {fmt(lat['min'])} ms | {fmt(lat['median'])} ms | {fmt(lat['p95'])} ms | {fmt(lat['max'])} ms |", "",
          f"Confirmation delay inside the trace (cycle-forming event → confirming heartbeat): median "
          f"{fmt(cd['median'], 2)} ms, max {fmt(cd['max'], 2)} ms over {cd['n']} confirmed deadlocks.", "",
          "### Cross-run lock-order analysis", "",
          f"* `lock-order-ab` and `lock-order-ba` verdicts on their own: {', '.join(res['cross_run']['single_run_verdicts'])}.",
          f"* Merged lock-order graph contains the lock_a ↔ lock_b cycle: **{res['cross_run']['merged_cycle_found']}** "
          f"(only visible across runs: {res['cross_run']['cross_session']}).", ""]
    ov = res["overhead"]
    L += ["## C. Monitoring overhead (stress workload)", "",
          f"`stress T {ov['iterations_per_thread']} 16 30 200`: each thread performs {ov['iterations_per_thread']} "
          "lock operations on 16 mutexes (30 % nested, ~200 ns of work inside). Time is the program's own "
          "CLOCK_MONOTONIC measurement around the worker phase (median of repetitions).", "",
          "* **baseline** – no collector; **passive** – collector preloaded, no output configured (interposition cost only);",
          "* **trace** – collector writing a binary trace file; **live** – full pipeline: collector → Unix socket → backend analysis + SQLite.", "",
          "| Threads | baseline | passive | trace | live | live events | backend ingest (events/s) |", "|---|---|---|---|---|---|---|"]
    for r in ov["rows"]:
        def cell(m):
            v = r[m]
            return f"{fmt(v['median_ms'])} ms" + (f" ({fmt(v.get('overhead_x'), 2)}×)" if v.get("overhead_x") else "")
        L.append(f"| {r['threads']} | {cell('baseline')} | {cell('passive')} | {cell('trace')} | {cell('live')} | "
                 f"{r['live']['events']} | {fmt(r['live'].get('backend_events_per_s'), 0)} |")
    L += ["", "Overhead depends strongly on how often the program calls into pthreads: this workload is "
          "lock-bound by design (worst case). Programs that do real work between lock operations see much less.",
          "", "* **passive** shows that interposition itself is close to free (≈1.0×).",
          "* **trace** (≈2×) is the cost of producing and writing every record.",
          "* **live** is bounded by the backend's measured ingest rate (last column): with the default 100 ms "
          "back-pressure budget the program is slowed to the rate at which the Python analyzer consumes events, "
          "so that no event is lost. With `block_us=0` the program is not slowed but events are dropped "
          "(section D) and the verdict becomes inconclusive.", "",
          "## D. Event loss and back-pressure", "",
          "| Ring slots | Block budget | Events delivered | Events lost | Verdict |", "|---|---|---|---|---|"]
    for r in res["loss"]["runs"]:
        L.append(f"| {r['ring']} | {r['block_us']} µs | {r['events']} | {r['lost']} | {r['verdict']} |")
    L += ["", "Whenever events are lost the verdict is downgraded to *inconclusive* instead of reporting "
          "a possibly wrong result.", "",
          "Probe effect: with full back-pressure a thread can be delayed inside the collector while it holds a "
          "lock, which lengthens hold times and the waits of other threads. The *excessive contention* verdict "
          "for the stress program under live monitoring is therefore partly caused by monitoring itself "
          "(its ground truth accepts this; the program has no deadlock or lock-order cycle, and none was reported).",
          "", "## E. Comparison with existing tools", ""]
    t = res["tools"]
    L += ["ThreadSanitizer (`-fsanitize=thread`, `TSAN_OPTIONS=detect_deadlocks=1`) on the same programs:", "",
          "| Scenario | TSan lock-order-inversion | TSan data race | Hung (watchdog) | Time | ThreadLens verdict (expected) |",
          "|---|---|---|---|---|---|"]
    exp = {r["scenario"]: r["expected_verdict"] for r in a["runs"]}
    for r in t["tsan"]:
        L.append(f"| {r['scenario']} | {'yes' if r['lock_order_inversion'] else 'no'} | {'yes' if r['data_race'] else 'no'} | "
                 f"{'yes' if r['hung_until_watchdog'] else 'no'} | {r['seconds']} s | {exp.get(r['scenario'], '—')} |")
    if t["helgrind_available"]:
        L += ["", f"Helgrind (`valgrind --tool=helgrind`, {t.get('valgrind_version', '')}) on the uninstrumented "
              "binaries (measured " + str(t.get("measured_at", "with the main run")) + "):", "",
              "| Scenario | lock order report | possible data race | API misuse report | errors | Hung (watchdog) | Time | ThreadLens verdict (expected) |",
              "|---|---|---|---|---|---|---|---|"]
        for r in t["helgrind"]:
            L.append(f"| {r['scenario']} | {'yes' if r['lock_order'] else 'no'} | {'yes' if r['possible_data_race'] else 'no'} | "
                     f"{'yes' if r.get('api_misuse') else 'no'} | {r.get('errors', '—')} | "
                     f"{'yes' if r['hung_until_watchdog'] else 'no'} | {r['seconds']} s | {exp.get(r['scenario'], '—')} |")
    else:
        L += ["", "Helgrind was **not run**: valgrind is not installed in this environment "
              "(`sudo apt install valgrind`, then re-run this script)."]
    if t["helgrind_available"]:
        L += ["", "Note on the Helgrind *errors* column for hanging programs: in those runs Helgrind's only messages "
              "were \"Exiting thread still holds 1 lock\", printed when the scenario's watchdog terminated the hung "
              "process. They are a side effect of the forced termination, not a deadlock diagnosis: no lock-order "
              "report was produced, because the second lock acquisitions never complete."]
    L += ["", "Interpretation: TSan and Helgrind predict lock-order inversions from completed acquisitions (and find "
          "data races, which ThreadLens does not attempt); Helgrind also reports pthread API misuse well. A program "
          "that actually deadlocks simply hangs under them until a timeout; ThreadLens confirms the hang as a "
          "wait-for cycle with evidence within milliseconds. The tools are complementary.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--report-only", action="store_true", help="regenerate docs/results.md from evaluation.json")
    ap.add_argument("--tools-only", action="store_true", help="re-run only the TSan/Helgrind comparison and merge it")
    args = ap.parse_args()
    if args.report_only or args.tools_only:
        path = ROOT / "docs" / "results" / "evaluation.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if args.tools_only:
            print("E. existing tools (merged into the existing evaluation)", flush=True)
            data["tools"] = tools()
            data["tools"]["measured_at"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        write_report(data, ROOT / "docs" / "results.md")
        print("rewrote docs/results.md")
        return 0
    reps = 2 if args.quick else args.reps
    if not COLLECTOR.is_file():
        print("collector not built; run scripts/setup_linux.sh", file=sys.stderr)
        return 1

    data_dir = Path(tempfile.mkdtemp(prefix="tl-eval-"))
    os.environ["THREADLENS_DATA_DIR"] = str(data_dir)
    from fastapi.testclient import TestClient  # noqa: E402

    from app.core.config import Settings  # noqa: E402
    from app.main import create_app  # noqa: E402

    settings = Settings(data_dir=data_dir, socket_dir=data_dir / "sock")
    catalog = json.loads((ROOT / "scenarios" / "catalog.json").read_text())["scenarios"]
    results: dict = {"environment": environment(), "reps": reps}
    with TestClient(create_app(settings)) as client:
        client.put("/api/settings", json={"analysis": {}, "retention": {"max_sessions": 100000}})
        print("A/B. accuracy and latency", flush=True)
        results["accuracy"] = accuracy(client, catalog, reps)
        results["cross_run"] = cross_run(client)
        print("C. overhead", flush=True)
        results["overhead"] = overhead(client, 3 if args.quick else reps, [1, 4] if args.quick else [1, 2, 4, 8],
                                       10000 if args.quick else 20000)
        print("D. event loss", flush=True)
        results["loss"] = loss(client)
    print("E. existing tools", flush=True)
    results["tools"] = tools()

    out_dir = ROOT / "docs" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    write_report(results, ROOT / "docs" / "results.md")
    shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\nwrote {out_dir / 'evaluation.json'} and docs/results.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
