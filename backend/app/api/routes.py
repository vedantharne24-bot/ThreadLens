"""REST API."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import platform
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api import deps
from app.api.deps import check_sid, row_public, session_row, session_snapshot
from app.core.config import Settings
from app.detection.engine import AnalysisConfig
from app.models.events import EventIn, SessionCreate, SettingsPayload
from app.models.wire import WIRE_MAGIC, EventType, Ev
from app.services.compare import compare_sessions, merge_lock_order
from app.services.eventview import TYPE_BY_NAME, TYPE_NAMES, EventFormatter
from app.services.process_manager import LaunchError
from app.services.replay import ReplayCache
from app.services.scenarios import Catalog, ScenarioError
from app.services.session import SessionManager
from app.services.symbolizer import Symbolizer
from app.services.timeline import build_timeline
from app.storage.db import Database

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ system

@router.get("/health")
def health(s: Settings = Depends(deps.settings), cat: Catalog = Depends(deps.catalog),
           mgr: SessionManager = Depends(deps.manager)) -> dict:
    scen = cat.list()
    return {
        "status": "ok", "version": "1.0.0", "platform": platform.platform(), "python": platform.python_version(),
        "collector_built": s.collector_path.is_file(), "collector_path": str(s.collector_path),
        "scenarios_total": len(scen), "scenarios_built": sum(1 for x in scen if x["built"]),
        "running_sessions": len(mgr.running()), "max_sessions": s.max_concurrent_sessions,
        "supported_primitives": ["pthread_mutex (normal, recursive, errorcheck, adaptive, robust)",
                                 "pthread_mutex_trylock / timedlock / clocklock",
                                 "pthread_rwlock (rdlock, wrlock, try / timed / clock variants, unlock, init, "
                                 "destroy; shared readers in the wait-for graph)",
                                 "pthread_cond (wait, timedwait, clockwait, signal, broadcast)",
                                 "sem_t (sem_wait, trywait, timedwait, clockwait, post, init, destroy; no owner, "
                                 "stall detection only)",
                                 "pthread_create / join / detach / exit / cancel",
                                 "pthread_barrier_wait (state only)", "pthread_setname_np"],
        "unsupported_primitives": ["pthread_spin", "sem_open / sem_close (named semaphores are tracked from "
                                   "their first wait/post; initial count unknown)",
                                   "futex / atomics-based custom locks", "statically linked programs"],
    }


@router.get("/settings")
def get_settings(mgr: SessionManager = Depends(deps.manager), db: Database = Depends(deps.db)) -> dict:
    return {"analysis": mgr.config.as_dict(), "retention": db.get_setting("retention", {"max_sessions": 200})}


@router.put("/settings")
def put_settings(payload: SettingsPayload, mgr: SessionManager = Depends(deps.manager),
                 db: Database = Depends(deps.db)) -> dict:
    mgr.set_config(AnalysisConfig(**payload.analysis.model_dump()))
    db.put_setting("retention", payload.retention.model_dump())
    return get_settings(mgr, db)


# --------------------------------------------------------------- scenarios

@router.get("/scenarios")
def list_scenarios(cat: Catalog = Depends(deps.catalog)) -> list[dict]:
    return cat.list()


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str, cat: Catalog = Depends(deps.catalog)) -> dict:
    try:
        return cat.get(scenario_id)
    except ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/scenarios/{scenario_id}/source")
def scenario_source(scenario_id: str, cat: Catalog = Depends(deps.catalog)) -> dict:
    try:
        return {"scenario": scenario_id, "files": cat.source(scenario_id)}
    except ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------- sessions

@router.post("/sessions", status_code=201)
async def create_session(req: SessionCreate, mgr: SessionManager = Depends(deps.manager),
                         cat: Catalog = Depends(deps.catalog)) -> dict:
    if bool(req.scenario_id) == bool(req.executable):
        raise HTTPException(422, "give exactly one of scenario_id or executable")
    try:
        if req.scenario_id:
            sc = cat.get(req.scenario_id)
            exe = sc["binary_path"]
            args = req.args if req.args is not None else sc.get("args", [])
            timeout = req.timeout_s or sc.get("timeout_s")
            label = req.label or sc["title"]
        else:
            exe, args, timeout, label = req.executable, req.args or [], req.timeout_s, req.label
        sess = await mgr.create(executable=exe, args=args, timeout_s=timeout, scenario_id=req.scenario_id,
                                label=label, kill_on_deadlock=req.kill_on_deadlock,
                                ring_capacity=req.ring_capacity, block_us=req.block_us)
    except ScenarioError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LaunchError as exc:
        raise HTTPException(400, str(exc)) from exc
    return sess.info()


@router.get("/sessions")
def list_sessions(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                  scenario_id: str | None = Query(None, max_length=64), db: Database = Depends(deps.db),
                  mgr: SessionManager = Depends(deps.manager)) -> list[dict]:
    rows = [row_public(r) for r in db.list_sessions(limit, offset, scenario_id)]
    for r in rows:  # live sessions: report fresh counters
        live = mgr.live.get(r["id"])
        if live is not None and not live.done.is_set():
            r.update(status=live.status, event_count=live.ingested, verdict=live.analyzer.verdict()["classification"],
                     metrics=live.metrics())
    return rows


@router.get("/sessions/{sid}")
def get_session(sid: str, db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager)) -> dict:
    live = mgr.live.get(check_sid(sid))
    if live is not None and not live.done.is_set():
        return live.info()
    row = session_row(db, sid)
    out = row_public(row)
    out["stdout"] = row.get("stdout")
    out["stderr"] = row.get("stderr")
    if row.get("summary"):
        snap = json.loads(row["summary"])
        out["verdict_detail"] = snap.get("verdict")
        out["coverage"] = snap.get("coverage")
        out["stats"] = snap.get("stats")
    return out


@router.post("/sessions/{sid}/stop")
async def stop_session(sid: str, mgr: SessionManager = Depends(deps.manager)) -> dict:
    live = mgr.live.get(check_sid(sid))
    if live is None or live.done.is_set():
        raise HTTPException(409, "session is not running")
    await live.stop("user_stop")
    try:
        await asyncio.wait_for(live.done.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    return live.info()


@router.delete("/sessions/{sid}", status_code=204)
def delete_session(sid: str, db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager),
                   cache: ReplayCache = Depends(deps.replay_cache)) -> None:
    live = mgr.live.get(check_sid(sid))
    if live is not None and not live.done.is_set():
        raise HTTPException(409, "stop the session before deleting it")
    session_row(db, sid)
    mgr.live.pop(sid, None)
    cache.invalidate(sid)
    db.delete_session(sid)
    db.flush()


@router.post("/sessions/import", status_code=201)
async def import_session(request: Request, label: str | None = Query(None, max_length=120),
                         executable: str | None = Query(None, max_length=1024),
                         s: Settings = Depends(deps.settings), mgr: SessionManager = Depends(deps.manager)) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > s.max_import_bytes:
            raise HTTPException(413, f"trace larger than {s.max_import_bytes} bytes")
    data = bytes(body)
    records = None
    if len(data) >= 2 and int.from_bytes(data[:2], "little") == WIRE_MAGIC:
        pass  # binary trace (THREADLENS_TRACE output)
    else:
        records = parse_ndjson(data)
    try:
        sid = await mgr.import_trace(data, label=label, executable=executable, records=records)
    except LaunchError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, f"invalid trace: {exc}") from exc
    return {"id": sid}


def parse_ndjson(data: bytes) -> list[Ev]:
    out = []
    for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = EventIn.model_validate_json(line)
        except ValidationError as exc:
            raise HTTPException(422, f"line {n}: {exc.errors()[0]['msg']}") from exc
        out.append(Ev(e.seq, e.ts, e.tid, TYPE_BY_NAME[e.type], e.flag_bits(), e.obj, e.aux, e.caller, e.result,
                      e.extra, e.text, e.pid))
    if not out:
        raise HTTPException(422, "empty trace")
    return out


@router.get("/sessions/{sid}/state")
def session_state(sid: str, db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager)) -> dict:
    snap, _ = session_snapshot(mgr, db, check_sid(sid))
    return snap


@router.get("/sessions/{sid}/findings")
def session_findings(sid: str, db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager)) -> list:
    snap, _ = session_snapshot(mgr, db, check_sid(sid))
    return snap.get("findings", [])


def _parse_types(types: str | None) -> list[int] | None:
    if not types:
        return None
    out = []
    for t in types.split(","):
        t = t.strip().upper()
        if t not in TYPE_BY_NAME:
            raise HTTPException(422, f"unknown event type {t}")
        out.append(TYPE_BY_NAME[t])
    return out


def _parse_ints(v: str | None, what: str) -> list[int] | None:
    if not v:
        return None
    try:
        return [int(x) for x in v.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(422, f"{what} must be a comma-separated list of integers") from exc


@router.get("/sessions/{sid}/events")
def session_events(sid: str,
                   after_seq: int | None = Query(None, ge=-1), before_seq: int | None = Query(None, ge=0),
                   limit: int = Query(200, ge=1, le=2000), types: str | None = Query(None, max_length=600),
                   threads: str | None = Query(None, max_length=600), locks: str | None = Query(None, max_length=600),
                   from_ns: int | None = Query(None, ge=0), to_ns: int | None = Query(None, ge=0),
                   q: str | None = Query(None, max_length=100), order: str = Query("asc", pattern="^(asc|desc)$"),
                   db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager)) -> dict:
    snap, live = session_snapshot(mgr, db, check_sid(sid))
    if live is not None:
        live.flush_rows()
        db.flush(timeout=5)
    fmt = EventFormatter(sid, snap)
    type_ids = _parse_types(types)
    thread_idx = _parse_ints(threads, "threads")
    tids = None
    if thread_idx is not None:
        tids = [t["tid"] for t in snap.get("threads", []) if t["index"] in set(thread_idx)] or [-1]
    lock_ids = _parse_ints(locks, "locks")
    first = snap.get("first_ts") or 0
    from_ts = first + from_ns if from_ns is not None else None
    to_ts = first + to_ns if to_ns is not None else None
    desc = order == "desc"
    needle = q.lower() if q else None
    out: list[dict] = []
    cursor_after, cursor_before = after_seq, before_seq
    scanned = 0
    while len(out) < limit and scanned < 50000:
        rows = db.query_events(sid, after_seq=cursor_after, before_seq=cursor_before, limit=min(2000, limit * 4),
                               types=type_ids, tids=tids, lock_refs=lock_ids, from_ts=from_ts, to_ts=to_ts,
                               descending=desc)
        if not rows:
            break
        scanned += len(rows)
        for r in rows:
            e = fmt.format(r)
            if needle and needle not in e["description"].lower() and needle not in e["type"].lower():
                continue
            out.append(e)
            if len(out) >= limit:
                break
        last = out[-1]["seq"] if len(out) >= limit else rows[-1]["seq"]
        if desc:
            cursor_before = last
        else:
            cursor_after = last
        if len(rows) < min(2000, limit * 4):
            break
    total = db.count_events(sid, types=type_ids, tids=tids, lock_refs=lock_ids) if not (needle or from_ts or to_ts) else None
    return {"events": out, "total": total, "has_more": len(out) >= limit,
            "next_cursor": out[-1]["seq"] if out else None, "type_names": list(TYPE_NAMES.values())}


@router.get("/sessions/{sid}/events/{seq}")
def session_event(sid: str, seq: int, db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager),
                  s: Settings = Depends(deps.settings)) -> dict:
    snap, live = session_snapshot(mgr, db, check_sid(sid))
    if live is not None:
        live.flush_rows()
        db.flush(timeout=5)
    r = db.get_event(sid, seq)
    if r is None:
        raise HTTPException(404, "event not found")
    fmt = EventFormatter(sid, snap)
    ev = fmt.format(r)
    related = []
    if r.get("corr") is not None:
        c = db.get_event(sid, r["corr"])
        if c:
            related.append({**fmt.format(c), "relation": "begins this operation"})
    nxt = db.query_events(sid, after_seq=seq, limit=1, tids=[r["tid"]])
    prv = db.query_events(sid, before_seq=seq, limit=1, tids=[r["tid"]], descending=True)
    site = None
    row = session_row(db, sid)
    if r.get("caller") and row.get("executable"):
        bias = (snap.get("process") or {}).get("load_bias", 0)
        site = Symbolizer(row["executable"], bias).sites([r["caller"]]).get(r["caller"])
    return {"event": ev, "related": related, "site": site,
            "thread_prev": fmt.format(prv[0]) if prv else None, "thread_next": fmt.format(nxt[0]) if nxt else None}


@router.get("/sessions/{sid}/replay")
def session_replay(sid: str, seq: int = Query(..., ge=-1), db: Database = Depends(deps.db),
                   mgr: SessionManager = Depends(deps.manager),
                   cache: ReplayCache = Depends(deps.replay_cache)) -> dict:
    row = session_row(db, sid)
    live = mgr.live.get(sid)
    final = live is None or live.done.is_set()
    if not final:
        live.flush_rows()
        db.flush(timeout=5)
    idx = cache.get(sid, mgr.config, row.get("executable"), final)
    snap = idx.state_at(seq)
    fmt = EventFormatter(sid, snap)
    window = db.query_events(sid, after_seq=max(seq - 6, -1), before_seq=seq + 7, limit=13)
    snap["window"] = [fmt.format(r) for r in window]
    return snap


@router.get("/sessions/{sid}/timeline")
def session_timeline(sid: str, max_segments: int = Query(600, ge=50, le=5000), db: Database = Depends(deps.db),
                     mgr: SessionManager = Depends(deps.manager)) -> dict:
    snap, live = session_snapshot(mgr, db, check_sid(sid))
    if live is not None:
        live.flush_rows()
        db.flush(timeout=5)
    return build_timeline(db, sid, snap, max_segments)


@router.get("/sessions/{sid}/export")
def export_session(sid: str, format: str = Query("ndjson", pattern="^(json|ndjson|csv|raw|report)$"),
                   db: Database = Depends(deps.db), mgr: SessionManager = Depends(deps.manager)):
    snap, live = session_snapshot(mgr, db, check_sid(sid))
    row = session_row(db, sid)
    if format == "report":
        report = {"threadlens_report": 1, "session": row_public(row), "verdict": snap.get("verdict"),
                  "coverage": snap.get("coverage"), "stats": snap.get("stats"), "findings": snap.get("findings"),
                  "threads": snap.get("threads"), "locks": snap.get("locks"), "wait_for": snap.get("wait_for"),
                  "lock_order": snap.get("lock_order"),
                  "note": "Event replay reconstructs analysis state; it does not re-execute the program."}
        return JSONResponse(report, headers={"Content-Disposition": f'attachment; filename="threadlens-{sid}-report.json"'})
    fmt = EventFormatter(sid, snap)

    def gen() -> Iterator[str]:
        if format == "json":
            yield "["
            first = True
            for r in db.iter_events(sid):
                yield ("" if first else ",\n") + json.dumps(fmt.format(r))
                first = False
            yield "]\n"
        elif format == "ndjson":
            for r in db.iter_events(sid):
                yield json.dumps(fmt.format(r)) + "\n"
        elif format == "raw":
            metas = db.meta_events(sid)
            mi = 0

            def meta_line(m: dict) -> str:
                return json.dumps({"seq": m["seq"] or 0, "ts": m["ts"] or 0, "tid": 0, "type": TYPE_NAMES[m["type"]],
                                   "flags": ["oob"], "obj": m["obj"] or 0, "aux": m["aux"] or 0,
                                   "result": m["result"] or 0, "extra": m["extra"] or 0}) + "\n"
            while mi < len(metas) and metas[mi]["after_seq"] < 0:
                yield meta_line(metas[mi])
                mi += 1
            for r in db.iter_events(sid):
                flags = [n for n, bit in (("timed", 1), ("try", 2), ("oob", 4), ("inferred", 8), ("late", 16),
                                          ("serial", 32)) if r["flags"] & bit]
                d = {"seq": r["seq"], "ts": r["ts"], "tid": r["tid"], "type": TYPE_NAMES[r["type"]], "flags": flags,
                     "obj": r["obj"] or 0, "aux": r["aux"] or 0, "caller": r["caller"] or 0,
                     "result": r["result"] or 0, "extra": r["extra"] or 0}
                if r.get("text") is not None:
                    d["text"] = r["text"]
                yield json.dumps(d) + "\n"
                while mi < len(metas) and metas[mi]["after_seq"] <= r["seq"]:
                    yield meta_line(metas[mi])
                    mi += 1
        else:  # csv
            cols = ["seq", "rel_ns", "ts", "tid", "thread", "thread_label", "type", "lock", "lock_label", "obj",
                    "result_name", "flags", "correlation", "caller", "description"]
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            yield buf.getvalue()
            for r in db.iter_events(sid):
                e = fmt.format(r)
                buf.seek(0)
                buf.truncate()
                w.writerow([("|".join(e[c]) if c == "flags" else e.get(c)) for c in cols])
                yield buf.getvalue()

    media = {"json": "application/json", "ndjson": "application/x-ndjson", "raw": "application/x-ndjson",
             "csv": "text/csv"}[format]
    ext = {"json": "json", "ndjson": "ndjson", "raw": "trace.ndjson", "csv": "csv"}[format]
    return StreamingResponse(gen(), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="threadlens-{sid}.{ext}"'})


# ------------------------------------------------------------- comparisons

@router.get("/compare")
def compare(a: str, b: str, db: Database = Depends(deps.db)) -> dict:
    ra, rb = session_row(db, a), session_row(db, b)
    if not ra.get("summary") or not rb.get("summary"):
        raise HTTPException(409, "both sessions must be finished")
    return compare_sessions(ra, rb)


@router.get("/analysis/lock-order")
def cross_run_lock_order(sessions: str = Query(..., max_length=2000), db: Database = Depends(deps.db)) -> dict:
    ids = [check_sid(x.strip()) for x in sessions.split(",") if x.strip()]
    if not 1 <= len(ids) <= 50:
        raise HTTPException(422, "give between 1 and 50 session ids")
    rows = []
    for sid in ids:
        r = session_row(db, sid)
        if not r.get("summary"):
            raise HTTPException(409, f"session {sid} is not finished")
        rows.append(r)
    return merge_lock_order(rows)


@router.get("/event-types")
def event_types() -> list[dict]:
    return [{"id": int(t), "name": t.name} for t in EventType]
