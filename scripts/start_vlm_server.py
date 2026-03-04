#!/usr/bin/env python3
"""
DEMASCAS — scripts/start_vlm_server.py
Start the MLX Vision-Language Model server on port 8082.

Uses mlx-vlm to serve Qwen2.5-VL-3B-Instruct-4bit with an
OpenAI-compatible /v1/chat/completions endpoint.

Version-agnostic: works with mlx-vlm 0.1.x → 0.4.x by trying
multiple entry points in order of preference.

After the server starts, probes candidate endpoint paths and writes
the working path to ~/.demascas/vlm_endpoint.txt for other modules.

Usage:
    python scripts/start_vlm_server.py
"""

import subprocess
import sys
import os
import shutil
import time
import threading
import pathlib

MODEL = "mlx-community/Qwen2.5-VL-3B-Instruct-4bit"
PORT = 8082
HOST = "127.0.0.1"

# Resolve venv python — prefer the venv that launched us
_VENV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "venv")
PYTHON = os.path.join(_VENV_DIR, "bin", "python") if os.path.isfile(
    os.path.join(_VENV_DIR, "bin", "python")) else sys.executable

_ENDPOINT_FILE = pathlib.Path.home() / ".demascas" / "vlm_endpoint.txt"


def find_working_endpoint(max_wait: int = 30) -> str | None:
    """
    Probe candidate VLM endpoint paths until one responds (non-404).
    Runs in a background thread after the server process is launched.
    Writes the discovered path to ~/.demascas/vlm_endpoint.txt.
    """
    import urllib.request
    import urllib.error
    import json

    candidates = ["/v1/chat/completions", "/chat", "/generate"]
    base = f"http://{HOST}:{PORT}"

    deadline = time.time() + max_wait
    while time.time() < deadline:
        for path in candidates:
            url = f"{base}{path}"
            try:
                body = json.dumps({
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "test"}],
                    "max_tokens": 1,
                }).encode()
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = urllib.request.urlopen(req, timeout=5)
                # Any non-exception response means endpoint exists
                print(f"[*] VLM endpoint discovered: {path}")
                _write_endpoint(path)
                return path
            except urllib.error.HTTPError as e:
                # 404 = wrong path, anything else = path exists
                if e.code != 404:
                    print(f"[*] VLM endpoint discovered: {path} (HTTP {e.code})")
                    _write_endpoint(path)
                    return path
            except (urllib.error.URLError, OSError):
                pass  # server not up yet
        time.sleep(2)

    print("[!] Could not discover VLM endpoint after probing. Using default.")
    _write_endpoint("/v1/chat/completions")
    return None


def _write_endpoint(path: str):
    """Write the discovered endpoint path to the config file."""
    try:
        _ENDPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ENDPOINT_FILE.write_text(path)
    except Exception as e:
        print(f"[!] Could not write endpoint file: {e}")


def main():
    print(f"[*] Starting MLX VLM server on {HOST}:{PORT}")
    print(f"    Model: {MODEL}")
    print(f"    Python: {PYTHON}")
    print()

    # Launch endpoint discovery in background thread
    probe_thread = threading.Thread(target=find_working_endpoint, daemon=True)
    probe_thread.start()

    # Strategy 1: `python -m mlx_vlm.server` (mlx-vlm 0.2+)
    try:
        subprocess.run(
            [
                PYTHON, "-m", "mlx_vlm.server",
                "--model", MODEL,
                "--port", str(PORT),
                "--host", HOST,
                "--log-level", "warning",
            ],
            check=True,
        )
        return  # server ran and exited cleanly
    except subprocess.CalledProcessError as e:
        print(f"[!] mlx_vlm.server exited with code {e.returncode}")
    except FileNotFoundError:
        print(f"[!] Python not found at {PYTHON}")

    print("[*] Trying alternative entry point (mlx_vlm.server.app)...")

    # Strategy 2: direct import fallback (mlx-vlm 0.1.x or 0.3.12+)
    try:
        # Ensure our venv site-packages are importable
        import importlib
        mod = importlib.import_module("mlx_vlm.server")
        if hasattr(mod, "app"):
            # Flask/ASGI app object — run with uvicorn or direct
            try:
                import uvicorn
                uvicorn.run(mod.app, host=HOST, port=PORT, log_level="warning")
                return
            except ImportError:
                pass
        # Last resort: call main() if it exists
        if hasattr(mod, "main"):
            mod.main(model=MODEL, port=PORT, host=HOST)
            return
    except ImportError:
        print("[!] mlx-vlm not installed. Install with:")
        print("    pip install mlx-vlm")
        sys.exit(1)
    except Exception as e2:
        print(f"[!] VLM server failed to start: {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
