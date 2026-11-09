"""
DEMASCAS — core/llm/governor.py
Balance latency against the laptop's resources:

* Keep the model loaded while DEMASCAS is in use.  Ollama unloads a model
  after 5 idle minutes by default, and the next command then waits for a
  cold load (seconds).  We preload at startup and, after each command,
  extend the keep-alive to `keep_loaded_minutes` — so it stays warm during a
  session but still frees RAM when the user stops using DEMASCAS.
* Never fight the user's apps for memory: skip preloading when free RAM is
  below the model's needs (the runtime would just push the laptop into swap).

Other runtimes (llama.cpp server, LM Studio, MLX) keep the model resident
for as long as the server runs, so there is nothing to do for them here.

All calls are fire-and-forget on a background thread with short timeouts;
nothing here ever blocks a user command.
"""

from __future__ import annotations

import threading
import time

import requests

from core.llm.settings import LLMConfig

_inflight = False
_lock = threading.Lock()


def _ollama_keep_alive(cfg: LLMConfig, minutes: int, timeout: float) -> bool:
    """Empty /api/generate request = load (or keep) the model loaded."""
    keep = "-1" if minutes < 0 else f"{minutes}m"
    try:
        r = requests.post(f"{cfg.base_url}/api/generate",
                          json={"model": cfg.model, "keep_alive": keep},
                          timeout=(0.5, timeout))
        return r.status_code == 200
    except requests.RequestException:
        return False


def preload(cfg: LLMConfig, free_ram_gb: float | None = None,
            model_ram_gb: float | None = None) -> str:
    """Load the model now so the first command doesn't pay for it."""
    if not cfg.configured or cfg.runtime != "ollama" or cfg.keep_loaded_minutes == 0:
        return "skipped"
    if free_ram_gb is not None and model_ram_gb is not None and free_ram_gb < model_ram_gb:
        return (f"skipped: only {free_ram_gb:.1f} GB free, model needs ~{model_ram_gb:.1f} GB "
                "(it will load on first use)")
    t0 = time.time()
    ok = _ollama_keep_alive(cfg, cfg.keep_loaded_minutes, timeout=120)
    return f"loaded in {time.time() - t0:.1f} s" if ok else "failed"


def touch(cfg: LLMConfig) -> None:
    """Call after EVERY command (async).  Each chat request through Ollama's
    OpenAI-compatible endpoint resets the idle timer to the server default
    (5 min), so the longer keep-alive must be re-applied after each one.
    The request is a no-op when the model is loaded (~ms)."""
    global _inflight
    if not cfg.configured or cfg.runtime != "ollama" or cfg.keep_loaded_minutes == 0:
        return
    with _lock:
        if _inflight:
            return
        _inflight = True

    def _run():
        global _inflight
        try:
            _ollama_keep_alive(cfg, cfg.keep_loaded_minutes, 5.0)
        finally:
            with _lock:
                _inflight = False

    threading.Thread(target=_run, daemon=True, name="llm-keepalive").start()
