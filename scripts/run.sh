#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — Run Script
#
# Starts the full polyglot stack:
#   1. Python tool micro-service  (localhost:5001)
#   2. C++ bare-metal daemon      (audio → whisper.cpp → Ollama → TTS)
#
# The C++ daemon runs in the foreground; Ctrl-C stops both processes.
#
# Usage:
#   chmod +x scripts/run.sh
#   ./scripts/run.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DAEMON_BIN="daemon/build/demascas_daemon"
TOOL_SERVER="server/tool_server.py"
VENV_DIR="$(cd "$PROJECT_ROOT/.." && pwd)/venv"

# ── Preflight checks ────────────────────────────────────────────────

if [ ! -f "$DAEMON_BIN" ]; then
    echo "❌  Daemon not built.  Run first:"
    echo "    ./scripts/build.sh"
    exit 1
fi

if [ ! -f "$TOOL_SERVER" ]; then
    echo "❌  Tool server not found at $TOOL_SERVER"
    exit 1
fi

# ── Activate Python venv ────────────────────────────────────────────

if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
fi

# ── Start Python tool server in background ───────────────────────────

echo "[*] Starting Python tool server (localhost:5001)..."
python "$TOOL_SERVER" &
TOOL_PID=$!

# Ensure we clean up the Python server on exit, Ctrl-C, or error
cleanup() {
    echo ""
    echo "[*] Stopping tool server (PID $TOOL_PID)..."
    kill "$TOOL_PID" 2>/dev/null || true
    wait "$TOOL_PID" 2>/dev/null || true
    echo "[*] Done."
}
trap cleanup EXIT INT TERM

# Give Flask a moment to bind the port
sleep 1

# ── Start C++ daemon in foreground ───────────────────────────────────

echo "[*] Starting C++ daemon..."
echo ""
cd daemon
./build/demascas_daemon
