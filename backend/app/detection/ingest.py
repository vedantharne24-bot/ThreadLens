"""Conservative ordering of in-band records.

The collector delivers records over one stream in sequence order, so in
practice this guard only passes events through. It exists for imported
traces and as a defence against transport bugs:

* duplicates (seq already processed)       -> dropped, counted
* out-of-order records (seq > expected)    -> held in a bounded window until
                                              the gap is filled
* a gap that is never filled               -> declared missing: the coverage
                                              becomes incomplete, which
                                              downgrades every later verdict
"""

from __future__ import annotations

import heapq

from app.detection.model import Coverage
from app.models.wire import Ev


class SequenceGuard:
    def __init__(self, coverage: Coverage, window: int = 4096, start_seq: int = 0) -> None:
        self.coverage = coverage
        self.window = window
        self.expected = start_seq
        self._heap: list[tuple[int, int, Ev]] = []
        self._held: set[int] = set()
        self._tiebreak = 0

    def push(self, ev: Ev) -> list[Ev]:
        if ev.seq < self.expected or ev.seq in self._held:
            self.coverage.duplicates += 1
            return []
        if ev.seq == self.expected and not self._heap:
            self.expected += 1
            return [ev]
        if ev.seq > self.expected:
            self.coverage.reordered += 1
        self._tiebreak += 1
        heapq.heappush(self._heap, (ev.seq, self._tiebreak, ev))
        self._held.add(ev.seq)
        out = self._drain()
        if len(self._heap) > self.window:
            out.extend(self._skip_gap())
        return out

    def _drain(self) -> list[Ev]:
        out = []
        while self._heap and self._heap[0][0] == self.expected:
            _, _, ev = heapq.heappop(self._heap)
            self._held.discard(ev.seq)
            out.append(ev)
            self.expected += 1
        return out

    def _skip_gap(self) -> list[Ev]:
        nxt = self._heap[0][0]
        self.coverage.seq_gaps += 1
        self.coverage.missing_events += nxt - self.expected
        self.expected = nxt
        return self._drain()

    def flush(self) -> list[Ev]:
        """End of stream: release everything, declaring remaining gaps."""
        out = []
        while self._heap:
            out.extend(self._drain())
            if self._heap:
                out.extend(self._skip_gap())
        return out
