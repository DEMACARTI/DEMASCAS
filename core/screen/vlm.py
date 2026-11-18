"""
DEMASCAS — core/screen/vlm.py
The vision model, loaded only while it's needed.

On an 8 GB Mac the text model (≈3.1 GB) and a vision model can't both stay
in memory without swapping — that swapping made every reply take 8–20 s
(18 Nov).  So the vision model (default Qwen3.5-2B, ≈1.9 GB) is started the
first time something needs to SEE, kept while it's being used, and stopped
after a few idle minutes so the memory comes back.

  ~/.demascas/config.json  →  "llm": {"vision": {"model": "mlx-community/Qwen3.5-2B-4bit",
                                                 "chat_url": "http://127.0.0.1:8082/v1/chat/completions",
                                                 "idle_unload_s": 180}}
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests

DEFAULT_MODEL = "mlx-community/Qwen3.5-2B-4bit"
DEFAULT_URL = "http://127.0.0.1:8082/v1/chat/completions"
_LOG = os.path.join(os.path.expanduser("~"), ".demascas", "logs", "vlm.log")

_lock = threading.Lock()
_state: dict = {"proc": None, "last": 0.0, "watch": False, "load_s": None}


def settings() -> dict:
    try:
        with open(os.path.join(os.path.expanduser("~"), ".demascas", "config.json")) as f:
            llm = json.load(f).get("llm") or {}
    except (FileNotFoundError, ValueError):
        llm = {}
    v = llm.get("vision") or {}
    text_model = llm.get("model", "")
    model = v.get("model") or ""
    return {
        "model": model,
        "chat_url": v.get("chat_url") or DEFAULT_URL,
        # the same model as the text one = one shared server, already running
        "shared": bool(model) and model == text_model and v.get("chat_url") == llm.get("chat_url"),
        "idle_unload_s": int(v.get("idle_unload_s", 60)),
        "text_url": llm.get("chat_url", ""),
        "text_model": text_model,
    }


def configured() -> bool:
    return bool(settings()["model"])


def _base(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.hostname}:{u.port}"


def _ready(url: str) -> bool:
    try:
        r = requests.get(_base(url) + "/health", timeout=0.5)
        return r.ok and '"model_ready":true' in r.text.replace(" ", "")
    except requests.RequestException:
        return False


def ensure(timeout: float = 90.0) -> str | None:
    """The vision chat URL, starting the server if needed (≈5–15 s the first
    time, instant after).  None if no vision model is set up or it won't start."""
    s = settings()
    if not s["model"]:
        return None
    if s["shared"]:
        return s["chat_url"]
    with _lock:
        _state["last"] = time.time()
        if _ready(s["chat_url"]):
            return s["chat_url"]
        p = _state["proc"]
        if p is None or p.poll() is not None:
            port = str(urlparse(s["chat_url"]).port or 8082)
            os.makedirs(os.path.dirname(_LOG), exist_ok=True)
            env = dict(os.environ, APC_ENABLED="0", MLX_VLM_MAX_NUM_SEQS="1")
            print(f"[VISION] loading {s['model']} on :{port} …")
            _state["t0"] = time.time()
            _state["proc"] = subprocess.Popen(
                [sys.executable, "-m", "mlx_vlm.server", "--model", s["model"], "--host", "127.0.0.1", "--port", port],
                stdout=open(_LOG, "a"), stderr=subprocess.STDOUT, env=env)
            _start_watch()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ready(s["chat_url"]):
            if _state.get("t0"):
                _state["load_s"] = round(time.time() - _state.pop("t0"), 1)
                print(f"[VISION] ready in {_state['load_s']} s")
            _state["last"] = time.time()
            return s["chat_url"]
        p = _state["proc"]
        if p is not None and p.poll() is not None:
            print(f"[VISION] the vision server stopped while loading (see {_LOG})")
            return None
        time.sleep(0.25)
    return None


def unload() -> None:
    with _lock:
        p = _state["proc"]
        _state["proc"] = None
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        print("[VISION] unloaded (idle) — memory back for the text model")
        _warm_text_model()


def _warm_text_model() -> None:
    """While the vision model was loaded the text model got swapped out: the
    next reply waited 9.6 s for it (measured 18 Nov).  Page it back in now,
    in the background, with a 1-token request."""
    s = settings()
    if not s["text_url"]:
        return

    def ping():
        try:
            requests.post(s["text_url"], json={"model": s["text_model"], "messages": [{"role": "user", "content": "hi"}],
                                               "max_tokens": 1}, timeout=60)
        except requests.RequestException:
            pass
    threading.Thread(target=ping, name="warm-text", daemon=True).start()


def _start_watch() -> None:
    if _state["watch"]:
        return
    _state["watch"] = True

    def watch():
        while True:
            time.sleep(15)
            p = _state["proc"]
            if p is not None and p.poll() is None and time.time() - _state["last"] > settings()["idle_unload_s"]:
                unload()
    threading.Thread(target=watch, name="vlm-idle", daemon=True).start()


atexit.register(unload)


def ask(prompt: str, image_path: str | None = None, image_b64: str | None = None, max_tokens: int = 220) -> str:
    """One vision question → the answer text (or "[-] …")."""
    url = ensure()
    if not url:
        return "[-] The vision model isn't available."
    if image_path and not image_b64:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        mime = "image/jpeg" if image_b64.startswith("/9j/") else "image/png"
        content.insert(0, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}})
    body = {"model": settings()["model"], "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens,
            "temperature": 0.1, "chat_template_kwargs": {"enable_thinking": False}}
    try:
        r = requests.post(url, json=body, timeout=120)
        _state["last"] = time.time()
        j = r.json()
        text = (j.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (requests.RequestException, ValueError) as e:
        return f"[-] The vision model didn't answer: {e}"
    # Qwen3.5 may still wrap reasoning in <think>…</think>
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


def status() -> dict:
    p = _state["proc"]
    return {"configured": configured(), "loaded": p is not None and p.poll() is None, "last_load_s": _state["load_s"],
            "model": settings()["model"]}
