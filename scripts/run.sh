#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — Run Script
#
# Starts the DEMASCAS stack:
#   1. Your plugged-in local LLM (checked; MLX is started if configured to)
#   2. MLX vision server      (localhost:8082)  — optional, manual
#   3. Python tool micro-service  (localhost:5001)
#   4. C++ bare-metal daemon      (audio → whisper.cpp → LLM → TTS)
#
# The C++ daemon runs in the foreground; Ctrl-C stops all processes.
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
VLM_SERVER="scripts/start_vlm_server.py"
VENV_DIR="$(cd "$PROJECT_ROOT/.." && pwd)/venv"

# DEMASCAS ships without an LLM — the user's plugged-in model is read from
# ~/.demascas/config.json (set it up with: python main.py setup).

# ── Track PIDs for cleanup ──────────────────────────────────────────

PIDS=()

cleanup() {
    echo ""
    echo "[*] Stopping all DEMASCAS processes..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    echo "[*] Done."
}
trap cleanup EXIT INT TERM

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

# ── Load config (API keys, etc.) ────────────────────────────────────
# Config file: ~/.demascas/config.json
# If it contains google_api_key / google_cse_id, smart_search uses Google.
# Otherwise smart_search falls back to DuckDuckGo (always works, no key).

CONFIG_FILE="$HOME/.demascas/config.json"
if [ -f "$CONFIG_FILE" ]; then
    echo "[*] Loading config from $CONFIG_FILE"
    # Read each key separately (no eval — values are never executed as shell).
    _cfg_get() {
        python3 - "$CONFIG_FILE" "$1" <<'PY'
import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2], "")
    sys.stdout.write(v if isinstance(v, str) else "")
except Exception:
    pass
PY
    }
    _gkey="$(_cfg_get google_api_key)"
    _gcse="$(_cfg_get google_cse_id)"
    [ -n "$_gkey" ] && export GOOGLE_API_KEY="$_gkey"
    [ -n "$_gcse" ] && export GOOGLE_CSE_ID="$_gcse"
    unset _gkey _gcse
else
    echo "[*] No config at $CONFIG_FILE — smart_search will use DuckDuckGo"
fi

# ── 1. Check the plugged-in LLM ─────────────────────────────────────
# llm-check exit codes: 0 reachable · 1 not reachable · 2 not configured ·
# 3 MLX with autostart (prints "<model>\n<port>" — MLX has no background
#   service, so DEMASCAS starts it).

echo "[*] Checking your local LLM..."
set +e
LLM_CHECK_OUT="$(python main.py llm-check)"
LLM_STATUS=$?
set -e

case "$LLM_STATUS" in
    0)
        echo "[✓] LLM runtime reachable."
        ;;
    2)
        echo "[!] No language model is set up yet. DEMASCAS will run shortcuts and"
        echo "    non-LLM actions only. Plug one in with:  python main.py setup"
        ;;
    3)
        MLX_MODEL="$(printf '%s\n' "$LLM_CHECK_OUT" | sed -n 1p)"
        MLX_PORT="$(printf '%s\n' "$LLM_CHECK_OUT" | sed -n 2p)"
        if lsof -nP -iTCP:"${MLX_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "❌  Port ${MLX_PORT} is in use by something that isn't answering as an LLM."
            exit 1
        fi
        echo "[*] Starting MLX server: ${MLX_MODEL} on port ${MLX_PORT}..."
        python -m mlx_lm server --model "$MLX_MODEL" --port "$MLX_PORT" &
        TEXT_PID=$!
        PIDS+=($TEXT_PID)
        # First run downloads the model — wait up to MLX_STARTUP_TIMEOUT s.
        MLX_STARTUP_TIMEOUT="${MLX_STARTUP_TIMEOUT:-300}"
        for i in $(seq 1 "$MLX_STARTUP_TIMEOUT"); do
            if curl -s "http://127.0.0.1:${MLX_PORT}/v1/models" > /dev/null 2>&1; then
                echo "[✓] MLX server ready."
                break
            fi
            if ! kill -0 "$TEXT_PID" 2>/dev/null; then
                echo "❌  MLX server exited during startup (see output above)."
                exit 1
            fi
            if [ "$i" -eq "$MLX_STARTUP_TIMEOUT" ]; then
                echo "❌  MLX server not ready after ${MLX_STARTUP_TIMEOUT}s."
                exit 1
            fi
            if [ $((i % 15)) -eq 0 ]; then echo "    ...still loading (${i}s)"; fi
            sleep 1
        done
        ;;
    *)
        echo "[!] Your configured LLM runtime isn't reachable. Start it (Ollama app,"
        echo "    LM Studio server, llama-server, ...) — details: python main.py doctor"
        ;;
esac

# ── 2. MLX vision server — LAZY LOAD (disabled by default) ──────────
# The VLM uses ~1.7GB RAM.  On 8GB M1, running it alongside the text
# model causes RAM swap and 13-second inference latency.
# To enable: uncomment the block below, or start manually:
#   python scripts/start_vlm_server.py &
# The daemon detects VLM_ENDPOINT_FILE at runtime and uses vision
# tools only when available.

# echo "[*] Starting MLX vision server (localhost:${MLX_VL_PORT})..."
# python "$VLM_SERVER" &
# VLM_PID=$!
# PIDS+=($VLM_PID)
# echo "    PID: $VLM_PID"
#
# echo "[*] Waiting for MLX vision server (optional, max 8s)..."
# for i in $(seq 1 8); do
#     if curl -s "http://127.0.0.1:${MLX_VL_PORT}/v1/models" > /dev/null 2>&1; then
#         echo "[✓] MLX vision server ready."
#         break
#     fi
#     if [ "$i" -eq 8 ]; then
#         echo "[⚠] MLX vision server not ready after 8s — continuing without VLM."
#     fi
#     sleep 1
# done

echo "[*] VLM server disabled (lazy-load mode for 8GB RAM). Start manually if needed."

# (Vision tools read ~/.demascas/vlm_endpoint.txt directly when the VLM
#  server is running; nothing needs to be exported here.)

# ── 3. Start Python tool server ─────────────────────────────────────

echo "[*] Starting Python tool server (localhost:5001)..."
TOOL_PID=""
if curl -s "http://127.0.0.1:5001/health" | grep -q '"status"' 2>/dev/null; then
    # e.g. the Docker container or a manual `python main.py` — reuse it
    # instead of launching a second copy that would fail to bind :5001.
    echo "[✓] Reusing the tool server already running on port 5001."
elif lsof -nP -iTCP:5001 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "❌  Port 5001 is in use by another program. Free it and retry."
    exit 1
else
    python "$TOOL_SERVER" &
    TOOL_PID=$!
    PIDS+=($TOOL_PID)
    echo "    PID: $TOOL_PID"

    # Wait for tool server /health endpoint (first start imports ChromaDB)
    echo "[*] Waiting for tool server..."
    for i in $(seq 1 60); do
        if curl -s "http://127.0.0.1:5001/health" | grep -q '"status"' 2>/dev/null; then
            echo "[✓] Tool server ready."
            break
        fi
        if ! kill -0 "$TOOL_PID" 2>/dev/null; then
            echo "❌  Tool server exited during startup (see output above)."
            exit 1
        fi
        if [ "$i" -eq 60 ]; then
            echo "❌  Tool server failed to start."
            exit 1
        fi
        sleep 1
    done
fi

# ── 4. Start C++ daemon in foreground ───────────────────────────────

echo "[*] Starting C++ daemon..."
echo ""
cd daemon

# sherpa-onnx shared libraries
export DYLD_LIBRARY_PATH="${PWD}/models/sherpa-onnx/lib:${DYLD_LIBRARY_PATH:-}"

./build/demascas_daemon
