"""Address symbolization with binutils (nm, addr2line).

Lock addresses are mapped to global data symbols ("lock_a", "accounts+0x30")
and caller return addresses to "function (file:line)". Executables are PIE
on modern Ubuntu, so every runtime address is first converted to a link-time
address by subtracting the load bias reported by the collector's HELLO record.

Only argument lists are passed to subprocess (never a shell), the executable
path has already been validated by the caller, and every call has a timeout.
"""

from __future__ import annotations

import bisect
import os
import shutil
import subprocess
import threading

DATA_TYPES = set("bBdDrRgGsSvV")


class Symbolizer:
    def __init__(self, exe_path: str | None, load_bias: int = 0) -> None:
        self.exe = exe_path if exe_path and os.path.isfile(exe_path) else None
        self.bias = load_bias
        self._lock = threading.Lock()
        self._data: list[tuple[int, int, str]] | None = None
        self._starts: list[int] = []
        self._text_lo = 0
        self._text_hi = 0
        self._sites: dict[int, dict | None] = {}
        self.nm = shutil.which("nm")
        self.addr2line = shutil.which("addr2line")

    def set_bias(self, bias: int) -> None:
        self.bias = bias

    # ------------------------------------------------------------------ data
    def _load(self) -> None:
        if self._data is not None:
            return
        self._data = []
        if not self.exe or not self.nm:
            return
        try:
            out = subprocess.run([self.nm, "-S", "-C", "--defined-only", self.exe], capture_output=True,
                                 text=True, timeout=15, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return
        text_lo, text_hi = None, 0
        for line in out.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) == 4:
                addr_s, size_s, typ, name = parts
                try:
                    addr, size = int(addr_s, 16), int(size_s, 16)
                except ValueError:
                    continue
                if typ in DATA_TYPES and size > 0:
                    self._data.append((addr, size, name))
                elif typ in "tTwW":
                    text_lo = addr if text_lo is None else min(text_lo, addr)
                    text_hi = max(text_hi, addr + size)
        self._data.sort()
        self._starts = [d[0] for d in self._data]
        self._text_lo, self._text_hi = text_lo or 0, text_hi

    def data_symbol(self, addr: int) -> str | None:
        with self._lock:
            self._load()
        if not self._data:
            return None
        rel = addr - self.bias
        i = bisect.bisect_right(self._starts, rel) - 1
        if i < 0:
            return None
        start, size, name = self._data[i]
        if start <= rel < start + size:
            return name if rel == start else f"{name}+{rel - start:#x}"
        return None

    # ------------------------------------------------------------------ code
    def sites(self, addrs: list[int]) -> dict[int, dict | None]:
        """Map return addresses to {'function', 'file', 'line'} (call site)."""
        with self._lock:
            self._load()
            todo = sorted({a for a in addrs if a and a not in self._sites})
            rels = {}
            for a in todo:
                rel = a - self.bias
                if self.exe and self.addr2line and self._text_lo <= rel < self._text_hi:
                    rels[a] = rel - 1  # return address -> address inside the call instruction
                else:
                    self._sites[a] = None
            if rels:
                self._run_addr2line(rels)
            return {a: self._sites.get(a) for a in addrs if a}

    def _run_addr2line(self, rels: dict[int, int]) -> None:
        order = list(rels)
        try:
            out = subprocess.run([self.addr2line, "-f", "-C", "-e", self.exe] + [f"{rels[a]:#x}" for a in order],
                                 capture_output=True, text=True, timeout=15, check=False).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            for a in order:
                self._sites[a] = None
            return
        for i, a in enumerate(order):
            func = out[2 * i] if 2 * i < len(out) else "??"
            loc = out[2 * i + 1] if 2 * i + 1 < len(out) else "??:0"
            path, _, line = loc.rpartition(":")
            line = line.split(" ")[0]
            self._sites[a] = {
                "function": None if func == "??" else func,
                "file": None if path in ("??", "") else os.path.basename(path),
                "path": None if path in ("??", "") else path,
                "line": int(line) if line.isdigit() and int(line) > 0 else None,
            }

    def site_label(self, addr: int) -> str | None:
        s = self.sites([addr]).get(addr)
        if not s:
            return None
        where = f"{s['file']}:{s['line']}" if s.get("file") and s.get("line") else (s.get("file") or "")
        fn = s.get("function") or "?"
        return f"{fn} ({where})" if where else fn
