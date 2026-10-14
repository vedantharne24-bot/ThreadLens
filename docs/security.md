# Security and reliability

ThreadLens executes programs supplied by the user and receives data written by
them. Monitored programs are treated as **potentially unsafe**.

## Implemented controls

| Threat | Control | Where |
|---|---|---|
| Running arbitrary commands | Only ELF executables (magic `\x7fELF`), regular files, executable bit set, resolved path (symlinks followed) inside `build/bin` or `THREADLENS_ALLOWED_DIRS`; scripts are rejected | `process_manager.validate_executable` |
| Shell injection | `execve` through `asyncio.create_subprocess_exec` with an argument list — no shell is ever invoked; arguments ≤ 32, ≤ 256 chars, no control characters | `process_manager.validate_args` |
| Environment leakage | Minimal environment (`PATH`, `LANG`, `HOME`, collector variables) — the backend's environment is not inherited | `session.LiveSession.start` |
| Writing outside a sandbox directory | Private working directory per session (`~/.threadlens/sessions/<id>`, mode 0700) | `session.LiveSession.start` |
| Runaway programs | Wall-clock timeout (default 10 s, max 120 s); `RLIMIT_CPU`, `RLIMIT_AS` (2 GiB), `RLIMIT_NPROC` (512), `RLIMIT_FSIZE` (64 MiB), no core dumps (`prlimit`) | `process_manager.ManagedProcess` |
| Orphan threads / children | Target runs in its own session (`setsid`); termination kills the whole process group (`killpg`, SIGKILL); deadlocked programs are terminated ~1 s after confirmation | `process_manager.kill`, `session._schedule_kill` |
| Fork bombs inside targets | `RLIMIT_NPROC`; forked children are not monitored and cannot reuse the session socket | collector `pthread_atfork`, backend accepts one connection |
| Foreign processes injecting events | Per-session socket in a 0700 directory with mode 0600; random 62-bit token checked in the `HELLO` handshake; only the first connection is accepted | `session._on_connection` |
| Malformed collector data | Magic/version/type validation per record; sequence guard; anomalies counted and downgrade verdicts | `wire.StreamDecoder`, `ingest.SequenceGuard` |
| Malicious imports | Size limit (128 MiB); NDJSON validated with a strict Pydantic schema (unknown fields rejected, integer ranges checked) | `routes.import_session`, `models.events.EventIn` |
| API input abuse | Pydantic request models with `extra="forbid"`; constrained query parameters; session-id pattern check (prevents path tricks); bounded page sizes and scans | `api/routes.py`, `api/deps.py` |
| Resource exhaustion in the backend | Maximum concurrent sessions (4), stored events per session (2 M, then counted but not stored), captured output (64 KiB per stream), bounded WebSocket queues, bounded reorder window | `core/config.py` |
| Cross-site scripting | All program output, thread names and lock names are rendered as React text (escaped); the code viewer highlights by producing text nodes; no `dangerouslySetInnerHTML` | `frontend/src` |
| Clickjacking / sniffing / referrer leaks | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`; Content-Security-Policy restricting scripts, connections and objects to the same origin | `main.security_headers` |
| Network exposure | Backend binds to `127.0.0.1` by default; CORS allows only the local dev server | `scripts/run_backend.sh`, `core/config.py` |
| Reading files through the API | Source viewer only serves files listed in the scenario catalog and resolved inside the project root | `services/scenarios.Catalog.source` |
| Symbolizer abuse | `nm`/`addr2line` run without a shell, on the already-validated executable, with timeouts | `services/symbolizer.py` |
| Unrelated data collection | The collector records only intercepted pthread calls of the target process (addresses, TIDs, return codes, caller addresses, thread/lock names). No memory contents, files, environment or system-wide data | `collector.c` |

## Remaining risks

* The target runs **as the same user** as the backend. Resource limits and a
  private directory are not a security sandbox: a malicious program can read the
  user's files. Run untrusted code in a VM or container.
* The API has **no authentication**. It is meant for a single local user;
  binding to a non-loopback address would expose process execution to the
  network. Do not change `THREADLENS_HOST` on shared machines.
* `THREADLENS_ALLOWED_DIRS` widens what can be executed; keep it narrow.
* Captured stdout/stderr and imported traces are stored unencrypted in
  `~/.threadlens`.
* Caller addresses are symbolized with `addr2line`; extremely large binaries
  may take long (bounded by a 15 s timeout).

## Future improvements

* Execute targets in a separate user namespace / cgroup with seccomp filters
  (e.g. `bwrap` or `systemd-run --user --scope -p MemoryMax=…`).
* Authentication tokens for the API and WebSocket, and TLS for remote use.
* Per-session disk quotas and automatic retention by age.
* Signed trace files to detect tampering with exported evidence.
