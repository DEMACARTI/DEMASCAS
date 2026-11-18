#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# DEMASCAS — one command on a new machine:   ./install.sh
#
# Adapts to the computer it runs on:
#   • OS / chip / RAM detected → the language model that fits (python main.py
#     setup: MLX on Apple Silicon, else Ollama / llama.cpp / LM Studio)
#   • vision sized to the RAM: a vision model loaded on demand (≥ 8 GB), or
#     on-device text recognition only (less)
#   • macOS → DEMASCAS.app in Applications (menu bar, no Terminal needed)
#     Linux → a launcher in your app menu
#
# Safe to run again: finished steps are skipped.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
OS="$(uname -s)"; ARCH="$(uname -m)"
if [ "$OS" = "Darwin" ]; then RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ));
elif [ "$OS" = "Linux" ]; then RAM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1048576 ));
else
    echo "❌  $OS isn't supported yet (DEMASCAS runs on macOS and Linux). Windows support is planned."
    exit 1
fi
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║            Installing DEMASCAS            ║"
echo "╚══════════════════════════════════════════╝"
echo "  This computer: $OS · $ARCH · ${RAM_GB} GB RAM"
echo ""

# ── 1. build: system packages, speech + voice models, Python, the Rust daemon
echo "━━ 1/5  Building (packages, voice models, Python, Rust) ━━"
./scripts/build.sh

VENV="$(cd "$ROOT/.." && pwd)/venv"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── 2. the brain: a language model that fits THIS machine
echo ""
echo "━━ 2/5  Choosing a language model for ${RAM_GB} GB ━━"
set +e; python main.py llm-check >/dev/null 2>&1; rc=$?; set -e
if [ "$rc" = "2" ]; then
    python main.py setup          # profiles hardware, detects runtimes, recommends, measures speed
else
    echo "  Already set up: $(python main.py doctor 2>/dev/null | grep '^LLM' | cut -c12-)"
fi

# ── 3. the eyes: sized to the RAM
echo ""
echo "━━ 3/5  Vision ━━"
python - "$RAM_GB" "$ARCH" "$OS" <<'PY'
import json, os, sys
ram, arch, osname = int(sys.argv[1]), sys.argv[2], sys.argv[3]
p = os.path.expanduser("~/.demascas/config.json")
cfg = json.load(open(p)) if os.path.exists(p) else {}
llm = cfg.setdefault("llm", {})
if llm.get("vision"):
    print(f"  Already set: {llm['vision'].get('model')}")
elif osname == "Darwin" and arch == "arm64" and ram >= 8:
    model = "mlx-community/Qwen3.5-4B-4bit" if ram >= 24 else "mlx-community/Qwen3.5-2B-4bit"
    llm["vision"] = {"chat_url": "http://127.0.0.1:8082/v1/chat/completions", "model": model,
                     "idle_unload_s": 300 if ram >= 16 else 60}
    json.dump(cfg, open(p, "w"), indent=1)
    print(f"  Vision model: {model} — loaded only when DEMASCAS needs to see, unloaded when idle")
    print("  (downloads on first use)")
else:
    print("  Screen reading with on-device text recognition (no vision model on this machine)")
PY

# ── 4. the app
echo ""
echo "━━ 4/5  App ━━"
if [ "$OS" = "Darwin" ]; then
    if pgrep -x DEMASCAS >/dev/null; then
        echo "  DEMASCAS is running — quit it from the menu bar, then run ./install.sh again to update the app."
    else
        ./scripts/build_app.sh
    fi
else
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/demascas.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=DEMASCAS
Comment=Your local voice assistant
Exec=bash -lc '"$ROOT/scripts/run.sh" >> "$HOME/.demascas/logs/demascas.log" 2>&1'
Terminal=false
Categories=Utility;
DESK
    mkdir -p "$HOME/.demascas/logs"
    echo "  Added DEMASCAS to your app menu (logs: ~/.demascas/logs/demascas.log)"
fi

# ── 5. what's still missing
echo ""
echo "━━ 5/5  Check ━━"
python main.py doctor --permissions || true
echo ""
echo "✅  DEMASCAS is installed."
if [ "$OS" = "Darwin" ]; then
    echo "   Open it from Applications (or Spotlight: DEMASCAS). First run: allow the"
    echo "   Microphone, and turn DEMASCAS on under Accessibility and Screen Recording"
    echo "   (menu bar icon → Permissions… shows what's missing)."
else
    echo "   Start it from your app menu, or run ./scripts/run.sh"
fi
