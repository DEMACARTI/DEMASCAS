#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — Build Script (macOS / Apple Silicon · Linux: Arch, Debian/Ubuntu, Fedora)
#
# What this does:
#   1. Checks prerequisites (cmake, pkg-config, portaudio, speexdsp) and
#      installs missing ones with Homebrew / pacman / apt / dnf
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

OS="$(uname -s)"          # Darwin | Linux
ARCH="$(uname -m)"        # arm64 | x86_64 | aarch64

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║        DEMASCAS Build Script              ║"
echo "╚══════════════════════════════════════════╝"
echo "     System: ${OS} ${ARCH}"
echo ""

# ── 1. Check prerequisites ──────────────────────────────────────────

echo "[1/5] Checking prerequisites..."

if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || {
        echo "❌  Homebrew not found.  Install from https://brew.sh"; exit 1; }
    for pkg in cmake pkg-config portaudio speexdsp; do
        if ! brew list "$pkg" &>/dev/null; then
            echo "[*] Installing $pkg via Homebrew..."
            brew install "$pkg"
        fi
    done
elif [ "$OS" = "Linux" ]; then
    # Package names per distro: build tools, PortAudio (mic + speaker),
    # speexdsp (echo cancellation), curl/bzip2 (model downloads).
    if command -v pacman >/dev/null 2>&1; then          # Arch / Manjaro / EndeavourOS
        PKGS="base-devel cmake pkgconf git curl bzip2 lsof portaudio speexdsp python"
        MISSING=$(pacman -T $PKGS 2>/dev/null || true)
        if [ -n "$MISSING" ]; then
            echo "[*] Installing with pacman: $MISSING"
            sudo pacman -S --needed --noconfirm $MISSING
        fi
    elif command -v apt-get >/dev/null 2>&1; then       # Debian / Ubuntu
        sudo apt-get update -qq
        sudo apt-get install -y build-essential cmake pkg-config git curl bzip2 lsof \
            portaudio19-dev libspeexdsp-dev python3-venv
    elif command -v dnf >/dev/null 2>&1; then           # Fedora
        sudo dnf install -y gcc-c++ make cmake pkgconf-pkg-config git curl bzip2 lsof \
            portaudio-devel speexdsp-devel python3
    else
        echo "⚠️  Unknown package manager — install: cmake, pkg-config, a C++17 compiler,"
        echo "   PortAudio (dev headers), speexdsp (dev headers), curl, bzip2."
    fi
else
    echo "❌  Unsupported system: $OS (use macOS or Linux; on Windows use WSL2)."; exit 1
fi
for tool in cmake pkg-config curl; do
    command -v "$tool" >/dev/null 2>&1 || { echo "❌  $tool is still missing."; exit 1; }
done
pkg-config --exists portaudio-2.0 || { echo "❌  PortAudio development files not found."; exit 1; }

# ── 2. (macOS: Homebrew packages are checked above) ─────────────────

echo "[2/5] Prerequisites ✓"

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
    # Pre-built shared library for this system (Kokoro neural voice).
    case "${OS}-${ARCH}" in
        Darwin-*)        SHERPA_ASSET="sherpa-onnx-${SHERPA_VER}-osx-universal2-shared.tar.bz2" ;;
        Linux-x86_64)    SHERPA_ASSET="sherpa-onnx-${SHERPA_VER}-linux-x64-shared.tar.bz2" ;;
        Linux-aarch64)   SHERPA_ASSET="sherpa-onnx-${SHERPA_VER}-linux-aarch64-shared-cpu.tar.bz2" ;;
        *) echo "❌  No sherpa-onnx build for ${OS} ${ARCH}"; exit 1 ;;
    esac
    SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/${SHERPA_VER}/${SHERPA_ASSET}"
    echo "     Downloading sherpa-onnx shared library (${SHERPA_ASSET})..."
    TMP_TAR="$(mktemp -t sherpa.XXXXXX).tar.bz2"
    curl -L --fail --progress-bar -o "$TMP_TAR" "$SHERPA_URL"

    mkdir -p "$SHERPA_DIR"
    tar -xjf "$TMP_TAR" -C "$SHERPA_DIR" --strip-components=1
    rm -f "$TMP_TAR"

    if [ -d "$SHERPA_DIR/lib" ]; then
        echo "     sherpa-onnx library ✓  (${OS} ${ARCH})"
    else
        echo "     ⚠️  sherpa-onnx download failed — DEMASCAS will have no neural voice"
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
    TMP_TAR="$(mktemp -t kokoro.XXXXXX).tar.bz2"
    curl -L --fail --progress-bar -o "$TMP_TAR" "$KOKORO_URL"

    tar -xjf "$TMP_TAR" -C "daemon/models/"
    rm -f "$TMP_TAR"

    if [ -f "$KOKORO_DIR/model.onnx" ]; then
        echo "     Kokoro model ✓  (82 M params, 24 kHz, 11 voices)"
    else
        echo "     ⚠️  Kokoro download failed — DEMASCAS will have no neural voice"
    fi
else
    echo "     Kokoro model ✓"
fi

# ── 3d. Speech detector (Silero VAD, 644 KB) — tells a human voice from
#        fans/typing/music, and powers barge-in at a normal speaking voice.
echo "[3d/5] Checking speech detector model..."
VAD_MODEL="daemon/models/silero_vad.onnx"
if [ ! -f "$VAD_MODEL" ]; then
    curl -L --progress-bar -o "$VAD_MODEL" \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx" \
        || echo "     ⚠️  VAD download failed — falling back to loudness-based listening"
fi
[ -f "$VAD_MODEL" ] && echo "     Silero VAD ✓"

# ── 4. Install Python dependencies ──────────────────────────────────

echo "[4/5] Checking Python dependencies..."
VENV_DIR="$(cd "$PROJECT_ROOT/.." && pwd)/venv"

if [ ! -d "$VENV_DIR" ]; then
    # Arch's system Python is "externally managed" (PEP 668): never pip
    # install into it — make the project venv instead.
    echo "     Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "     Python packages ✓ (venv)"

# ── 5. Build C++ daemon ─────────────────────────────────────────────

echo "[5/5] Building C++ daemon..."
mkdir -p daemon/build
cd daemon/build

CMAKE_ARGS=(-DCMAKE_BUILD_TYPE=Release)
if [ "$OS" = "Darwin" ]; then
    CMAKE_ARGS+=(-DCMAKE_OSX_ARCHITECTURES=arm64)
fi
cmake .. "${CMAKE_ARGS[@]}" \
    2>&1 | grep -E "^(--|Fetching|Configuring|Building|\\[)" || true

NPROC=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
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
