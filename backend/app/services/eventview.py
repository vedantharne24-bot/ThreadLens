"""Presentation of stored/live events as the public event schema.

Events are stored compactly (numeric columns). This module turns a stored row
plus a state snapshot (for thread and lock names) into the documented JSON
event object (see app.models.events.EventOut and docs/event-schema.md).
"""

from __future__ import annotations

import errno

from app.diagnostics.findings import fmt_ms
from app.models.wire import EventType, Flag, flag_names, mutex_kind_name

TYPE_NAMES = {int(t): t.name for t in EventType}
TYPE_BY_NAME = {t.name: int(t) for t in EventType}

LOCK_TYPES = {EventType.LOCK_REQUEST, EventType.LOCK_ACQUIRED, EventType.LOCK_FAILED, EventType.LOCK_RELEASE,
              EventType.UNLOCK_FAILED, EventType.MUTEX_INIT, EventType.MUTEX_DESTROY, EventType.LOCK_NAME}
THREAD_TARGET_TYPES = {EventType.JOIN_BEGIN, EventType.JOIN_END, EventType.THREAD_CREATE,
                       EventType.THREAD_DETACH, EventType.CANCEL_REQUEST}


def errno_name(rc: int | None) -> str | None:
    if rc is None:
        return None
    if rc == 0:
        return "OK"
    return errno.errorcode.get(rc, str(rc))


class EventFormatter:
    def __init__(self, session_id: str, snapshot: dict) -> None:
        self.sid = session_id
        self.first_ts = snapshot.get("first_ts") or 0
        self.threads_by_tid: dict[int, dict] = {}
        self.threads_by_handle: dict[int, dict] = {}
        for t in snapshot.get("threads", []):
            self.threads_by_tid.setdefault(t["tid"], t)
            if t.get("handle"):
                self.threads_by_handle[t["handle"]] = t
        self.locks = {l["id"]: l for l in snapshot.get("locks", [])}

    def _thread(self, tid: int) -> dict | None:
        return self.threads_by_tid.get(tid)

    def format(self, row: dict) -> dict:
        typ = row["type"]
        t = self._thread(row["tid"])
        lock = self.locks.get(row.get("lock_ref")) if row.get("lock_ref") is not None else None
        flags = row["flags"]
        target = None
        if typ in THREAD_TARGET_TYPES:
            tt = self.threads_by_handle.get(row["obj"])
            target = tt["index"] if tt else None
        meta: dict = {}
        if typ in (EventType.LOCK_REQUEST,):
            meta["glibc_owner_tid"] = row["aux"] or None
            meta["mutex_kind"] = mutex_kind_name(row["extra"])
        elif typ in (EventType.LOCK_ACQUIRED, EventType.LOCK_FAILED):
            meta["wait_ns"] = row["aux"]
            meta["mutex_kind"] = mutex_kind_name(row["extra"])
        elif typ in (EventType.COND_WAIT_BEGIN, EventType.COND_WAIT_END):
            meta["cond"] = f"{row['obj']:#x}"
        elif typ == EventType.THREAD_START:
            meta["thread_index"] = row["extra"]
        elif typ == EventType.THREAD_EXIT:
            meta["exit_kind"] = {0: "return", 1: "pthread_exit", 2: "cancelled_or_unknown", 3: "process_exit"}.get(row["extra"])
        return {
            "session_id": self.sid,
            "event_id": f"{self.sid}:{row['seq']}",
            "seq": row["seq"],
            "ts": row["ts"],
            "rel_ns": row["ts"] - self.first_ts if self.first_ts else 0,
            "tid": row["tid"],
            "thread": t["index"] if t else None,
            "thread_label": t["label"] if t else None,
            "type": TYPE_NAMES.get(typ, str(typ)),
            "lock": lock["id"] if lock else None,
            "lock_label": lock["label"] if lock else None,
            "obj": f"{row['obj']:#x}" if row.get("obj") else None,
            "target_thread": target,
            "correlation": row.get("corr"),
            "result": row.get("result"),
            "result_name": errno_name(row.get("result")) if typ not in (EventType.THREAD_START, EventType.THREAD_EXIT) else None,
            "flags": flag_names(flags),
            "caller": f"{row['caller']:#x}" if row.get("caller") else None,
            "text": row.get("text"),
            "metadata": meta,
            "description": self.describe(row, t, lock, target),
        }

    def describe(self, row: dict, t: dict | None, lock: dict | None, target: int | None) -> str:
        who = t["label"] if t else f"tid {row['tid']}"
        lname = lock["label"] if lock else (f"{row['obj']:#x}" if row.get("obj") else "?")
        typ = row["type"]
        flags = row["flags"]
        timed = " (timed)" if flags & Flag.TIMED else ""
        if typ == EventType.LOCK_REQUEST:
            return f"{who} requests {lname}{timed}"
        if typ == EventType.LOCK_ACQUIRED:
            if flags & Flag.TRY:
                return f"{who} acquired {lname} with trylock"
            return f"{who} acquired {lname} after {fmt_ms(row['aux'])}"
        if typ == EventType.LOCK_FAILED:
            how = "trylock" if flags & Flag.TRY else "lock"
            return f"{who}: {how} on {lname} failed ({errno_name(row['result'])})"
        if typ == EventType.LOCK_RELEASE:
            return f"{who} releases {lname}"
        if typ == EventType.UNLOCK_FAILED:
            return f"{who}: unlock of {lname} failed ({errno_name(row['result'])})"
        if typ == EventType.COND_WAIT_BEGIN:
            return f"{who} waits on cond {row['obj']:#x}{timed}, releasing {lname}"
        if typ == EventType.COND_WAIT_END:
            inferred = " (inferred: cancelled)" if flags & Flag.INFERRED else ""
            return f"{who} woke from cond {row['obj']:#x} ({errno_name(row['result'])}), re-acquired {lname}{inferred}"
        if typ == EventType.COND_SIGNAL:
            return f"{who} signals cond {row['obj']:#x}"
        if typ == EventType.COND_BROADCAST:
            return f"{who} broadcasts cond {row['obj']:#x}"
        if typ in THREAD_TARGET_TYPES:
            tt = self.threads_by_handle.get(row["obj"])
            tname = tt["label"] if tt else "a thread"
            return {EventType.JOIN_BEGIN: f"{who} joins {tname}",
                    EventType.JOIN_END: f"{who} finished joining {tname} ({errno_name(row['result'])})",
                    EventType.THREAD_CREATE: f"{who} created {tname}",
                    EventType.THREAD_DETACH: f"{who} detached {tname}",
                    EventType.CANCEL_REQUEST: f"{who} requested cancellation of {tname}"}[EventType(typ)]
        if typ == EventType.THREAD_START:
            return f"{who} started" + (" (discovered late)" if flags & Flag.LATE else "")
        if typ == EventType.THREAD_EXIT:
            kinds = {0: "returned", 1: "called pthread_exit", 2: "exited (cancelled or unknown path)",
                     3: "ended with the process"}
            return f"{who} {kinds.get(row['extra'], 'exited')}"
        if typ == EventType.THREAD_NAME:
            return f"thread named \"{row.get('text')}\""
        if typ == EventType.LOCK_NAME:
            return f"lock {row['obj']:#x} named \"{row.get('text')}\""
        if typ == EventType.USER_MARK:
            return f"marker: {row.get('text')}"
        if typ == EventType.MUTEX_INIT:
            return f"{who} initialised {lname} ({mutex_kind_name(row['extra'])})"
        if typ == EventType.MUTEX_DESTROY:
            return f"{who} destroyed {lname} ({errno_name(row['result'])})"
        if typ == EventType.BARRIER_WAIT_BEGIN:
            return f"{who} waits at barrier {row['obj']:#x}"
        if typ == EventType.BARRIER_WAIT_END:
            return f"{who} passed barrier {row['obj']:#x}"
        if typ == EventType.PROCESS_START:
            return f"process started (parent pid {row['obj']})"
        if typ == EventType.PROCESS_EXIT:
            return f"process exiting ({row['obj']} events dropped in total)"
        return TYPE_NAMES.get(typ, str(typ))
