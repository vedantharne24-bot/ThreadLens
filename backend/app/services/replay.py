"""Event replay: reconstruct the analysis state at any recorded sequence number.

This is EVENT replay — re-applying the recorded events to the deterministic
analyzer — not deterministic EXECUTION replay (as done by rr). The program
is not re-executed; we reconstruct what ThreadLens knew at that point.

Out-of-band records (heartbeats, loss reports) are stored with the in-band
sequence number after which they were applied, and are re-applied at exactly
the same position, so a replayed snapshot is identical to the live one.

Random access is O(K) events per query using pickled checkpoints taken every
K events (built once per session, then cached).
"""

from __future__ import annotations

import bisect
import pickle
import threading
from collections import OrderedDict

from app.detection.engine import AnalysisConfig, Analyzer
from app.models.wire import EventType, Ev
from app.services.symbolizer import Symbolizer
from app.storage.db import Database

CHECKPOINT_EVERY = 5000


def row_to_ev(r: dict) -> Ev:
    return Ev(r["seq"], r["ts"], r["tid"], r["type"], r["flags"], r["obj"] or 0, r["aux"] or 0, r["caller"] or 0,
              r["result"] or 0, r["extra"] or 0, r["text"])


def meta_to_ev(m: dict) -> Ev:
    return Ev(m["seq"] or 0, m["ts"] or 0, 0, m["type"], 0x4, m["obj"] or 0, m["aux"] or 0, 0, m["result"] or 0,
              m["extra"] or 0)


class ReplayIndex:
    def __init__(self, db: Database, sid: str, config: AnalysisConfig, executable: str | None,
                 final: bool) -> None:
        self.db = db
        self.sid = sid
        self.config = config
        self.executable = executable
        self.final = final
        self.metas = db.meta_events(sid)
        self.bias = next((m["aux"] for m in self.metas if m["type"] == EventType.HELLO), 0)
        self.checkpoints: list[tuple[int, bytes]] = []
        self.cp_seqs: list[int] = []
        self.last_seq = -1
        self.count = 0
        self._sym = Symbolizer(executable, self.bias)
        self._build()

    def _new(self) -> Analyzer:
        an = Analyzer(self.config, resolver=self._sym)
        return an

    def _metas_after(self, an: Analyzer, after_seq: int, start: int) -> int:
        i = start
        while i < len(self.metas) and self.metas[i]["after_seq"] <= after_seq:
            an.apply_meta(meta_to_ev(self.metas[i]))
            i += 1
        return i

    def _build(self) -> None:
        an = self._new()
        mi = self._metas_after(an, -1, 0)
        self.checkpoints.append((-1, pickle.dumps((an, mi))))
        self.cp_seqs.append(-1)
        n = 0
        for r in self.db.iter_events(self.sid):
            an.apply(row_to_ev(r))
            mi = self._metas_after(an, r["seq"], mi)
            n += 1
            self.last_seq = r["seq"]
            if n % CHECKPOINT_EVERY == 0:
                self.checkpoints.append((r["seq"], pickle.dumps((an, mi))))
                self.cp_seqs.append(r["seq"])
        self.count = n

    def state_at(self, seq: int) -> dict:
        seq = min(seq, self.last_seq) if self.last_seq >= 0 else -1
        i = bisect.bisect_right(self.cp_seqs, seq) - 1
        cp_seq, blob = self.checkpoints[max(i, 0)]
        an, mi = pickle.loads(blob)
        an.resolver = self._sym
        if seq > cp_seq:
            for r in self._events_between(cp_seq, seq):
                an.apply(row_to_ev(r))
                mi = self._metas_after(an, r["seq"], mi)
        if self.final and seq >= self.last_seq:
            an.finalize()
        snap = an.snapshot()
        snap["replay"] = {"seq": seq, "last_seq": self.last_seq, "events": self.count}
        return snap

    def _events_between(self, after: int, upto: int):
        last = after
        while True:
            rows = self.db.query_events(self.sid, after_seq=last, before_seq=upto + 1, limit=5000)
            if not rows:
                return
            yield from rows
            last = rows[-1]["seq"]


class ReplayCache:
    def __init__(self, db: Database, size: int = 8) -> None:
        self.db = db
        self.size = size
        self._cache: OrderedDict[str, ReplayIndex] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sid: str, config: AnalysisConfig, executable: str | None, final: bool) -> ReplayIndex:
        with self._lock:
            idx = self._cache.get(sid)
            if idx is not None and final and idx.final:
                self._cache.move_to_end(sid)
                return idx
        idx = ReplayIndex(self.db, sid, config, executable, final)
        if final:
            with self._lock:
                self._cache[sid] = idx
                while len(self._cache) > self.size:
                    self._cache.popitem(last=False)
        return idx

    def invalidate(self, sid: str) -> None:
        with self._lock:
            self._cache.pop(sid, None)
