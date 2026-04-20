#!/usr/bin/env python3
"""
DEMASCAS — scripts/start_vlm_server.py
Start the Vision-Language Model server for screen analysis.

For Ollama (Linux/macOS):
    Pulls and serves gemma4:31b-cloud via Ollama (unified LLM+VLM)

For MLX (macOS only):
    Falls back to mlx-vlm if Ollama is not available

Usage:
    python scripts/start_vlm_server.py
"""

import subprocess
import sys
import os
import time
import pathlib

# Ollama configuration
OLLAMA_MODEL = "gemma4:31b-cloud"
OLLAMA_PORT = 11434
OLLAMA_HOST = "http://127.0.0.1"

# MLX fallback (macOS only)
MLX_MODEL = "mlx-community/Qwen2.5-VL-3B-Instruct-4bit"
MLX_PORT = 8082

_ENDPOINT_FILE = pathlib.Path.home() / ".demascas" / "vlm_endpoint.txt"


def write_endpoint(path: str):
    """Write the discovered endpoint path to the config file."""
    try:
        _ENDPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ENDPOINT_FILE.write_text(path)
    except Exception as e:
        print(f"[!] Could not write endpoint file: {e}")


def check_ollama() -> bool:
    """Check if Ollama is installed and running."""
    # Check if ollama command exists
    if not subprocess.run(["which", "ollama"], capture_output=True).returncode == 0:
        return False

    # Check if Ollama server is responding
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{OLLAMA_HOST}:{OLLAMA_PORT}/api/tags", timeout=2)
        return req.status == 200
    except Exception:
        return False


def start_ollama_server() -> bool:
    """Start Ollama serve process if not running."""
    try:
        # Start ollama serve in background
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for server to be ready
        for _ in range(15):
            time.sleep(1)
            if check_ollama():
                return True
        return False
    except Exception as e:
        print(f"[!] Failed to start Ollama: {e}")
        return False


def pull_model(model: str) -> bool:
    """Pull a model via Ollama."""
    print(f"[*] Pulling {model} (this may take a while)...")
    try:
        result = subprocess.run(
            ["ollama", "pull", model],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes for large models
        )
        if result.returncode == 0:
            print(f"[✓] Model {model} ready")
            return True
        else:
            print(f"[!] Failed to pull model: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print(f"[!] Model pull timed out")
        return False
    except Exception as e:
        print(f"[!] Model pull failed: {e}")
        return False


def is_model_available(model: str) -> bool:
    """Check if a model is available in Ollama."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return model in result.stdout
    except Exception:
        return False


def main():
    print("[*] Starting VLM server...")
    print()

    # Try Ollama first (cross-platform)
    print("[*] Checking Ollama...")

    ollama_available = check_ollama()

    if not ollama_available:
        # Try to start Ollama
        print("[*] Ollama not running, attempting to start...")
        if not start_ollama_server():
            print("[!] Ollama not available. Check installation: https://ollama.com")
            print("[*] Falling back to MLX (macOS only)...")

            # MLX fallback
            try:
                import mlx_vlm
                print("[*] Starting MLX VLM server (macOS only)...")
                from mlx_vlm import server
                # This would need the original mlx-vlm startup logic
                print("[!] MLX fallback not fully implemented. Please install Ollama.")
                sys.exit(1)
            except ImportError:
                print("[!] Neither Ollama nor MLX available. Install Ollama:")
                print("    Linux: curl -fsSL https://ollama.com/install.sh | sh")
                print("    macOS: brew install ollama")
                sys.exit(1)

    print("[✓] Ollama server running")

    # Check and pull VLM model
    if not is_model_available(OLLAMA_MODEL):
        if not pull_model(OLLAMA_MODEL):
            print(f"[!] Failed to pull {OLLAMA_MODEL}")
            sys.exit(1)
    else:
        print(f"[✓] Model {OLLAMA_MODEL} available")

    # Write endpoint for other modules
    write_endpoint("/v1/chat/completions")

    print()
    print("═══════════════════════════════════════════════")
    print("✓  VLM server ready!")
    print()
    print(f"  Model: {OLLAMA_MODEL}")
    print(f"  Endpoint: {OLLAMA_HOST}:{OLLAMA_PORT}/v1/chat/completions")
    print()
    print("  The daemon will auto-detect the VLM endpoint.")
    print("═══════════════════════════════════════════════")
    print()

    # Keep running (Ollama serve runs in background)
    # This script just ensures the model is pulled and available
    print("[*] VLM setup complete. Ollama serve continues in background.")


if __name__ == "__main__":
    main()
