"""ThreadLens FastAPI application.

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes, ws
from app.core.config import Settings, settings as default_settings
from app.services.replay import ReplayCache
from app.services.scenarios import Catalog
from app.services.session import SessionManager
from app.storage.db import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        app.state.settings = s
        app.state.db = Database(s.db_path)
        app.state.manager = SessionManager(s, app.state.db)
        app.state.catalog = Catalog(s)
        app.state.replay = ReplayCache(app.state.db)
        yield
        await app.state.manager.shutdown()
        app.state.db.close()

    app = FastAPI(title="ThreadLens", version="1.0.0", lifespan=lifespan,
                  description="Live Linux concurrency debugger and deadlock visualizer")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith("/api") and not request.url.path.startswith("/docs"):
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:; "
                "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return resp

    app.include_router(routes.router)
    app.include_router(ws.router)

    dist = s.frontend_dist
    if (dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith(("api/", "ws/")):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = (dist / path).resolve()
            if path and dist.resolve() in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
