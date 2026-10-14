from __future__ import annotations

import json
import re

from fastapi import HTTPException, Request

from app.core.config import Settings
from app.services.replay import ReplayCache
from app.services.scenarios import Catalog
from app.services.session import LiveSession, SessionManager
from app.storage.db import Database

SESSION_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def settings(request: Request) -> Settings:
    return request.app.state.settings


def db(request: Request) -> Database:
    return request.app.state.db


def manager(request: Request) -> SessionManager:
    return request.app.state.manager


def catalog(request: Request) -> Catalog:
    return request.app.state.catalog


def replay_cache(request: Request) -> ReplayCache:
    return request.app.state.replay


def check_sid(sid: str) -> str:
    if not SESSION_ID_RE.match(sid):
        raise HTTPException(400, "malformed session id")
    return sid


def session_row(database: Database, sid: str) -> dict:
    row = database.get_session(check_sid(sid))
    if row is None:
        raise HTTPException(404, f"session {sid} not found")
    return row


def session_snapshot(mgr: SessionManager, database: Database, sid: str) -> tuple[dict, LiveSession | None]:
    """Current analysis state: live analyzer if running, else the stored final snapshot."""
    live = mgr.live.get(sid)
    if live is not None and not live.done.is_set():
        return live.final_snapshot(), live
    row = session_row(database, sid)
    if not row.get("summary"):
        raise HTTPException(409, "session has no analysis yet")
    return json.loads(row["summary"]), live


def row_public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in ("summary", "stdout", "stderr")}
    for k in ("args", "metrics", "result", "options"):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except json.JSONDecodeError:
                pass
    return out
