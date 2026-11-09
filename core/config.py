"""
DEMASCAS — core/config.py
Single source of truth for service endpoints used by the Python side.

DEMASCAS ships WITHOUT a language model.  The LLM the user plugged in is
resolved once at import from core/llm/settings.py (env vars → saved config
→ not configured).  Run `python main.py setup` to choose one; restart the
tool server after changing it.
"""

import os

from core.llm import settings as _llm_settings

LLM = _llm_settings.load()

# ── Text LLM (any OpenAI-compatible local runtime) ───────────────────
LLM_CHAT_URL = LLM.chat_url          # "" when not configured
LLM_MODEL = LLM.model
LLM_CONFIGURED = LLM.configured
LLM_NOT_CONFIGURED_MSG = (
    "[-] No language model is set up yet. Run: python main.py setup"
)

# ── Vision model (optional) ──────────────────────────────────────────
VL_CHAT_URL = LLM.vision.chat_url    # "" when vision is not configured
VL_MODEL = LLM.vision.model
MLX_VL_BASE = os.getenv(
    "DEMASCAS_MLX_VL_BASE",
    (LLM.vision.chat_url.split("/v1/")[0] if LLM.vision.chat_url else "http://127.0.0.1:8082"),
).rstrip("/")
MLX_VL_MODEL = VL_MODEL or os.getenv(
    "DEMASCAS_MLX_VL_MODEL", "mlx-community/Qwen2.5-VL-3B-Instruct-4bit"
)

# Backward-compatible aliases (older modules imported these names).
MLX_TEXT_URL = LLM_CHAT_URL
MLX_TEXT_MODEL = LLM_MODEL

# ── Python tool server (used by agents calling back into the server) ─
TOOL_SERVER_URL = os.getenv(
    "DEMASCAS_TOOL_SERVER_URL",
    f"http://127.0.0.1:{os.getenv('DEMASCAS_PORT', '5001')}",
).rstrip("/")
