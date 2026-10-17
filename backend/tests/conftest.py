"""Shared helpers: a tiny DSL for writing synthetic traces with known answers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.detection.engine import AnalysisConfig, Analyzer  # noqa: E402
from app.models.wire import EventType as E, Ev, Flag  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
BUILD = PROJECT / "build"
COLLECTOR = BUILD / "lib" / "libthreadlens.so"
BIN = BUILD / "bin"

needs_build = pytest.mark.skipif(not COLLECTOR.is_file() or sys.platform != "linux",
                                 reason="collector not built (run scripts/setup_linux.sh on Linux)")

NORMAL, RECURSIVE, ERRORCHECK = 0, 1, 2


class Trace:
    """Builds a sequence of in-band and out-of-band records."""

    def __init__(self) -> None:
        self.seq = 0
        self.ts = 1_000_000
        self.items: list[Ev] = []

    def ev(self, tid: int, typ: E, obj: int = 0, aux: int = 0, flags: int = 0, result: int = 0, extra: int = 0,
           text: str | None = None, caller: int = 0) -> Ev:
        e = Ev(self.seq, self.ts, tid, int(typ), flags, obj, aux, caller, result, extra, text)
        self.seq += 1
        self.ts += 1000
        self.items.append(e)
        return e

    def heartbeat(self, lost: int = 0) -> Ev:
        e = Ev(self.seq, self.ts, 0, int(E.HEARTBEAT), int(Flag.OOB), lost, 0, 0, 0, 0)
        self.items.append(e)
        return e

    def lost(self, total: int) -> Ev:
        e = Ev(self.seq, self.ts, 0, int(E.EVENTS_LOST), int(Flag.OOB), total, total, 0, 0, 0)
        self.items.append(e)
        return e

    def advance(self, ns: int) -> None:
        self.ts += ns

    # ---- conveniences
    def start(self, tid: int, index: int, name: str | None = None) -> None:
        self.ev(tid, E.THREAD_START, obj=self.handle(tid), extra=index)
        if name:
            self.ev(tid, E.THREAD_NAME, obj=self.handle(tid), text=name)

    @staticmethod
    def handle(tid: int) -> int:
        return 0x7F0000000000 + tid * 0x1000

    def exit(self, tid: int, kind: int = 0) -> None:
        self.ev(tid, E.THREAD_EXIT, obj=self.handle(tid), extra=kind)

    def request(self, tid: int, m: int, kind: int = NORMAL, timed: bool = False) -> None:
        self.ev(tid, E.LOCK_REQUEST, obj=m, extra=kind, flags=Flag.TIMED if timed else 0)

    def acquired(self, tid: int, m: int, kind: int = NORMAL, wait: int = 0, timed: bool = False, result: int = 0) -> None:
        self.ev(tid, E.LOCK_ACQUIRED, obj=m, aux=wait, extra=kind, flags=Flag.TIMED if timed else 0, result=result)

    def lock(self, tid: int, m: int, kind: int = NORMAL, wait: int = 0) -> None:
        self.request(tid, m, kind)
        self.acquired(tid, m, kind, wait)

    def trylock(self, tid: int, m: int, ok: bool = True, kind: int = NORMAL) -> None:
        if ok:
            self.ev(tid, E.LOCK_ACQUIRED, obj=m, extra=kind, flags=Flag.TRY)
        else:
            self.ev(tid, E.LOCK_FAILED, obj=m, extra=kind, flags=Flag.TRY, result=16)  # EBUSY

    def unlock(self, tid: int, m: int, kind: int = NORMAL) -> None:
        self.ev(tid, E.LOCK_RELEASE, obj=m, extra=kind)

    # ---- read-write locks (extra = mode | kind << 8, as TL_RW_EXTRA)
    @staticmethod
    def rw_extra(mode: str, kind: int = 0) -> int:
        return (1 if mode == "w" else 0) | (kind << 8)

    def rw_request(self, tid: int, rw: int, mode: str, kind: int = 0, timed: bool = False) -> None:
        self.ev(tid, E.RW_REQUEST, obj=rw, extra=self.rw_extra(mode, kind), flags=Flag.TIMED if timed else 0)

    def rw_acquired(self, tid: int, rw: int, mode: str, kind: int = 0, wait: int = 0, timed: bool = False,
                    try_: bool = False) -> None:
        flags = (Flag.TIMED if timed else 0) | (Flag.TRY if try_ else 0)
        self.ev(tid, E.RW_ACQUIRED, obj=rw, aux=wait, extra=self.rw_extra(mode, kind), flags=flags)

    def rdlock(self, tid: int, rw: int, kind: int = 0) -> None:
        self.rw_request(tid, rw, "r", kind)
        self.rw_acquired(tid, rw, "r", kind)

    def wrlock(self, tid: int, rw: int, kind: int = 0) -> None:
        self.rw_request(tid, rw, "w", kind)
        self.rw_acquired(tid, rw, "w", kind)

    def rw_unlock(self, tid: int, rw: int, mode: str = "r", kind: int = 0) -> None:
        self.ev(tid, E.RW_RELEASE, obj=rw, extra=self.rw_extra(mode, kind))

    # ---- semaphores
    def sem_init(self, tid: int, s: int, value: int, pshared: bool = False) -> None:
        self.ev(tid, E.SEM_INIT, obj=s, aux=value, extra=1 if pshared else 0)

    def sem_wait_begin(self, tid: int, s: int, timed: bool = False) -> None:
        self.ev(tid, E.SEM_WAIT_BEGIN, obj=s, flags=Flag.TIMED if timed else 0)

    def sem_wait_end(self, tid: int, s: int, result: int = 0, try_: bool = False) -> None:
        self.ev(tid, E.SEM_WAIT_END, obj=s, result=result, flags=Flag.TRY if try_ else 0)

    def sem_post(self, tid: int, s: int) -> None:
        self.ev(tid, E.SEM_POST, obj=s)


def run(trace: Trace, cfg: AnalysisConfig | None = None, finalize: bool = False) -> Analyzer:
    an = Analyzer(cfg or AnalysisConfig())
    for ev in trace.items:
        if ev.oob:
            an.apply_meta(ev)
        else:
            an.apply(ev)
    if finalize:
        an.finalize()
    return an


def findings(an: Analyzer, classification: str | None = None, kind: str | None = None) -> list:
    out = list(an.findings.values())
    if classification:
        out = [f for f in out if f.classification == classification]
    if kind:
        out = [f for f in out if f.kind == kind]
    return out


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("THREADLENS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


os.environ.setdefault("THREADLENS_DATA_DIR", str(Path("/tmp") / f"threadlens-test-{os.getpid()}"))
