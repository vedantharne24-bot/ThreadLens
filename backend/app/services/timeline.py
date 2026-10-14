"""Per-thread activity lanes for the Live Threads timeline.

One pass over the stored events produces state segments per thread:
running, holding (running inside a critical section), blocked_lock,
waiting_cond, blocked_join and barrier. When a lane has more segments than
the requested resolution, adjacent short segments are merged and the merged
segment keeps the most "severe" state, so short but important waits stay
visible after downsampling.
"""

from __future__ import annotations

from app.models.wire import EventType as E, Flag
from app.storage.db import Database

SEVERITY = {"running": 0, "holding": 1, "barrier": 2, "waiting_cond": 3, "blocked_join": 4, "blocked_lock": 5}


def build_timeline(db: Database, sid: str, snap: dict, max_segments: int) -> dict:
    first = snap.get("first_ts") or 0
    end = snap.get("now_ts") or first
    lanes: dict[int, dict] = {}
    for t in snap.get("threads", []):
        lanes[t["tid"]] = {"thread": t["index"], "label": t["label"], "tid": t["tid"], "segments": [],
                           "state": None, "since": None, "lock": None, "held": 0,
                           "start": t["started_ts"], "end": t.get("ended_ts")}

    def close(lane: dict, ts: int) -> None:
        if lane["state"] is not None and lane["since"] is not None and ts >= lane["since"]:
            lane["segments"].append([lane["since"] - first, ts - first, lane["state"], lane["lock"]])
        lane["since"] = ts

    def set_state(lane: dict, ts: int, state: str, lock: int | None = None) -> None:
        if lane["state"] == state and lane["lock"] == lock:
            return
        close(lane, ts)
        lane["state"], lane["lock"] = state, lock

    def running(lane: dict) -> str:
        return "holding" if lane["held"] > 0 else "running"

    for r in db.iter_events(sid):
        lane = lanes.get(r["tid"])
        if lane is None:
            continue
        typ, ts, lock = r["type"], r["ts"], r.get("lock_ref")
        if typ == E.THREAD_START:
            lane["held"] = 0
            set_state(lane, ts, "running")
        elif typ == E.THREAD_EXIT:
            close(lane, ts)
            lane["state"] = None
        elif typ == E.LOCK_REQUEST:
            set_state(lane, ts, "blocked_lock", lock)
        elif typ == E.LOCK_ACQUIRED:
            lane["held"] += 1
            set_state(lane, ts, running(lane))
        elif typ == E.LOCK_FAILED:
            if not r["flags"] & Flag.TRY:
                set_state(lane, ts, running(lane))
        elif typ == E.LOCK_RELEASE:
            lane["held"] = max(0, lane["held"] - 1)
            set_state(lane, ts, running(lane))
        elif typ == E.UNLOCK_FAILED:
            lane["held"] += 1
        elif typ == E.COND_WAIT_BEGIN:
            lane["held"] = max(0, lane["held"] - 1)
            set_state(lane, ts, "waiting_cond", lock)
        elif typ == E.COND_WAIT_END:
            lane["held"] += 1
            set_state(lane, ts, running(lane))
        elif typ == E.JOIN_BEGIN:
            set_state(lane, ts, "blocked_join")
        elif typ == E.JOIN_END:
            set_state(lane, ts, running(lane))
        elif typ == E.BARRIER_WAIT_BEGIN:
            set_state(lane, ts, "barrier")
        elif typ == E.BARRIER_WAIT_END:
            set_state(lane, ts, running(lane))

    duration = max(1, end - first)
    out = []
    downsampled = False
    for lane in lanes.values():
        if lane["state"] is not None:
            close(lane, lane["end"] or end)
        segs = lane["segments"]
        if len(segs) > max_segments:
            segs = _downsample(segs, duration, max_segments)
            downsampled = True
        out.append({"thread": lane["thread"], "label": lane["label"], "tid": lane["tid"],
                    "start_ns": (lane["start"] or first) - first,
                    "end_ns": (lane["end"] - first) if lane["end"] else None,
                    "segments": segs})
    out.sort(key=lambda x: x["thread"])
    return {"duration_ns": duration, "lanes": out, "downsampled": downsampled,
            "resolution_ns": int(duration / max_segments) if downsampled else 0}


def _downsample(segs: list[list], duration: int, n: int) -> list[list]:
    """Quantize time into n buckets; each bucket shows its dominant state.

    Blocking states are weighted ×4 so that short waits are not hidden by the
    surrounding running time. Consecutive equal buckets are merged again.
    """
    width = duration / n
    acc: list[dict] = [{} for _ in range(n)]
    for s0, s1, state, lock in segs:
        if s1 <= s0:
            continue
        weight = 4.0 if SEVERITY.get(state, 0) >= 3 else 1.0
        b0 = min(n - 1, int(s0 // width))
        b1 = min(n - 1, int((s1 - 1) // width))
        for b in range(b0, b1 + 1):
            overlap = min(s1, (b + 1) * width) - max(s0, b * width)
            if overlap > 0:
                key = (state, lock)
                acc[b][key] = acc[b].get(key, 0.0) + overlap * weight
    out: list[list] = []
    for b, scores in enumerate(acc):
        if not scores:
            continue
        state, lock = max(scores.items(), key=lambda kv: kv[1])[0]
        start, stop = int(b * width), int((b + 1) * width)
        if out and out[-1][2] == state and out[-1][3] == lock and out[-1][1] >= start - 1:
            out[-1][1] = stop
        else:
            out.append([start, stop, state, lock])
    return out
