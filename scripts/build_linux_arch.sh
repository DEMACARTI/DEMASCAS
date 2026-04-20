#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — Build Script (Linux/Arch)
#
# What this does:
#   1. Checks prerequisites (cmake, pacman, portaudio, espeak-ng, xdotool)
#   2. Downloads whisper.cpp GGML models if missing
#   3. Downloads Kokoro TTS model (cross-platform)
#   4. Downloads sherpa-onnx Linux library
#   5. Installs Python deps from requirements.txt + Playwright browser
#   6. Compiles the C++ daemon via CMake (Release mode)
#
# Usage:
#   chmod +x scripts/build_linux_arch.sh
#   ./scripts/build_linux_arch.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# Navigate to DEMASCAS project root
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║    DEMASCAS Build Script (Linux/Arch)    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Check for sudo privileges ─────────────────────────────────────

echo "[0/6] Checking sudo privileges..."
if [ "$EUID" -ne 0 ]; then
    sudo -v || {
        echo "❌  Sudo access denied."
        exit 1
    }
    echo "     Sudo access ✓"
else
    echo "     Running as root ✓"
fi

# ── 1. Check prerequisites ──────────────────────────────────────────

echo "[1/6] Checking prerequisites..."

command -v pacman >/dev/null 2>&1 || {
    echo "❌  pacman not found. This script requires Arch Linux."; exit 1; }

command -v cmake >/dev/null 2>&1 || {
    echo "[*] Installing cmake..."
    sudo pacman -S --noconfirm cmake
}

command -v pkg-config >/dev/null 2>&1 || {
    echo "[*] Installing pkg-config..."
    sudo pacman -S --noconfirm pkg-config
}

# ── 1b. Install Linux UI automation dependencies ────────────────────

echo "[1b/6] Checking Linux UI automation deps..."
# Note: at-spi2-core provides the AT-SPI2 library, python-atspi provides the pyatspi Python binding
for pkg in xdotool at-spi2-core python-atspi xclip xsel; do
    if ! pacman -Q "$pkg" &>/dev/null; then
        echo "[*] Installing $pkg..."
        sudo pacman -S --noconfirm "$pkg"
    else
        echo "     $pkg ✓"
    fi
done

# ── 2. Install PortAudio ────────────────────────────────────────────

echo "[2/6] Checking PortAudio..."
if ! pacman -Q portaudio &>/dev/null; then
    echo "[*] Installing PortAudio..."
    sudo pacman -S --noconfirm portaudio
else
    echo "     PortAudio ✓"
fi

# ── 2b. Install espeak-ng for TTS fallback ──────────────────────────

echo "[2b/6] Checking espeak-ng..."
if ! pacman -Q espeak-ng &>/dev/null; then
    echo "[*] Installing espeak-ng..."
    sudo pacman -S --noconfirm espeak-ng
else
    echo "     espeak-ng ✓"
fi

# ── 3. Download whisper.cpp GGML models ─────────────────────────────

echo "[3/6] Checking whisper models..."
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

# ── 3b. Download sherpa-onnx pre-built library (Linux x86_64) ────────

echo "[3b/6] Checking sherpa-onnx library..."
SHERPA_DIR="daemon/models/sherpa-onnx"
SHERPA_VER="v1.12.28"

if [ ! -d "$SHERPA_DIR/lib" ]; then
    SHERPA_ASSET="sherpa-onnx-${SHERPA_VER}-linux-x64-shared.tar.bz2"
    SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/${SHERPA_VER}/${SHERPA_ASSET}"
    echo "     Downloading sherpa-onnx (${SHERPA_ASSET})..."
    curl -L --progress-bar -o "/tmp/sherpa-onnx.tar.bz2" "$SHERPA_URL"

    mkdir -p "$SHERPA_DIR"
    tar -xjf "/tmp/sherpa-onnx.tar.bz2" -C "$SHERPA_DIR" --strip-components=1
    rm -f "/tmp/sherpa-onnx.tar.bz2"

    if [ -d "$SHERPA_DIR/lib" ]; then
        echo "     sherpa-onnx ✓  (Linux x64 shared)"
    else
        echo "     ⚠️  sherpa-onnx download failed — using espeak-ng fallback"
    fi
else
    echo "     sherpa-onnx ✓"
fi

# ── 3c. Download Kokoro TTS voice model ──────────────────────────────

echo "[3c/6] Checking Kokoro TTS model..."
KOKORO_DIR="daemon/models/kokoro-en-v0_19"

if [ ! -f "$KOKORO_DIR/model.onnx" ]; then
    KOKORO_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-en-v0_19.tar.bz2"
    echo "     Downloading Kokoro model (~335 MB)..."
    curl -L --progress-bar -o "/tmp/kokoro.tar.bz2" "$KOKORO_URL"

    tar -xjf "/tmp/kokoro.tar.bz2" -C "daemon/models/"
    rm -f "/tmp/kokoro.tar.bz2"

    if [ -f "$KOKORO_DIR/model.onnx" ]; then
        echo "     Kokoro ✓  (82M params, 24 kHz)"
    else
        echo "     ⚠️  Kokoro download failed — using espeak-ng fallback"
    fi
else
    echo "     Kokoro ✓"
fi

# ── 4. Install Python dependencies ──────────────────────────────────

echo "[4/6] Checking Python dependencies..."
VENV_DIR="$(cd "$PROJECT_ROOT/.." && pwd)/venv"

if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
    echo "     Installing Python packages from requirements.txt..."
    pip install -q -r requirements.txt 2>/dev/null
    playwright install chromium 2>/dev/null || true
    echo "     requirements.txt + Playwright browser ✓ (venv)"
else
    echo "     ⚠️  No venv found at $VENV_DIR"
    echo "     Create with: python -m venv ../venv"
fi

# ── 5. Configure CMake ──────────────────────────────────────────────

echo "[5/6] Configuring CMake..."
mkdir -p daemon/build
cd daemon/build

cmake .. -DCMAKE_BUILD_TYPE=Release 2>&1 | grep -E "^(--|Checking|Found|Using)" || true

# ── 6. Build C++ daemon ─────────────────────────────────────────────

echo "[6/6] Building C++ daemon..."
NPROC=$(nproc 2>/dev/null || echo 4)
cmake --build . --config Release -j"$NPROC"

cd "$PROJECT_ROOT"

echo ""
echo "═══════════════════════════════════════════════"
echo "✅  Build complete!"
echo ""
echo "Binary:  daemon/build/demascas_daemon"
echo "Models:  daemon/models/ggml-tiny.bin"
echo "         daemon/models/ggml-base.bin"
echo "         daemon/models/kokoro-en-v0_19/"
echo "         daemon/models/sherpa-onnx/"
echo ""
echo "Linux deps: xdotool, at-spi2-core, python-atspi, xclip, xsel, espeak-ng"
echo "Python deps: requirements.txt + playwright browser (in venv)"
echo ""
echo "Run with:  ./scripts/run.sh"
echo "═══════════════════════════════════════════════"
