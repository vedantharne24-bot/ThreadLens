"""Public, validated event schema (Pydantic).

``EventOut`` is what the API and WebSocket return for every event.
``EventIn`` validates externally supplied events (NDJSON import), so traces
produced or edited outside the collector cannot inject malformed records.
The binary collector stream is validated structurally by app.models.wire.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.wire import EventType, Flag

EVENT_TYPE_NAMES = tuple(t.name for t in EventType)
FLAG_NAMES = tuple(f.name.lower() for f in Flag)
U64_MAX = (1 << 64) - 1


class EventOut(BaseModel):
    """One observed synchronization event, enriched with names."""

    session_id: str
    event_id: str = Field(description="'<session_id>:<seq>' — unique within the database")
    seq: int = Field(ge=0, description="per-process total order assigned by the collector")
    ts: int = Field(ge=0, description="CLOCK_MONOTONIC nanoseconds in the target process")
    rel_ns: int = Field(description="ts relative to the first event of the session")
    tid: int = Field(ge=0, description="kernel thread id")
    thread: int | None = Field(None, description="ThreadLens thread index (T0 = main)")
    thread_label: str | None = None
    type: str
    lock: int | None = Field(None, description="session-local lock id (L1, L2, ...)")
    lock_label: str | None = None
    obj: str | None = Field(None, description="primary object address (hex)")
    target_thread: int | None = None
    correlation: int | None = Field(None, description="seq of the paired begin event (request, wait begin, join begin)")
    result: int | None = None
    result_name: str | None = None
    flags: list[str] = []
    caller: str | None = Field(None, description="return address of the intercepted call (hex)")
    text: str | None = None
    metadata: dict[str, Any] = {}
    description: str


class EventIn(BaseModel):
    """Externally supplied event (import). Strict: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0, le=U64_MAX)
    ts: int = Field(ge=0, le=U64_MAX)
    tid: int = Field(ge=0, le=0xFFFFFFFF)
    type: str
    flags: list[str] = []
    obj: int = Field(0, ge=0, le=U64_MAX)
    aux: int = Field(0, ge=0, le=U64_MAX)
    caller: int = Field(0, ge=0, le=U64_MAX)
    result: int = Field(0, ge=-(1 << 31), le=(1 << 31) - 1)
    extra: int = Field(0, ge=0, le=0xFFFFFFFF)
    text: str | None = Field(None, max_length=23)
    pid: int = Field(0, ge=0, le=0xFFFFFFFF)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in EVENT_TYPE_NAMES:
            raise ValueError(f"unknown event type {v!r}")
        return v

    @field_validator("flags")
    @classmethod
    def _known_flags(cls, v: list[str]) -> list[str]:
        bad = [f for f in v if f not in FLAG_NAMES]
        if bad:
            raise ValueError(f"unknown flags {bad}")
        return v

    def flag_bits(self) -> int:
        bits = 0
        for f in self.flags:
            bits |= Flag[f.upper()]
        return bits


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str | None = Field(None, max_length=64)
    executable: str | None = Field(None, max_length=1024, description="path inside an allowed directory")
    args: list[str] | None = Field(None, max_length=32)
    timeout_s: float | None = Field(None, gt=0, le=600)
    label: str | None = Field(None, max_length=120)
    kill_on_deadlock: bool = True
    ring_capacity: int | None = Field(None, ge=1024, le=1 << 22)
    block_us: int | None = Field(None, ge=0, le=1_000_000)


class AnalysisSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contention_min_acquisitions: int = Field(20, ge=1, le=1_000_000)
    contention_ratio: float = Field(0.5, ge=0.0, le=1.0)
    contention_total_wait_ms: float = Field(100.0, ge=0.0)
    long_wait_ms: float = Field(500.0, ge=1.0)
    stall_ms: float = Field(1000.0, ge=50.0)
    confirm_requires_heartbeat: bool = True


class RetentionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_sessions: int = Field(200, ge=1, le=100_000)


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: AnalysisSettings = AnalysisSettings()
    retention: RetentionSettings = RetentionSettings()


Severity = Literal["critical", "warning", "info"]
