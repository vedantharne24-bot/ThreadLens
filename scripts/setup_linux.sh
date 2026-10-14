#!/usr/bin/env bash
# One-time setup on Linux (native Ubuntu or WSL2 Ubuntu):
#   - creates a Python virtual environment for the backend
#   - installs backend requirements (and CMake from PyPI if the system has none)
#   - configures and builds the collector and all scenarios
#   - runs the collector's C-level tests (ctest)
#
# Preferred system packages (optional, need sudo):
#   sudo apt install build-essential cmake python3-venv python3-pip valgrind
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${THREADLENS_VENV:-$HOME/.threadlens/venv}"
BUILD="${THREADLENS_BUILD_DIR:-$ROOT/build}"

echo "==> ThreadLens root: $ROOT"
echo "==> virtualenv:      $VENV"

if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$(dirname "$VENV")"
    if ! python3 -m venv "$VENV" 2>/dev/null; then
        # Ubuntu ships ensurepip in a separate package (python3-venv). Without
        # sudo we create the venv without pip and bootstrap pip from PyPA.
        echo "==> python3-venv not installed; bootstrapping pip with get-pip.py"
        rm -rf "$VENV"
        python3 -m venv --without-pip "$VENV"
        tmp="$(mktemp -d)"
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$tmp/get-pip.py"
        "$VENV/bin/python" "$tmp/get-pip.py" --quiet
        rm -rf "$tmp"
    fi
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/backend/requirements.txt"

CMAKE="$(command -v cmake || true)"
[ -z "$CMAKE" ] && CMAKE="$VENV/bin/cmake"
echo "==> cmake: $CMAKE ($("$CMAKE" --version | head -1))"

"$CMAKE" -S "$ROOT" -B "$BUILD" -DCMAKE_BUILD_TYPE=RelWithDebInfo
"$CMAKE" --build "$BUILD" -j"$(nproc)"

echo "==> running collector tests (ctest)"
(cd "$BUILD" && "$(dirname "$CMAKE")/ctest" --output-on-failure)

echo
echo "Setup complete."
echo "  collector : $BUILD/lib/libthreadlens.so"
echo "  scenarios : $BUILD/bin/"
echo "  start backend: scripts/run_backend.sh"
