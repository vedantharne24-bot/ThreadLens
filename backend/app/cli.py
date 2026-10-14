"""Offline analysis of a ThreadLens trace file.

    THREADLENS_TRACE=/tmp/run.tlt LD_PRELOAD=build/lib/libthreadlens.so build/bin/bank_deadlock
    python -m app.cli analyze /tmp/run.tlt --exe build/bin/bank_deadlock
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.detection.engine import AnalysisConfig, Analyzer
from app.detection.ingest import SequenceGuard
from app.models.wire import EventType, iter_trace
from app.services.symbolizer import Symbolizer


def analyze_trace(data: bytes, exe: str | None = None, config: AnalysisConfig | None = None) -> Analyzer:
    sym = Symbolizer(exe)
    an = Analyzer(config, resolver=sym)
    guard = SequenceGuard(an.coverage)
    for ev in iter_trace(data):
        if ev.oob:
            if ev.type == EventType.HELLO:
                sym.set_bias(ev.aux)
            an.apply_meta(ev)
            continue
        for ready in guard.push(ev):
            an.apply(ready)
    for ready in guard.flush():
        an.apply(ready)
    an.finalize()
    return an


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="threadlens")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="analyze a binary trace file")
    a.add_argument("trace")
    a.add_argument("--exe", help="executable that produced the trace (for symbol names)")
    a.add_argument("--json", action="store_true", help="print the full snapshot as JSON")
    args = p.parse_args(argv)

    an = analyze_trace(Path(args.trace).read_bytes(), args.exe)
    snap = an.snapshot()
    if args.json:
        json.dump(snap, sys.stdout, indent=2, default=str)
        return 0
    v = snap["verdict"]
    print(f"verdict: {v['label']}")
    print(f"events: {snap['stats']['events']}  threads: {snap['stats']['threads_total']}  "
          f"locks: {snap['stats']['locks_total']}  coverage complete: {snap['coverage']['complete']}")
    for f in snap["findings"]:
        print(f"\n[{f['severity'].upper()}] {f['id']} {f['classification']} ({f['status']}): {f['title']}")
        for line in f["explanation"]:
            print(f"    {line}")
        for e in f["evidence"]:
            print(f"      seq {e['seq']:>6}  {e['role']:<8} {e['description']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
