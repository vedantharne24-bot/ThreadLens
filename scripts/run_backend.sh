#!/usr/bin/env bash
# Start the ThreadLens backend (Linux / WSL2). Binds to localhost only.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${THREADLENS_VENV:-$HOME/.threadlens/venv}"
HOST="${THREADLENS_HOST:-127.0.0.1}"
PORT="${THREADLENS_PORT:-8000}"
if [ ! -f "$ROOT/build/lib/libthreadlens.so" ]; then
    echo "collector not built — run scripts/setup_linux.sh first" >&2
    exit 1
fi
cd "$ROOT/backend"
exec "$VENV/bin/python" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
