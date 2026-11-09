"""
DEMASCAS — core/llm/settings.py
The user's plugged-in LLM, stored in ~/.demascas/config.json under "llm"
(the same file that holds other DEMASCAS settings; other keys are preserved).

Resolution order (first match wins):
  1. Environment: DEMASCAS_LLM_URL + DEMASCAS_LLM_MODEL
     (legacy DEMASCAS_MLX_TEXT_URL / DEMASCAS_MLX_TEXT_MODEL still honoured)
  2. Saved config (written by `python main.py setup`)
  3. Nothing → "not configured" (DEMASCAS ships without an LLM; the tool
     server tells the user to run setup)

Example config:
  "llm": {
    "runtime": "ollama",
    "chat_url": "http://127.0.0.1:11434/v1/chat/completions",
    "model": "qwen2.5:3b",
    "context_tokens": 4096,
    "keep_loaded_minutes": 15,
    "autostart": false,
    "vision": {"chat_url": "...", "model": "qwen2.5vl:3b"}
  }
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".demascas")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONTEXT = {"ollama": 4096, "lmstudio": 4096, "llamacpp": 4096,
                   "mlx": 8192, "jan": 4096, "openai": 4096}


@dataclass
class VisionConfig:
    chat_url: str = ""
    model: str = ""


@dataclass
class LLMConfig:
    runtime: str = ""
    chat_url: str = ""
    model: str = ""
    context_tokens: int = 4096
    keep_loaded_minutes: int = 15      # Ollama: keep weights in RAM this long after last use
    autostart: bool = False            # run.sh may start the runtime (MLX has no background service)
    vision: VisionConfig = field(default_factory=VisionConfig)
    source: str = "none"               # env | config | none   (not persisted)

    @property
    def configured(self) -> bool:
        return bool(self.chat_url and self.model)

    @property
    def base_url(self) -> str:
        p = urlparse(self.chat_url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["configured"] = self.configured
        return d


def _normalize_chat_url(url: str) -> str:
    """Accept a base URL or a full chat URL."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _read_file() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def load() -> LLMConfig:
    """Resolve the active LLM (env → config file → not configured)."""
    env_url = os.getenv("DEMASCAS_LLM_URL") or os.getenv("DEMASCAS_MLX_TEXT_URL", "")
    env_model = os.getenv("DEMASCAS_LLM_MODEL") or os.getenv("DEMASCAS_MLX_TEXT_MODEL", "")
    raw = _read_file().get("llm") or {}

    if env_url and env_model:
        cfg = LLMConfig(
            runtime=os.getenv("DEMASCAS_LLM_RUNTIME", raw.get("runtime", "openai")),
            chat_url=_normalize_chat_url(env_url), model=env_model, source="env",
        )
        cfg.context_tokens = int(os.getenv("DEMASCAS_LLM_CONTEXT",
                                           raw.get("context_tokens")
                                           or DEFAULT_CONTEXT.get(cfg.runtime, 4096)))
        cfg.keep_loaded_minutes = int(raw.get("keep_loaded_minutes", 15))
    elif raw.get("chat_url") and raw.get("model"):
        rt = raw.get("runtime", "openai")
        cfg = LLMConfig(
            runtime=rt, chat_url=_normalize_chat_url(raw["chat_url"]), model=raw["model"],
            context_tokens=int(raw.get("context_tokens") or DEFAULT_CONTEXT.get(rt, 4096)),
            keep_loaded_minutes=int(raw.get("keep_loaded_minutes", 15)),
            autostart=bool(raw.get("autostart", False)),
            source="config",
        )
    else:
        cfg = LLMConfig()

    v = raw.get("vision") or {}
    env_vl = os.getenv("DEMASCAS_VLM_URL", "")
    if env_vl and os.getenv("DEMASCAS_VLM_MODEL"):
        cfg.vision = VisionConfig(_normalize_chat_url(env_vl), os.getenv("DEMASCAS_VLM_MODEL", ""))
    elif v.get("chat_url") and v.get("model"):
        cfg.vision = VisionConfig(_normalize_chat_url(v["chat_url"]), v["model"])
    return cfg


def save(cfg: LLMConfig) -> str:
    """Persist the LLM choice atomically, keeping other config keys."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = _read_file()
    llm = {
        "runtime": cfg.runtime,
        "chat_url": _normalize_chat_url(cfg.chat_url),
        "model": cfg.model,
        "context_tokens": int(cfg.context_tokens),
        "keep_loaded_minutes": int(cfg.keep_loaded_minutes),
        "autostart": bool(cfg.autostart),
    }
    if cfg.vision.chat_url and cfg.vision.model:
        llm["vision"] = {"chat_url": _normalize_chat_url(cfg.vision.chat_url),
                         "model": cfg.vision.model}
    data["llm"] = llm
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)   # may hold API keys
    except OSError:
        pass
    return CONFIG_PATH
