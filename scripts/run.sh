#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — Run Script
#
# Starts the full polyglot stack (4 processes):
#   1. LLM server             (localhost:11434) — Ollama (Linux) or MLX (macOS)
#   2. MLX vision server      (localhost:8082)  — Qwen2.5-VL-3B (optional)
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

# LLM configuration
OLLAMA_MODEL="gemma4:31b-cloud"
OLLAMA_PORT=11434
MLX_TEXT_MODEL="mlx-community/Qwen2.5-3B-Instruct-4bit"
MLX_TEXT_PORT=8081
MLX_VL_PORT=8082

# ── Track PIDs for cleanup ──────────────────────────────────────────

PIDS=()

cleanup() {
    echo ""
    echo "[*] Stopping all DEMASCAS processes..."
    for pid in "${PIDS[@]}"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    for pid in "${PIDS[@]}"; do
        if [ -n "$pid" ]; then
            wait "$pid" 2>/dev/null || true
        fi
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
    # Extract and export API keys using python (reliable JSON parsing)
    eval "$(python3 -c "
import json, os
try:
    cfg = json.load(open(os.path.expanduser('~/.demascas/config.json')))
    for k in ('google_api_key', 'google_cse_id'):
        v = cfg.get(k, '')
        if v:
            print(f'export {k.upper()}=\"{v}\"')
except Exception:
    pass
")"
else
    echo "[*] No config at $CONFIG_FILE — smart_search will use DuckDuckGo"
fi

# ── 1. Start LLM server (Ollama for Linux, MLX for macOS) ──────────

echo "[*] Checking for LLM backend..."
if python -c "import mlx_lm" 2>/dev/null; then
    # macOS with MLX
    echo "[*] Starting MLX text server (localhost:${MLX_TEXT_PORT})..."
    python -m mlx_lm.server \
        --model "$MLX_TEXT_MODEL" \
        --port "$MLX_TEXT_PORT" &
    TEXT_PID=$!
    PIDS+=($TEXT_PID)
    echo "    PID: $TEXT_PID"

    echo "[*] Waiting for MLX text server..."
    for i in $(seq 1 30); do
        if curl -s "http://127.0.0.1:${MLX_TEXT_PORT}/v1/models" > /dev/null 2>&1; then
            echo "[✓] MLX text server ready."
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "❌  MLX text server failed to start."
            exit 1
        fi
        sleep 1
    done
    export DEMASCAS_LLM_ENDPOINT="http://127.0.0.1:${MLX_TEXT_PORT}"
    echo "[✓] MLX endpoint: $DEMASCAS_LLM_ENDPOINT"
else
    # Linux with Ollama
    echo "[⚠] MLX not found — using Ollama (Linux mode)"
    echo "[*] Checking Ollama..."
    if ! command -v ollama &>/dev/null; then
        echo "[*] Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    # Start Ollama server if not running
    if ! curl -s "http://127.0.0.1:${OLLAMA_PORT}/api/tags" > /dev/null 2>&1; then
        echo "[*] Starting Ollama server (localhost:${OLLAMA_PORT})..."
        ollama serve &
        OLLAMA_PID=$!
        PIDS+=($OLLAMA_PID)
        echo "    PID: $OLLAMA_PID"

        echo "[*] Waiting for Ollama server..."
        for i in $(seq 1 15); do
            if curl -s "http://127.0.0.1:${OLLAMA_PORT}/api/tags" > /dev/null 2>&1; then
                echo "[✓] Ollama server ready."
                break
            fi
            if [ "$i" -eq 15 ]; then
                echo "❌  Ollama server failed to start."
                exit 1
            fi
            sleep 1
        done
    else
        echo "[✓] Ollama server already running."
    fi

    # Pull model if not present
    echo "[*] Checking Ollama model: $OLLAMA_MODEL..."
    if ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
        echo "     Model $OLLAMA_MODEL ✓"
    else
        echo "[*] Pulling $OLLAMA_MODEL (this may take a while)..."
        ollama pull "$OLLAMA_MODEL"
    fi

    export DEMASCAS_LLM_ENDPOINT="http://127.0.0.1:${OLLAMA_PORT}"
    echo "[✓] Ollama endpoint: $DEMASCAS_LLM_ENDPOINT"
fi

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

# Export discovered VLM endpoint (written by start_vlm_server.py probe thread)
VLM_ENDPOINT_FILE="$HOME/.demascas/vlm_endpoint.txt"
if [ -f "$VLM_ENDPOINT_FILE" ]; then
    export DEMASCAS_VLM_ENDPOINT=$(cat "$VLM_ENDPOINT_FILE")
    echo "[✓] VLM endpoint: $DEMASCAS_VLM_ENDPOINT"
fi

# Export LLM endpoint (or empty if not running)
if [ -z "${DEMASCAS_LLM_ENDPOINT:-}" ]; then
    echo "[*] No LLM endpoint — LLM features disabled"
fi

# ── 3. Start Python tool server ─────────────────────────────────────

echo "[*] Starting Python tool server (localhost:5001)..."
python "$TOOL_SERVER" &
TOOL_PID=$!
PIDS+=($TOOL_PID)
echo "    PID: $TOOL_PID"

# Wait for tool server /health endpoint
echo "[*] Waiting for tool server..."
for i in $(seq 1 15); do
    if curl -s "http://127.0.0.1:5001/health" | grep -q '"status"' 2>/dev/null; then
        echo "[✓] Tool server ready."
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "❌  Tool server failed to start."
        exit 1
    fi
    sleep 1
done

# ── 4. Start C++ daemon in foreground ───────────────────────────────

echo "[*] Starting C++ daemon..."
echo ""
cd daemon

# sherpa-onnx shared libraries (Linux: LD_LIBRARY_PATH, macOS: DYLD_LIBRARY_PATH)
export LD_LIBRARY_PATH="${PWD}/models/sherpa-onnx/lib:${LD_LIBRARY_PATH:-}"
export DYLD_LIBRARY_PATH="${PWD}/models/sherpa-onnx/lib:${DYLD_LIBRARY_PATH:-}"

./build/demascas_daemon
