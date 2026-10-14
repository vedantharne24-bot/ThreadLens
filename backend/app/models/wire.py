"""Decoder for the collector's 64-byte binary records (mirror of tl_wire.h).

The hot path avoids per-record Pydantic validation: records are decoded with
``struct.iter_unpack`` and checked structurally (magic, version, known type).
The public, validated schema lives in :mod:`app.models.events`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator

WIRE_MAGIC = 0x4C54
WIRE_VERSION = 1
WIRE_SIZE = 64
WIRE_FORMAT = "<HBBHHQQIIQQQiI"
assert struct.calcsize(WIRE_FORMAT) == WIRE_SIZE
_STRUCT = struct.Struct(WIRE_FORMAT)


class EventType(IntEnum):
    HELLO = 1
    PROCESS_START = 2
    PROCESS_EXIT = 3
    HEARTBEAT = 4
    EVENTS_LOST = 5
    THREAD_CREATE = 10
    THREAD_START = 11
    THREAD_EXIT = 12
    THREAD_NAME = 13
    JOIN_BEGIN = 14
    JOIN_END = 15
    THREAD_DETACH = 16
    CANCEL_REQUEST = 17
    MUTEX_INIT = 20
    MUTEX_DESTROY = 21
    LOCK_REQUEST = 22
    LOCK_ACQUIRED = 23
    LOCK_FAILED = 24
    LOCK_RELEASE = 25
    UNLOCK_FAILED = 26
    LOCK_NAME = 27
    COND_WAIT_BEGIN = 30
    COND_WAIT_END = 31
    COND_SIGNAL = 32
    COND_BROADCAST = 33
    BARRIER_WAIT_BEGIN = 40
    BARRIER_WAIT_END = 41
    USER_MARK = 50


KNOWN_TYPES = frozenset(int(t) for t in EventType)
STRING_TYPES = frozenset({EventType.THREAD_NAME, EventType.LOCK_NAME, EventType.USER_MARK})
OOB_TYPES = frozenset({EventType.HELLO, EventType.HEARTBEAT, EventType.EVENTS_LOST})


class Flag(IntEnum):
    TIMED = 0x0001
    TRY = 0x0002
    OOB = 0x0004
    INFERRED = 0x0008
    LATE = 0x0010
    SERIAL = 0x0020


def flag_names(flags: int) -> list[str]:
    return [f.name.lower() for f in Flag if flags & f]


class ExitKind(IntEnum):
    RETURN = 0
    PTHREAD_EXIT = 1
    UNKNOWN = 2
    PROCESS = 3


# glibc mutex "kind" (pthread_mutex_t.__data.__kind)
MUTEX_KIND_MASK = 0x3
MUTEX_ROBUST_BIT = 0x10
MUTEX_KIND_NAMES = {0: "normal", 1: "recursive", 2: "errorcheck", 3: "adaptive"}


def mutex_kind_name(kind: int) -> str:
    base = MUTEX_KIND_NAMES.get(kind & MUTEX_KIND_MASK, "normal")
    return base + ("+robust" if kind & MUTEX_ROBUST_BIT else "")


@dataclass(slots=True)
class Ev:
    """One decoded record. ``text`` is set for string-carrying types."""

    seq: int
    ts: int
    tid: int
    type: int
    flags: int
    obj: int
    aux: int
    caller: int
    result: int
    extra: int
    text: str | None = None
    pid: int = 0

    @property
    def oob(self) -> bool:
        return bool(self.flags & Flag.OOB)

    def to_row(self) -> tuple:
        return (self.seq, self.ts, self.tid, self.type, self.flags, self.obj, self.aux,
                self.caller, self.result, self.extra, self.text)


class WireError(ValueError):
    pass


def _decode_one(fields: tuple, raw: memoryview | bytes) -> Ev:
    magic, version, etype, flags, _res, seq, ts, pid, tid, obj, aux, caller, result, extra = fields
    if magic != WIRE_MAGIC or version != WIRE_VERSION:
        raise WireError(f"bad record header magic={magic:#x} version={version}")
    if etype not in KNOWN_TYPES:
        raise WireError(f"unknown event type {etype}")
    text = None
    if etype in STRING_TYPES:
        text = bytes(raw[40:64]).split(b"\0", 1)[0].decode("utf-8", "replace")
        aux = caller = result = extra = 0
    return Ev(seq, ts, tid, etype, flags, obj, aux, caller, result, extra, text, pid)


class StreamDecoder:
    """Incremental decoder: feed arbitrary byte chunks, get whole records.

    Validation performed per record: header magic/version, known type.
    Sequence checks (gaps, duplicates, reordering) are done by the session's
    ingest layer because they need session context.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.records = 0
        self.bytes = 0

    def feed(self, data: bytes) -> list[Ev]:
        self.bytes += len(data)
        self._buf.extend(data)
        whole = len(self._buf) - (len(self._buf) % WIRE_SIZE)
        if whole == 0:
            return []
        view = memoryview(self._buf)[:whole]
        out: list[Ev] = []
        try:
            for i, fields in enumerate(_STRUCT.iter_unpack(view)):
                etype = fields[2]
                if etype in STRING_TYPES:
                    out.append(_decode_one(fields, view[i * WIRE_SIZE:(i + 1) * WIRE_SIZE]))
                else:
                    out.append(_decode_one(fields, b""))
        finally:
            view.release()
        del self._buf[:whole]
        self.records += len(out)
        return out

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


def iter_trace(data: bytes) -> Iterator[Ev]:
    """Decode a complete trace file (THREADLENS_TRACE output)."""
    if len(data) % WIRE_SIZE:
        raise WireError(f"trace size {len(data)} is not a multiple of {WIRE_SIZE}")
    dec = StreamDecoder()
    yield from dec.feed(data)


def encode(ev: Ev) -> bytes:
    """Encode a record (used by tests and the NDJSON importer)."""
    if ev.type in STRING_TYPES:
        head = _STRUCT.pack(WIRE_MAGIC, WIRE_VERSION, ev.type, ev.flags, 0, ev.seq, ev.ts,
                            ev.pid, ev.tid, ev.obj, 0, 0, 0, 0)
        s = (ev.text or "").encode()[:23]
        return head[:40] + s + b"\0" * (24 - len(s))
    return _STRUCT.pack(WIRE_MAGIC, WIRE_VERSION, ev.type, ev.flags, 0, ev.seq, ev.ts, ev.pid,
                        ev.tid, ev.obj, ev.aux, ev.caller, ev.result, ev.extra & 0xFFFFFFFF)
