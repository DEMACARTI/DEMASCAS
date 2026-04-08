#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — Build Script (macOS / Apple Silicon)
#
# What this does:
#   1. Checks prerequisites (cmake, brew, portaudio)
#   2. Downloads whisper.cpp GGML models if missing
#   3. Installs Python deps (flask) for the tool server
#   4. Compiles the C++ daemon via CMake (Release mode)
#
# Usage:
#   chmod +x scripts/build.sh
#   ./scripts/build.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# Navigate to DEMASCAS project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║    DEMASCAS Build Script (macOS ARM64)   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Check prerequisites ──────────────────────────────────────────

echo "[1/5] Checking prerequisites..."

command -v brew  >/dev/null 2>&1 || {
    echo "❌  Homebrew not found.  Install from https://brew.sh"; exit 1; }

command -v cmake >/dev/null 2>&1 || {
    echo "[*] Installing cmake via Homebrew..."
    brew install cmake
}

command -v pkg-config >/dev/null 2>&1 || {
    echo "[*] Installing pkg-config via Homebrew..."
    brew install pkg-config
}

# ── 2. Install PortAudio ────────────────────────────────────────────

echo "[2/5] Checking PortAudio..."
if ! brew list portaudio &>/dev/null; then
    echo "[*] Installing PortAudio..."
    brew install portaudio
else
    echo "     PortAudio ✓"
fi

# ── 3. Download whisper.cpp GGML models ─────────────────────────────

echo "[3/5] Checking whisper models..."
mkdir -p daemon/models

MODEL_BASE_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

for model in tiny base; do
    MODEL_FILE="daemon/models/ggml-${model}.bin"
    if [ ! -f "$MODEL_FILE" ]; then
        echo "     Downloading ggml-${model}.bin..."
        curl -L --progress-bar -o "$MODEL_FILE" \
            "${MODEL_BASE_URL}/ggml-${model}.bin"
    else
        echo "     ggml-${model}.bin ✓"
    fi
done

# ── 3b. Download sherpa-onnx pre-built library (native arm64 TTS) ────

echo "[3b/5] Checking sherpa-onnx library..."
SHERPA_DIR="daemon/models/sherpa-onnx"
SHERPA_VER="v1.12.28"

if [ ! -d "$SHERPA_DIR/lib" ]; then
    SHERPA_ASSET="sherpa-onnx-${SHERPA_VER}-osx-universal2-shared.tar.bz2"
    SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/${SHERPA_VER}/${SHERPA_ASSET}"
    echo "     Downloading sherpa-onnx shared library (${SHERPA_ASSET})..."
    curl -L --progress-bar -o "/tmp/sherpa-onnx.tar.bz2" "$SHERPA_URL"

    mkdir -p "$SHERPA_DIR"
    tar -xjf "/tmp/sherpa-onnx.tar.bz2" -C "$SHERPA_DIR" --strip-components=1
    rm -f "/tmp/sherpa-onnx.tar.bz2"

    if [ -d "$SHERPA_DIR/lib" ]; then
        echo "     sherpa-onnx library ✓  (native arm64 + x86_64)"
    else
        echo "     ⚠️  sherpa-onnx download failed — will use macOS 'say' as fallback"
    fi
else
    echo "     sherpa-onnx library ✓"
fi

# ── 3c. Download Kokoro TTS voice model ──────────────────────────────

echo "[3c/5] Checking Kokoro TTS model..."
KOKORO_DIR="daemon/models/kokoro-en-v0_19"

if [ ! -f "$KOKORO_DIR/model.onnx" ]; then
    KOKORO_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-en-v0_19.tar.bz2"
    echo "     Downloading Kokoro-82M voice model (~335 MB)..."
    curl -L --progress-bar -o "/tmp/kokoro.tar.bz2" "$KOKORO_URL"

    tar -xjf "/tmp/kokoro.tar.bz2" -C "daemon/models/"
    rm -f "/tmp/kokoro.tar.bz2"

    if [ -f "$KOKORO_DIR/model.onnx" ]; then
        echo "     Kokoro model ✓  (82 M params, 24 kHz, 11 voices)"
    else
        echo "     ⚠️  Kokoro download failed — will use macOS 'say' as fallback"
    fi
else
    echo "     Kokoro model ✓"
fi

# ── 4. Install Python dependencies ──────────────────────────────────

echo "[4/5] Checking Python dependencies..."
VENV_DIR="$(cd "$PROJECT_ROOT/.." && pwd)/venv"

if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
    pip install -q flask 2>/dev/null
    echo "     Flask ✓ (venv)"
else
    echo "     ⚠️  No venv found at $VENV_DIR — install flask manually"
fi

# ── 5. Build C++ daemon ─────────────────────────────────────────────

echo "[5/5] Building C++ daemon..."
mkdir -p daemon/build
cd daemon/build

cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    2>&1 | grep -E "^(--|Fetching|Configuring|Building|\\[)" || true

NPROC=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
cmake --build . --config Release -j"$NPROC"

cd "$PROJECT_ROOT"

echo ""
echo "═══════════════════════════════════════════════"
echo "✅  Build complete!"
echo ""
echo "Binary:  daemon/build/demascas_daemon"
echo "Models:  daemon/models/ggml-tiny.bin"
echo "         daemon/models/ggml-base.bin"
echo ""
echo "Run with:  ./scripts/run.sh"
echo "═══════════════════════════════════════════════"
