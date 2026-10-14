"""Runtime configuration (environment variables with safe defaults)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser().resolve() if v else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    project_root: Path = PROJECT_ROOT
    build_dir: Path = field(default_factory=lambda: _env_path("THREADLENS_BUILD_DIR", PROJECT_ROOT / "build"))
    data_dir: Path = field(default_factory=lambda: _env_path("THREADLENS_DATA_DIR", Path.home() / ".threadlens"))
    socket_dir: Path = field(default_factory=lambda: _env_path(
        "THREADLENS_SOCKET_DIR", Path(f"/tmp/threadlens-{os.getuid() if hasattr(os, 'getuid') else 0}")))
    # extra directories from which arbitrary executables may be launched (colon separated)
    allowed_dirs: list[Path] = field(default_factory=lambda: [
        Path(p).expanduser().resolve() for p in os.environ.get("THREADLENS_ALLOWED_DIRS", "").split(":") if p])
    max_timeout_s: int = field(default_factory=lambda: _env_int("THREADLENS_MAX_TIMEOUT_S", 120))
    default_timeout_s: int = 10
    max_args: int = 32
    max_arg_len: int = 256
    max_concurrent_sessions: int = field(default_factory=lambda: _env_int("THREADLENS_MAX_SESSIONS", 4))
    max_stored_events: int = field(default_factory=lambda: _env_int("THREADLENS_MAX_STORED_EVENTS", 2_000_000))
    max_output_bytes: int = 64 * 1024        # captured stdout/stderr per stream
    max_import_bytes: int = 128 * 1024 * 1024
    rlimit_as_bytes: int = 2 * 1024 ** 3     # address space per monitored program
    rlimit_nproc: int = 512
    rlimit_fsize_bytes: int = 64 * 1024 ** 2
    ring_capacity: int = 1 << 16
    block_us: int = 100_000                  # bounded back-pressure: favour complete traces
    heartbeat_ms: int = 100
    deadlock_grace_s: float = 1.0            # keep a confirmed deadlock alive briefly, then kill
    broadcast_interval_s: float = 0.1
    cors_origins: list[str] = field(default_factory=lambda: [
        o for o in os.environ.get("THREADLENS_CORS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o])

    @property
    def collector_path(self) -> Path:
        return self.build_dir / "lib" / "libthreadlens.so"

    @property
    def bin_dir(self) -> Path:
        return self.build_dir / "bin"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "threadlens.db"

    @property
    def catalog_path(self) -> Path:
        return self.project_root / "scenarios" / "catalog.json"

    @property
    def frontend_dist(self) -> Path:
        return self.project_root / "frontend" / "dist"


settings = Settings()
