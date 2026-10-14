"""Process supervision safeguards and collector-level trace properties."""

import asyncio
import os
import subprocess
import sys

import pytest

from app.core.config import Settings
from app.models.wire import EventType as E, iter_trace
from app.services.process_manager import LaunchError, ManagedProcess, validate_args, validate_executable
from conftest import BIN, BUILD, COLLECTOR, needs_build

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux only")


def test_rejects_executables_outside_allowed_dirs(tmp_path):
    s = Settings(build_dir=tmp_path / "build")
    with pytest.raises(LaunchError):
        validate_executable("/bin/ls", s)
    with pytest.raises(LaunchError):
        validate_executable(tmp_path / "missing", s)


def test_rejects_scripts_and_non_executables(tmp_path):
    bindir = tmp_path / "build" / "bin"
    bindir.mkdir(parents=True)
    script = bindir / "evil.sh"
    script.write_text("#!/bin/sh\necho pwned\n")
    script.chmod(0o755)
    s = Settings(build_dir=tmp_path / "build")
    with pytest.raises(LaunchError, match="ELF"):
        validate_executable(script, s)
    data = bindir / "data"
    data.write_bytes(b"\x7fELF")
    with pytest.raises(LaunchError, match="executable"):
        validate_executable(data, s)


def test_symlink_escape_is_rejected(tmp_path):
    bindir = tmp_path / "build" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "ls").symlink_to("/bin/ls")
    with pytest.raises(LaunchError, match="outside"):
        validate_executable(bindir / "ls", Settings(build_dir=tmp_path / "build"))


def test_argument_validation():
    s = Settings()
    assert validate_args(["8", "--fixed"], s) == ["8", "--fixed"]
    for bad in (["a\nb"], ["x" * 300], ["a"] * 40, ["\x00"]):
        with pytest.raises(LaunchError):
            validate_args(bad, s)


@needs_build
def test_timeout_kills_whole_process_group(tmp_path):
    s = Settings(data_dir=tmp_path)

    async def go():
        p = ManagedProcess(BIN / "lost_wakeup", [], tmp_path / "cwd", {"PATH": "/usr/bin:/bin"}, s, cpu_seconds=10)
        pid = await p.start()
        code, sig, timed_out = await p.wait(timeout=0.5)
        return pid, code, sig, timed_out, p.killed_by

    pid, code, sig, timed_out, reason = asyncio.run(go())
    assert timed_out and sig == 9 and reason == "timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def run_traced(tmp_path, binary, *args, env_extra=None):
    trace = tmp_path / "t.tlt"
    env = {"PATH": "/usr/bin:/bin", "LD_PRELOAD": str(COLLECTOR), "THREADLENS_TRACE": str(trace),
           "SCENARIO_WATCHDOG": "3", **(env_extra or {})}
    proc = subprocess.run([str(BIN / binary), *args], env=env, capture_output=True, text=True, timeout=30)
    return proc, list(iter_trace(trace.read_bytes()))


@needs_build
def test_collector_sequence_and_pairing(tmp_path):
    proc, evs = run_traced(tmp_path, "producer_consumer", "40")
    assert proc.returncode == 0
    assert evs[0].type == E.HELLO
    inband = [e for e in evs if not e.oob]
    assert [e.seq for e in inband] == list(range(len(inband)))
    begins = sum(1 for e in inband if e.type == E.COND_WAIT_BEGIN)
    ends = sum(1 for e in inband if e.type == E.COND_WAIT_END)
    assert begins == ends > 0
    req = sum(1 for e in inband if e.type == E.LOCK_REQUEST)
    acq = sum(1 for e in inband if e.type == E.LOCK_ACQUIRED and not e.flags & 2)
    assert req == acq
    assert any(e.type == E.PROCESS_EXIT for e in inband)


@needs_build
def test_release_always_precedes_next_acquire(tmp_path):
    """The ordering rule: per mutex, ownership alternates in sequence order."""
    _, evs = run_traced(tmp_path, "contention", "6", "10", "200")
    owner = {}
    for e in evs:
        if e.oob:
            continue
        if e.type == E.LOCK_ACQUIRED:
            assert owner.get(e.obj) is None, f"seq {e.seq}: acquired while held"
            owner[e.obj] = e.tid
        elif e.type == E.LOCK_RELEASE:
            assert owner.get(e.obj) == e.tid
            owner[e.obj] = None


@needs_build
def test_deadlocked_process_still_delivers_events(tmp_path):
    proc, evs = run_traced(tmp_path, "bank_deadlock", env_extra={"SCENARIO_WATCHDOG": "1"})
    assert proc.returncode == -14  # SIGALRM watchdog
    requests = [e for e in evs if e.type == E.LOCK_REQUEST]
    assert len(requests) >= 4
    assert any(e.type == E.HEARTBEAT for e in evs)


@needs_build
def test_cpp_std_mutex_and_thread_are_intercepted(tmp_path):
    proc, evs = run_traced(tmp_path, "bank_scoped_cpp")
    assert proc.returncode == 0
    types = {e.type for e in evs}
    assert {E.THREAD_START, E.LOCK_ACQUIRED, E.JOIN_BEGIN} <= types
    assert any(e.type == E.LOCK_ACQUIRED and e.flags & 2 for e in evs)  # std::lock uses try_lock


@needs_build
def test_passthrough_without_output_env():
    env = {"PATH": "/usr/bin:/bin", "LD_PRELOAD": str(COLLECTOR)}
    out = subprocess.run([str(BUILD / "bin" / "healthy")], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "TLRESULT" in out.stdout
