"""Monitoring sessions: launch -> collect -> analyze -> persist -> broadcast.

One ``LiveSession`` owns:
  * a private Unix-domain socket (0600, in a 0700 directory) that the
    collector inside the target connects to;
  * the supervised target process;
  * an ``Analyzer`` fed in sequence order through a ``SequenceGuard``;
  * buffered persistence through the database writer thread;
  * a throttled broadcaster (≤ 10 updates/s) for WebSocket subscribers.

Dashboard rendering can never block the target: the target only writes to
its in-process ring buffer; the backend reads the socket in its own event
loop; WebSocket clients receive snapshots through bounded per-client queues
(the oldest update is dropped when a client is slow).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.detection.engine import AnalysisConfig, Analyzer
from app.detection.ingest import SequenceGuard
from app.models.wire import EventType, Ev, StreamDecoder, WireError, iter_trace
from app.services.eventview import EventFormatter
from app.services.process_manager import LaunchError, ManagedProcess, validate_args, validate_executable
from app.services.symbolizer import Symbolizer
from app.storage.db import Database

TERMINAL = {"completed", "failed", "killed", "error"}


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def parse_result_line(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("TLRESULT "):
            try:
                return json.loads(line[len("TLRESULT "):])
            except json.JSONDecodeError:
                return None
    return None


class Subscriber:
    def __init__(self, maxsize: int = 32) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, msg: dict) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()  # drop the oldest update; snapshots are self-contained
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(msg)


class SessionBase:
    """State and persistence shared by live and imported sessions."""

    def __init__(self, sid: str, settings: Settings, db: Database, config: AnalysisConfig,
                 executable: Path | None) -> None:
        self.id = sid
        self.settings = settings
        self.db = db
        self.symbolizer = Symbolizer(str(executable) if executable else None)
        self.analyzer = Analyzer(config, resolver=self.symbolizer)
        self.guard = SequenceGuard(self.analyzer.coverage)
        self.rows: list[tuple] = []
        self.meta_rows: list[tuple] = []
        self.meta_n = 0
        self.ingested = 0
        self.stored = 0
        self.truncated = False
        self.recent: deque[dict] = deque(maxlen=400)

    def ingest(self, records: list[Ev]) -> None:
        an = self.analyzer
        for ev in records:
            if ev.oob:
                if ev.type == EventType.HELLO:
                    self.on_hello(ev)
                an.apply_meta(ev)
                self.meta_rows.append((self.meta_n, an.seq, ev.ts, ev.type, ev.seq, ev.obj, ev.aux, ev.result, ev.extra))
                self.meta_n += 1
                continue
            for ready in self.guard.push(ev):
                self._apply(ready)

    def _apply(self, ev: Ev) -> None:
        lock_ref, corr = self.analyzer.apply(ev)
        self.ingested += 1
        row = ev.to_row() + (lock_ref, corr)
        if self.stored < self.settings.max_stored_events:
            self.rows.append(row)
            self.stored += 1
        else:
            self.truncated = True
        self.recent.append(row)

    def on_hello(self, ev: Ev) -> None:
        self.symbolizer.set_bias(ev.aux)

    def flush_rows(self) -> None:
        if self.rows:
            self.db.insert_events(self.id, self.rows)
            self.rows = []
        if self.meta_rows:
            self.db.insert_meta(self.id, self.meta_rows)
            self.meta_rows = []

    def drain_guard(self) -> None:
        for ev in self.guard.flush():
            self._apply(ev)

    def symbolize_findings(self) -> None:
        addrs = {e.caller for f in self.analyzer.findings.values() for e in f.evidence if e.caller and not e.site}
        if not addrs:
            return
        sites = self.symbolizer.sites(sorted(addrs))
        for f in self.analyzer.findings.values():
            for e in f.evidence:
                s = sites.get(e.caller)
                if s and not e.site:
                    where = f"{s['file']}:{s['line']}" if s.get("file") and s.get("line") else (s.get("file") or "")
                    e.site = f"{s.get('function') or '?'}" + (f" ({where})" if where else "")

    def final_snapshot(self) -> dict:
        snap = self.analyzer.snapshot()
        snap["storage"] = {"stored_events": self.stored, "truncated": self.truncated}
        return snap


class LiveSession(SessionBase):
    def __init__(self, sid: str, settings: Settings, db: Database, config: AnalysisConfig, *,
                 executable: Path, args: list[str], timeout_s: float, scenario_id: str | None,
                 label: str | None, kill_on_deadlock: bool, ring_capacity: int | None,
                 block_us: int | None) -> None:
        super().__init__(sid, settings, db, config, executable)
        self.executable = executable
        self.args = args
        self.timeout_s = timeout_s
        self.scenario_id = scenario_id
        self.label = label
        self.kill_on_deadlock = kill_on_deadlock
        self.ring_capacity = ring_capacity or settings.ring_capacity
        self.block_us = settings.block_us if block_us is None else block_us
        self.token = secrets.randbits(62)
        self.sock_path = settings.socket_dir / f"{sid}.sock"
        self.cwd = settings.data_dir / "sessions" / sid
        self.status = "starting"
        self.end_reason: str | None = None
        self.exit_code: int | None = None
        self.signal: int | None = None
        self.created_at = time.time()
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.started_mono: float | None = None
        self.process: ManagedProcess | None = None
        self.server: asyncio.base_events.Server | None = None
        self.connected = False
        self.rejected_connections = 0
        self.token_ok: bool | None = None
        self.stream_closed = asyncio.Event()
        self.subscribers: set[Subscriber] = set()
        self.pending_events: list[tuple] = []
        self.skipped_events = 0
        self.rate: deque[tuple[float, int]] = deque(maxlen=50)
        self.bytes_received = 0
        self.notified: dict[str, str] = {}
        self.alerts: deque[dict] = deque(maxlen=50)
        self.detection: dict[str, Any] = {}
        self.result: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._kill_scheduled = False
        self._last_version = -1
        self._last_findings_version = -1
        self._symbolizing = False
        self.done = asyncio.Event()

    # ----------------------------------------------------------------- start
    async def start(self) -> None:
        s = self.settings
        s.socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(s.socket_dir, 0o700)
        if self.sock_path.exists():
            self.sock_path.unlink()
        self.server = await asyncio.start_unix_server(self._on_connection, path=str(self.sock_path),
                                                      limit=1 << 20)
        os.chmod(self.sock_path, 0o600)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(self.cwd),
            "LD_PRELOAD": str(s.collector_path),
            "THREADLENS_SOCKET": str(self.sock_path),
            "THREADLENS_TOKEN": str(self.token),
            "THREADLENS_RING": str(self.ring_capacity),
            "THREADLENS_BLOCK_US": str(self.block_us),
            "THREADLENS_HEARTBEAT_MS": str(s.heartbeat_ms),
            # standalone safety net inside the scenarios; the supervisor normally acts first
            "SCENARIO_WATCHDOG": str(int(self.timeout_s) + 5),
        }
        # load the executable's symbol table now (worker thread), so the first lock seen
        # during the run does not stall the event loop on an `nm` subprocess
        await asyncio.to_thread(self.symbolizer.data_symbol, 0)
        self.process = ManagedProcess(self.executable, self.args, self.cwd, env, s,
                                      cpu_seconds=int(self.timeout_s * 4) + 5)
        self.db.insert_session({
            "id": self.id, "created_at": self.created_at, "source": "live", "scenario_id": self.scenario_id,
            "label": self.label, "executable": str(self.executable), "args": json.dumps(self.args),
            "status": "starting", "options": json.dumps({"timeout_s": self.timeout_s,
                                                         "kill_on_deadlock": self.kill_on_deadlock,
                                                         "ring_capacity": self.ring_capacity,
                                                         "block_us": self.block_us}),
        })
        try:
            pid = await self.process.start()
        except OSError as exc:
            self.status, self.end_reason = "error", f"spawn failed: {exc}"
            await self._close_server()
            self.db.update_session(self.id, status=self.status, end_reason=self.end_reason, ended_at=time.time())
            self.done.set()
            raise LaunchError(self.end_reason) from exc
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self.status = "running"
        self.db.update_session(self.id, status="running", started_at=self.started_at, pid=pid)
        self._tasks = [asyncio.create_task(self._supervise(), name=f"supervise-{self.id}"),
                       asyncio.create_task(self._broadcast_loop(), name=f"broadcast-{self.id}")]

    # --------------------------------------------------------- event stream
    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connected:
            # only the target process may stream; children/forks are refused
            self.rejected_connections += 1
            writer.close()
            return
        self.connected = True
        decoder = StreamDecoder()
        try:
            while True:
                chunk = await reader.read(256 * 1024)
                if not chunk:
                    break
                self.bytes_received += len(chunk)
                try:
                    records = decoder.feed(chunk)
                except WireError as exc:
                    self.analyzer.coverage.anomalies += 1
                    self.alerts.append({"level": "error", "text": f"protocol error: {exc}"})
                    break
                if records and self.token_ok is None:
                    first = records[0]
                    self.token_ok = first.type == EventType.HELLO and first.obj == self.token
                    if not self.token_ok:
                        self.alerts.append({"level": "error", "text": "collector handshake failed (bad token)"})
                        break
                self.ingest(records)
                self.rate.append((time.monotonic(), self.ingested))
                if len(self.rows) >= 5000:
                    self.flush_rows()
                self._after_ingest()
                await asyncio.sleep(0)  # let other sessions and the API run
        finally:
            writer.close()
            self.stream_closed.set()

    def _apply(self, ev: Ev) -> None:
        super()._apply(ev)
        if len(self.pending_events) < 300:
            self.pending_events.append(self.recent[-1])
        else:
            self.skipped_events += 1

    def _after_ingest(self) -> None:
        an = self.analyzer
        if an.findings_version == self._last_findings_version:
            return
        self._last_findings_version = an.findings_version
        now_mono_ns = time.monotonic_ns()
        for f in an.findings.values():
            prev = self.notified.get(f.id)
            if prev == f.status:
                continue
            self.notified[f.id] = f.status
            if f.kind == "deadlock" and f.status == "confirmed" and "deadlock" not in self.detection:
                formed_ts = f.metrics.get("formed_ts", f.detected_ts)
                # target and backend share the kernel's CLOCK_MONOTONIC, so this
                # is the true end-to-end latency (collector + transport + analysis)
                self.detection["deadlock"] = {
                    "finding": f.id,
                    "latency_ms": (now_mono_ns - formed_ts) / 1e6,
                    "confirmation_delay_ms": f.metrics.get("confirmation_delay_ns", 0) / 1e6,
                }
                self._schedule_kill("deadlock_kill")
            if f.kind == "stall" and f.status == "active" and "stall" not in self.detection:
                self.detection["stall"] = {"finding": f.id, "latency_ms": (now_mono_ns - an.last_inband_ts) / 1e6}
                self._schedule_kill("stall_kill")
            if prev is None or f.status == "confirmed":
                if f.severity in ("critical", "warning") and f.status != "filtered":
                    self.alerts.append({"level": f.severity, "finding": f.id, "title": f.title,
                                        "classification": f.classification, "status": f.status})

    def _schedule_kill(self, reason: str) -> None:
        if not self.kill_on_deadlock or self._kill_scheduled or self.process is None:
            return
        self._kill_scheduled = True

        async def later() -> None:
            await asyncio.sleep(self.settings.deadlock_grace_s)
            if self.process is not None:
                self.process.kill(reason)

        self._tasks.append(asyncio.create_task(later()))

    # ------------------------------------------------------------ supervise
    async def _supervise(self) -> None:
        assert self.process is not None
        code, sig, timed_out = await self.process.wait(self.timeout_s)
        self.exit_code, self.signal = code, sig
        if self.process.killed_by:
            self.end_reason = self.process.killed_by
        elif timed_out:
            self.end_reason = "timeout"
        elif sig:
            self.end_reason = f"signal {sig}"
        else:
            self.end_reason = "exit"
        if self.connected:
            try:
                await asyncio.wait_for(self.stream_closed.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        await self._finalize()

    async def stop(self, reason: str = "user_stop") -> None:
        if self.process is not None:
            self.process.kill(reason)

    async def _finalize(self) -> None:
        self.drain_guard()
        if not self.connected:
            self.alerts.append({"level": "warning", "text": "the collector never connected (static binary, "
                                                            "missing library, or crash before main?)"})
        self.analyzer.finalize()
        await asyncio.to_thread(self.symbolize_findings)
        self._after_ingest()
        self.flush_rows()
        self.ended_at = time.time()
        assert self.process is not None
        stdout, stderr = self.process.stdout.text(), self.process.stderr.text()
        self.result = parse_result_line(stdout)
        if self.end_reason in ("deadlock_kill", "stall_kill", "timeout", "user_stop") or self.signal:
            final_status = "killed"
        elif self.exit_code == 0:
            final_status = "completed"
        else:
            final_status = "failed"
        snap = self.final_snapshot()
        metrics = self.metrics(snap)
        self.db.save_findings(self.id, snap["findings"])
        self.db.update_session(
            self.id, status=final_status, end_reason=self.end_reason, exit_code=self.exit_code,
            signal=self.signal, ended_at=self.ended_at, event_count=self.ingested, stored_events=self.stored,
            lost_events=self.analyzer.coverage.lost_events, verdict=snap["verdict"]["classification"],
            wall_ms=metrics["wall_ms"],
            detection_latency_ms=self.detection.get("deadlock", {}).get("latency_ms"),
            summary=json.dumps(snap), metrics=json.dumps(metrics),
            result=json.dumps(self.result) if self.result else None, stdout=stdout, stderr=stderr)
        await asyncio.to_thread(self.db.flush)
        # only now report a terminal status: everything a client may ask for next is persisted
        self.status = final_status
        await self._close_server()
        self._broadcast(final=True)
        self.done.set()

    async def _close_server(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ metrics
    def event_rate(self) -> float:
        if len(self.rate) < 2:
            return 0.0
        now = time.monotonic()
        recent = [(t, n) for t, n in self.rate if now - t <= 2.0]
        if len(recent) < 2:
            return 0.0
        (t0, n0), (t1, n1) = recent[0], recent[-1]
        return (n1 - n0) / (t1 - t0) if t1 > t0 else 0.0

    def metrics(self, snap: dict | None = None) -> dict:
        an = self.analyzer
        cov = an.coverage
        end = self.ended_at or time.time()
        wall_ms = (end - self.started_at) * 1000 if self.started_at else 0.0
        hb_age = (time.monotonic_ns() - cov.last_heartbeat_ts) / 1e6 if cov.last_heartbeat_ts else None
        stats = (snap or {}).get("stats") or {}
        return {
            "events": self.ingested, "stored_events": self.stored, "truncated": self.truncated,
            "events_per_sec": round(self.event_rate(), 1) if self.status == "running" else
            round(self.ingested / (wall_ms / 1000), 1) if wall_ms > 0 else 0.0,
            "bytes_received": self.bytes_received, "wall_ms": round(wall_ms, 2),
            "process_ms": round(((an.now_ts - an.first_ts) / 1e6) if an.first_ts else 0.0, 3),
            "lost_events": cov.lost_events, "heartbeats": cov.heartbeats,
            "heartbeat_age_ms": round(hb_age, 1) if hb_age is not None and self.status == "running" else None,
            "ring_depth": cov.ring_depth, "ring_capacity": cov.ring_capacity or self.ring_capacity,
            "collector_connected": self.connected, "rejected_connections": self.rejected_connections,
            "detection": self.detection,
            **{k: stats.get(k) for k in ("lock_acquisitions", "contended_acquisitions", "total_wait_ns")},
        }

    def info(self) -> dict:
        return {
            "id": self.id, "source": "live", "status": self.status, "scenario_id": self.scenario_id,
            "label": self.label, "executable": str(self.executable), "args": self.args,
            "pid": self.process.pid if self.process else None, "created_at": self.created_at,
            "started_at": self.started_at, "ended_at": self.ended_at, "end_reason": self.end_reason,
            "exit_code": self.exit_code, "signal": self.signal, "timeout_s": self.timeout_s,
            "event_count": self.ingested, "lost_events": self.analyzer.coverage.lost_events,
            "verdict": self.analyzer.verdict()["classification"], "result": self.result,
            "metrics": self.metrics(), "alerts": list(self.alerts)[-10:],
        }

    # ---------------------------------------------------------- broadcasting
    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self.subscribers.add(sub)
        snap = self.final_snapshot()
        fmt = EventFormatter(self.id, snap)
        sub.offer({"type": "hello", "session": self.info(), "snapshot": snap,
                   "events": [fmt.format(_row_dict(r)) for r in list(self.recent)[-200:]]})
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def _broadcast(self, final: bool = False) -> None:
        an = self.analyzer
        changed = an.version != self._last_version
        if not changed and not self.pending_events and not final:
            info = self.info()
            for sub in list(self.subscribers):
                sub.offer({"type": "status", "session": info})
            return
        self._last_version = an.version
        snap = self.final_snapshot()
        fmt = EventFormatter(self.id, snap)
        events = [fmt.format(_row_dict(r)) for r in self.pending_events]
        msg = {"type": "end" if final else "update", "session": self.info(), "snapshot": snap,
               "events": events, "skipped": self.skipped_events}
        self.pending_events = []
        self.skipped_events = 0
        for sub in list(self.subscribers):
            sub.offer(msg)

    async def _broadcast_loop(self) -> None:
        interval = self.settings.broadcast_interval_s
        while self.status not in TERMINAL:
            await asyncio.sleep(interval)
            if self.status in TERMINAL:
                break
            if self.rows:
                self.flush_rows()
            if (self.analyzer.findings_version != getattr(self, "_sym_version", -1)) and not self._symbolizing:
                self._symbolizing = True
                self._sym_version = self.analyzer.findings_version
                try:
                    await asyncio.to_thread(self.symbolize_findings)
                finally:
                    self._symbolizing = False
            self._broadcast()


def _row_dict(r: tuple) -> dict:
    return {"seq": r[0], "ts": r[1], "tid": r[2], "type": r[3], "flags": r[4], "obj": r[5], "aux": r[6],
            "caller": r[7], "result": r[8], "extra": r[9], "text": r[10], "lock_ref": r[11], "corr": r[12]}


class SessionManager:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.live: dict[str, LiveSession] = {}
        self.config = AnalysisConfig(**db.get_setting("analysis", {}))

    def set_config(self, cfg: AnalysisConfig) -> None:
        self.config = cfg
        self.db.put_setting("analysis", cfg.as_dict())

    def running(self) -> list[LiveSession]:
        return [s for s in self.live.values() if s.status not in TERMINAL]

    async def create(self, *, executable: str, args: list[str], timeout_s: float | None,
                     scenario_id: str | None, label: str | None, kill_on_deadlock: bool = True,
                     ring_capacity: int | None = None, block_us: int | None = None) -> LiveSession:
        s = self.settings
        if not s.collector_path.is_file():
            raise LaunchError(f"collector not built: {s.collector_path} (run scripts/setup_linux.sh)")
        if len(self.running()) >= s.max_concurrent_sessions:
            raise LaunchError(f"too many running sessions (max {s.max_concurrent_sessions})")
        exe = validate_executable(executable, s)
        args = validate_args(args, s)
        timeout = min(max(timeout_s or s.default_timeout_s, 0.5), s.max_timeout_s)
        if ring_capacity is not None and ring_capacity & (ring_capacity - 1):
            raise LaunchError("ring_capacity must be a power of two")
        sess = LiveSession(new_session_id(), s, self.db, self.config, executable=exe, args=args,
                           timeout_s=timeout, scenario_id=scenario_id, label=label,
                           kill_on_deadlock=kill_on_deadlock, ring_capacity=ring_capacity, block_us=block_us)
        self.live[sess.id] = sess
        await sess.start()
        self._prune_live()
        return sess

    def _prune_live(self) -> None:
        done = [sid for sid, sess in self.live.items() if sess.status in TERMINAL]
        for sid in done[:-20]:  # keep a few finished sessions in memory for fast access
            del self.live[sid]
        # retention policy (Settings → "keep at most N sessions"): delete the oldest finished sessions
        keep = int(self.db.get_setting("retention", {}).get("max_sessions", 200))
        rows = self.db.list_sessions(limit=100000)
        finished = [r for r in rows if r["status"] not in ("running", "starting", "analyzing")]
        excess = len(rows) - keep
        for r in reversed(finished):  # list is newest first
            if excess <= 0:
                break
            self.db.delete_session(r["id"])
            self.live.pop(r["id"], None)
            excess -= 1

    async def import_trace(self, data: bytes, *, label: str | None, executable: str | None,
                           records: list[Ev] | None = None) -> str:
        """Analyze a recorded trace (binary .tlt, or pre-validated records)."""
        exe = None
        if executable:
            exe = validate_executable(executable, self.settings)
        sid = new_session_id()
        sess = SessionBase(sid, self.settings, self.db, self.config, exe)
        created = time.time()
        self.db.insert_session({"id": sid, "created_at": created, "started_at": created, "source": "import",
                                "label": label or "imported trace", "executable": str(exe) if exe else None,
                                "args": "[]", "status": "analyzing"})

        def work() -> dict:
            recs = records if records is not None else list(iter_trace(data))
            sess.ingest(recs)
            sess.drain_guard()
            sess.analyzer.finalize()
            sess.symbolize_findings()
            sess.flush_rows()
            return sess.final_snapshot()

        snap = await asyncio.to_thread(work)
        self.db.save_findings(sid, snap["findings"])
        an = sess.analyzer
        self.db.update_session(sid, status="completed", end_reason="imported", ended_at=time.time(),
                               event_count=sess.ingested, stored_events=sess.stored,
                               lost_events=an.coverage.lost_events, verdict=snap["verdict"]["classification"],
                               wall_ms=(an.now_ts - (an.first_ts or an.now_ts)) / 1e6,
                               summary=json.dumps(snap),
                               metrics=json.dumps({"events": sess.ingested, "stored_events": sess.stored,
                                                   "lost_events": an.coverage.lost_events,
                                                   "process_ms": (an.now_ts - (an.first_ts or an.now_ts)) / 1e6}))
        await asyncio.to_thread(self.db.flush)
        self._prune_live()
        return sid

    async def shutdown(self) -> None:
        for sess in self.running():
            await sess.stop("server_shutdown")
        for sess in list(self.live.values()):
            try:
                await asyncio.wait_for(sess.done.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        shutil.rmtree(self.settings.socket_dir, ignore_errors=True)
