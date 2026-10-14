"""Controlled execution of monitored programs.

Safety measures (see docs/security.md):
  * the executable must be a regular ELF file inside the scenario build
    directory or an explicitly allowed directory (THREADLENS_ALLOWED_DIRS);
  * arguments are a validated list passed directly to execve — no shell;
  * a minimal environment and a private working directory are used;
  * resource limits (address space, processes, file size, core dumps, CPU
    time) are applied with prlimit right after the process is created;
  * the program runs in its own session / process group so the supervisor
    can kill it and every thread/child it created (killpg);
  * stdout/stderr are captured into bounded buffers.
"""

from __future__ import annotations

import asyncio
import os
import resource
import signal
import stat
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings


class LaunchError(ValueError):
    pass


def validate_executable(path: str | Path, settings: Settings) -> Path:
    try:
        p = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LaunchError(f"executable not found: {path}") from exc
    roots = [settings.bin_dir.resolve()] + list(settings.allowed_dirs)
    if not any(p == r or r in p.parents for r in roots):
        raise LaunchError("executable is outside the allowed directories "
                          f"({', '.join(str(r) for r in roots)}); set THREADLENS_ALLOWED_DIRS to allow more")
    st = p.stat()
    if not stat.S_ISREG(st.st_mode):
        raise LaunchError("executable is not a regular file")
    if not os.access(p, os.X_OK):
        raise LaunchError("file is not executable")
    with open(p, "rb") as fh:
        if fh.read(4) != b"\x7fELF":
            raise LaunchError("only ELF executables can be monitored (scripts are rejected)")
    return p


def validate_args(args: list[str], settings: Settings) -> list[str]:
    if len(args) > settings.max_args:
        raise LaunchError(f"too many arguments (max {settings.max_args})")
    out = []
    for a in args:
        if not isinstance(a, str):
            raise LaunchError("arguments must be strings")
        if len(a) > settings.max_arg_len:
            raise LaunchError(f"argument longer than {settings.max_arg_len} characters")
        if "\x00" in a or any(ord(c) < 32 for c in a):
            raise LaunchError("arguments may not contain control characters")
        out.append(a)
    return out


@dataclass
class BoundedBuffer:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    truncated: int = 0

    def add(self, chunk: bytes) -> None:
        room = self.limit - len(self.data)
        if room > 0:
            self.data.extend(chunk[:room])
        if len(chunk) > max(room, 0):
            self.truncated += len(chunk) - max(room, 0)

    def text(self) -> str:
        s = self.data.decode("utf-8", "replace")
        if self.truncated:
            s += f"\n[... {self.truncated} bytes truncated by ThreadLens]"
        return s


class ManagedProcess:
    def __init__(self, executable: Path, args: list[str], cwd: Path, env: dict[str, str],
                 settings: Settings, cpu_seconds: int) -> None:
        self.executable = executable
        self.args = args
        self.cwd = cwd
        self.env = env
        self.settings = settings
        self.cpu_seconds = cpu_seconds
        self.proc: asyncio.subprocess.Process | None = None
        self.stdout = BoundedBuffer(settings.max_output_bytes)
        self.stderr = BoundedBuffer(settings.max_output_bytes)
        self._readers: list[asyncio.Task] = []
        self.killed_by: str | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def start(self) -> int:
        self.cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.proc = await asyncio.create_subprocess_exec(
            str(self.executable), *self.args,
            cwd=str(self.cwd), env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, close_fds=True)
        self._apply_limits(self.proc.pid)
        self._readers = [asyncio.create_task(self._pump(self.proc.stdout, self.stdout)),
                         asyncio.create_task(self._pump(self.proc.stderr, self.stderr))]
        return self.proc.pid

    def _apply_limits(self, pid: int) -> None:
        s = self.settings
        limits = [
            (resource.RLIMIT_AS, s.rlimit_as_bytes),
            (resource.RLIMIT_NPROC, s.rlimit_nproc),
            (resource.RLIMIT_FSIZE, s.rlimit_fsize_bytes),
            (resource.RLIMIT_CORE, 0),
            (resource.RLIMIT_CPU, max(1, self.cpu_seconds)),
        ]
        for res, value in limits:
            try:
                soft, hard = resource.prlimit(pid, res)
                value = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.prlimit(pid, res, (value, value if hard == resource.RLIM_INFINITY else hard))
            except (OSError, ValueError, ProcessLookupError):
                pass  # process may already have exited, or limit not permitted

    async def _pump(self, stream: asyncio.StreamReader | None, buf: BoundedBuffer) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            buf.add(chunk)

    async def wait(self, timeout: float | None) -> tuple[int | None, int | None, bool]:
        """Wait for exit. Returns (exit_code, signal, timed_out)."""
        assert self.proc is not None
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            self.kill("timeout")
            await self.proc.wait()
        for r in self._readers:
            try:
                await asyncio.wait_for(r, timeout=2)
            except asyncio.TimeoutError:
                r.cancel()
        rc = self.proc.returncode
        if rc is not None and rc < 0:
            return None, -rc, timed_out
        return rc, None, timed_out

    def kill(self, reason: str, sig: int = signal.SIGKILL) -> None:
        """Kill the whole process group. Individual threads of a deadlocked
        process cannot be unwound safely (their locks and stacks are in an
        inconsistent state), so the isolated test process is terminated."""
        if self.proc is None or self.proc.returncode is not None:
            return
        if self.killed_by is None:
            self.killed_by = reason
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                pass
