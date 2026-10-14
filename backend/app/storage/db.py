"""SQLite persistence.

Writes go through one background writer thread (batched, transactional) so
the asyncio event loop that receives events is never blocked by disk I/O.
Reads use short-lived per-thread connections; WAL mode lets readers run
while the writer appends events.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    source TEXT NOT NULL,
    scenario_id TEXT,
    label TEXT,
    executable TEXT,
    args TEXT,
    status TEXT NOT NULL,
    end_reason TEXT,
    exit_code INTEGER,
    signal INTEGER,
    pid INTEGER,
    event_count INTEGER DEFAULT 0,
    stored_events INTEGER DEFAULT 0,
    lost_events INTEGER DEFAULT 0,
    verdict TEXT,
    wall_ms REAL,
    detection_latency_ms REAL,
    summary TEXT,
    metrics TEXT,
    result TEXT,
    stdout TEXT,
    stderr TEXT,
    options TEXT
);
CREATE TABLE IF NOT EXISTS events(
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    tid INTEGER NOT NULL,
    type INTEGER NOT NULL,
    flags INTEGER NOT NULL,
    obj INTEGER,
    aux INTEGER,
    caller INTEGER,
    result INTEGER,
    extra INTEGER,
    text TEXT,
    lock_ref INTEGER,
    corr INTEGER,
    PRIMARY KEY(session_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_by_tid ON events(session_id, tid, seq);
CREATE INDEX IF NOT EXISTS events_by_lock ON events(session_id, lock_ref, seq);
CREATE INDEX IF NOT EXISTS events_by_type ON events(session_id, type, seq);
CREATE TABLE IF NOT EXISTS meta_events(
    session_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    after_seq INTEGER NOT NULL,
    ts INTEGER, type INTEGER, seq INTEGER, obj INTEGER, aux INTEGER, result INTEGER, extra INTEGER,
    PRIMARY KEY(session_id, n)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS findings(
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(session_id, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

U64 = 1 << 64
I64_MAX = (1 << 63) - 1


def s64(x: int | None) -> int | None:
    """Store unsigned 64-bit values in SQLite's signed INTEGER."""
    if x is None:
        return None
    return x - U64 if x > I64_MAX else x


def u64(x: int | None) -> int | None:
    if x is None:
        return None
    return x + U64 if x < 0 else x


EVENT_COLUMNS = ("seq", "ts", "tid", "type", "flags", "obj", "aux", "caller", "result", "extra", "text",
                 "lock_ref", "corr")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._local = threading.local()
        with self.connect() as con:
            con.executescript(SCHEMA)
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        self._writer = threading.Thread(target=self._write_loop, name="tl-db-writer", daemon=True)
        self._writer.start()

    # ------------------------------------------------------------- connections
    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.row_factory = sqlite3.Row
        return con

    def reader(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = self.connect()
        return con

    # ------------------------------------------------------------------ writer
    def _write_loop(self) -> None:
        con = self.connect()
        while True:
            item = self._queue.get()
            if item is None:
                break
            batch = [item]
            try:
                while len(batch) < 64:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            stop = False
            try:
                con.execute("BEGIN")
                for it in batch:
                    if it is None:
                        stop = True
                        continue
                    kind = it[0]
                    if kind == "many":
                        con.executemany(it[1], it[2])
                    elif kind == "one":
                        con.execute(it[1], it[2])
                    elif kind == "barrier":
                        pass
                con.execute("COMMIT")
            except Exception as exc:  # keep the writer alive; report the error
                if con.in_transaction:
                    con.execute("ROLLBACK")
                print(f"[threadlens] database write failed: {exc!r}")
            for it in batch:
                if it is not None and it[0] == "barrier":
                    it[1].set()
            if stop:
                break
        con.close()

    def submit_many(self, sql: str, rows: list[tuple]) -> None:
        if rows:
            self._queue.put(("many", sql, rows))

    def submit(self, sql: str, params: tuple | dict = ()) -> None:
        self._queue.put(("one", sql, params))

    def flush(self, timeout: float = 30.0) -> bool:
        ev = threading.Event()
        self._queue.put(("barrier", ev))
        return ev.wait(timeout)

    def close(self) -> None:
        self.flush()
        self._queue.put(None)
        self._writer.join(timeout=5)

    # ---------------------------------------------------------------- sessions
    def insert_session(self, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        self.submit(f"INSERT INTO sessions({cols}) VALUES({marks})", row)

    def update_session(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.submit(f"UPDATE sessions SET {sets} WHERE id = :_id", {**fields, "_id": sid})

    def get_session(self, sid: str) -> dict | None:
        r = self.reader().execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        return dict(r) if r else None

    def list_sessions(self, limit: int = 100, offset: int = 0, scenario_id: str | None = None) -> list[dict]:
        q = ("SELECT id, created_at, started_at, ended_at, source, scenario_id, label, executable, args, status, "
             "end_reason, exit_code, signal, pid, event_count, stored_events, lost_events, verdict, wall_ms, "
             "detection_latency_ms, metrics FROM sessions")
        params: list[Any] = []
        if scenario_id:
            q += " WHERE scenario_id = ?"
            params.append(scenario_id)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in self.reader().execute(q, params).fetchall()]

    def delete_session(self, sid: str) -> None:
        for table in ("events", "meta_events", "findings"):
            self.submit(f"DELETE FROM {table} WHERE session_id = ?", (sid,))
        self.submit("DELETE FROM sessions WHERE id = ?", (sid,))

    # ------------------------------------------------------------------ events
    def insert_events(self, sid: str, rows: list[tuple]) -> None:
        self.submit_many(
            "INSERT OR IGNORE INTO events(session_id, seq, ts, tid, type, flags, obj, aux, caller, result, extra, "
            "text, lock_ref, corr) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, r[0], r[1], r[2], r[3], r[4], s64(r[5]), s64(r[6]), s64(r[7]), r[8], r[9], r[10], r[11], r[12])
             for r in rows])

    def insert_meta(self, sid: str, rows: list[tuple]) -> None:
        self.submit_many(
            "INSERT OR IGNORE INTO meta_events(session_id, n, after_seq, ts, type, seq, obj, aux, result, extra) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(sid, n, after, ts, typ, s64(seq), s64(obj), s64(aux), res, extra)
             for (n, after, ts, typ, seq, obj, aux, res, extra) in rows])

    @staticmethod
    def _event_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("obj", "aux", "caller"):
            d[k] = u64(d.get(k))
        return d

    def query_events(self, sid: str, *, after_seq: int | None = None, before_seq: int | None = None,
                     limit: int = 200, types: list[int] | None = None, tids: list[int] | None = None,
                     lock_refs: list[int] | None = None, from_ts: int | None = None, to_ts: int | None = None,
                     descending: bool = False) -> list[dict]:
        where = ["session_id = ?"]
        params: list[Any] = [sid]
        if after_seq is not None:
            where.append("seq > ?")
            params.append(after_seq)
        if before_seq is not None:
            where.append("seq < ?")
            params.append(before_seq)
        if types:
            where.append(f"type IN ({','.join('?' * len(types))})")
            params += types
        if tids:
            where.append(f"tid IN ({','.join('?' * len(tids))})")
            params += tids
        if lock_refs:
            where.append(f"lock_ref IN ({','.join('?' * len(lock_refs))})")
            params += lock_refs
        if from_ts is not None:
            where.append("ts >= ?")
            params.append(from_ts)
        if to_ts is not None:
            where.append("ts <= ?")
            params.append(to_ts)
        order = "DESC" if descending else "ASC"
        q = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY seq {order} LIMIT ?"
        params.append(limit)
        return [self._event_dict(r) for r in self.reader().execute(q, params).fetchall()]

    def count_events(self, sid: str, **filters: Any) -> int:
        where = ["session_id = ?"]
        params: list[Any] = [sid]
        for col, key in (("type", "types"), ("tid", "tids"), ("lock_ref", "lock_refs")):
            vals = filters.get(key)
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                params += vals
        return self.reader().execute(f"SELECT COUNT(*) FROM events WHERE {' AND '.join(where)}", params).fetchone()[0]

    def get_event(self, sid: str, seq: int) -> dict | None:
        r = self.reader().execute("SELECT * FROM events WHERE session_id = ? AND seq = ?", (sid, seq)).fetchone()
        return self._event_dict(r) if r else None

    def iter_events(self, sid: str, chunk: int = 20000, upto_seq: int | None = None) -> Iterator[dict]:
        con = self.connect()
        try:
            last = -1
            while True:
                q = "SELECT * FROM events WHERE session_id = ? AND seq > ?"
                params: list[Any] = [sid, last]
                if upto_seq is not None:
                    q += " AND seq <= ?"
                    params.append(upto_seq)
                rows = con.execute(q + " ORDER BY seq LIMIT ?", params + [chunk]).fetchall()
                if not rows:
                    return
                for r in rows:
                    yield self._event_dict(r)
                last = rows[-1]["seq"]
        finally:
            con.close()

    def meta_events(self, sid: str) -> list[dict]:
        rows = self.reader().execute("SELECT * FROM meta_events WHERE session_id = ? ORDER BY n", (sid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("seq", "obj", "aux"):
                d[k] = u64(d[k])
            out.append(d)
        return out

    # ---------------------------------------------------------------- findings
    def save_findings(self, sid: str, findings: Iterable[dict]) -> None:
        self.submit_many("INSERT OR REPLACE INTO findings(session_id, id, data) VALUES(?,?,?)",
                         [(sid, f["id"], json.dumps(f)) for f in findings])

    def get_findings(self, sid: str) -> list[dict]:
        rows = self.reader().execute("SELECT data FROM findings WHERE session_id = ?", (sid,)).fetchall()
        out = [json.loads(r[0]) for r in rows]
        out.sort(key=lambda f: int(f["id"][1:]) if f["id"][1:].isdigit() else 0)
        return out

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.reader().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put_setting(self, key: str, value: Any) -> None:
        self.submit("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))

