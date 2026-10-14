"""Scenario catalog (scenarios/catalog.json) and source-code access."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.core.config import Settings


class ScenarioError(LookupError):
    pass


class Catalog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._data = json.loads(settings.catalog_path.read_text(encoding="utf-8"))
        self.by_id = {s["id"]: s for s in self._data["scenarios"]}

    def list(self) -> list[dict]:
        out = []
        for s in self._data["scenarios"]:
            binary = self.settings.bin_dir / s["binary"]
            out.append({**s, "built": binary.is_file(), "binary_path": str(binary)})
        return out

    def get(self, sid: str) -> dict:
        s = self.by_id.get(sid)
        if s is None:
            raise ScenarioError(f"unknown scenario {sid!r}")
        return {**s, "built": (self.settings.bin_dir / s["binary"]).is_file(),
                "binary_path": str(self.settings.bin_dir / s["binary"])}

    def source(self, sid: str) -> list[dict]:
        s = self.get(sid)
        root = self.settings.project_root.resolve()
        files = []
        for rel in s.get("sources", []):
            p = (root / rel).resolve()
            if root not in p.parents or not p.is_file():  # never read outside the project
                continue
            files.append({"path": rel, "name": p.name, "content": _read(str(p), p.stat().st_mtime)})
        return files


@lru_cache(maxsize=64)
def _read(path: str, mtime: float) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")
