"""WebSocket streaming of live session state."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import SESSION_ID_RE, row_public

router = APIRouter()


@router.websocket("/ws/sessions/{sid}")
async def session_stream(ws: WebSocket, sid: str) -> None:
    await ws.accept()
    if not SESSION_ID_RE.match(sid):
        await ws.close(code=4400)
        return
    mgr = ws.app.state.manager
    live = mgr.live.get(sid)
    if live is None or live.done.is_set():
        row = ws.app.state.db.get_session(sid)
        if row is None:
            await ws.send_json({"type": "error", "error": "session not found"})
            await ws.close(code=4404)
            return
        await ws.send_json({"type": "final", "session": row_public(row),
                            "snapshot": json.loads(row["summary"]) if row.get("summary") else None})
        await ws.close()
        return

    sub = live.subscribe()

    async def receiver() -> None:
        # the client may send {"type":"ping"}; anything else is ignored
        while True:
            msg = await ws.receive_text()
            if len(msg) < 200 and '"ping"' in msg:
                await ws.send_json({"type": "pong"})

    recv_task = asyncio.create_task(receiver())
    try:
        while True:
            get_task = asyncio.create_task(sub.queue.get())
            done, _ = await asyncio.wait({get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                get_task.cancel()
                break
            msg = get_task.result()
            if sub.dropped:
                msg = {**msg, "dropped_updates": sub.dropped}
            await ws.send_json(msg)
            if msg.get("type") == "end":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        recv_task.cancel()
        live.unsubscribe(sub)
        try:
            await ws.close()
        except RuntimeError:
            pass
