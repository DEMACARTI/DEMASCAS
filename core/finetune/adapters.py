"""
DEMASCAS — core/finetune/adapters.py
Versioned LoRA adapters: ~/.demascas/adapters/vN/
  adapters.safetensors + adapter_config.json   (written by mlx_lm lora)
  meta.json   {created, base_model, samples, metrics, adopted, reason}

The active adapter is llm.adapter_path in ~/.demascas/config.json; the MLX
server loads it at start (scripts/run.sh → --adapter-path).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time

ADAPTERS_DIR = os.path.join(os.path.expanduser("~"), ".demascas", "adapters")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".demascas", "config.json")


def _versions() -> list[tuple[int, str]]:
    if not os.path.isdir(ADAPTERS_DIR):
        return []
    out = []
    for d in os.listdir(ADAPTERS_DIR):
        m = re.fullmatch(r"v(\d+)", d)
        if m:
            out.append((int(m.group(1)), os.path.join(ADAPTERS_DIR, d)))
    return sorted(out)


def new_version_dir() -> str:
    n = (_versions()[-1][0] + 1) if _versions() else 1
    path = os.path.join(ADAPTERS_DIR, f"v{n}")
    os.makedirs(path, exist_ok=True)
    return path


def read_meta(path: str) -> dict:
    try:
        with open(os.path.join(path, "meta.json")) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_meta(path: str, meta: dict) -> None:
    with open(os.path.join(path, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)


def list_all() -> list[dict]:
    active = active_path()
    rows = []
    for n, path in _versions():
        meta = read_meta(path)
        rows.append({"version": f"v{n}", "path": path, "active": path == active,
                     "has_weights": os.path.exists(os.path.join(path, "adapters.safetensors")),
                     **meta})
    return rows


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_config(data: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), prefix=".config.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def active_path() -> str:
    return str((_read_config().get("llm") or {}).get("adapter_path", ""))


def activate(path: str) -> None:
    """Make `path` the active adapter ("" = base model).  Edits only
    llm.adapter_path so env-based setups and other keys stay untouched."""
    data = _read_config()
    llm = data.get("llm") or {}
    if path:
        llm["adapter_path"] = path
    else:
        llm.pop("adapter_path", None)
    data["llm"] = llm
    _write_config(data)


def rollback() -> str:
    """Switch to the previous ADOPTED adapter, or the base model."""
    m = re.fullmatch(r"v(\d+)", os.path.basename(active_path().rstrip("/")))
    current_n = int(m.group(1)) if m else 0
    older = [r for r in list_all()
             if r.get("adopted") and r["has_weights"] and int(r["version"][1:]) < current_n]
    target = older[-1]["path"] if older else ""
    activate(target)
    return target


def learning_settings() -> dict:
    d = _read_config().get("learning") or {}
    return {
        "auto_finetune": bool(d.get("auto_finetune", True)),
        "min_samples": int(d.get("min_samples", 150)),
        "min_new_samples": int(d.get("min_new_samples", 50)),
        "idle_minutes": float(d.get("idle_minutes", 10)),
        "min_free_gb": float(d.get("min_free_gb", 3.5)),
        "check_every_s": float(d.get("check_every_s", 300)),
    }


def status_line(cfg=None) -> str:
    from core.finetune import dataset
    rows = list_all()
    act = next((r for r in rows if r["active"]), None)
    try:
        _, st = dataset.collect()
        n = st.samples
    except Exception:
        n = 0
    ls = learning_settings()
    parts = [f"adapter {act['version']}" if act else "base model (no adapter yet)",
             f"{n} training examples collected (first training at {ls['min_samples']})",
             "auto fine-tune " + ("on (idle + plugged in)" if ls["auto_finetune"] else "off")]
    if cfg is not None and getattr(cfg, "runtime", "") != "mlx":
        parts.append("fine-tuning currently needs the MLX runtime")
    return " · ".join(parts)
