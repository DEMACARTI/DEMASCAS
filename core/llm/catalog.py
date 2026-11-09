"""
DEMASCAS — core/llm/catalog.py
Curated open-weight models that work well with DEMASCAS's JSON tool-calling
prompts, with per-runtime identifiers and approximate memory needs.

This is DATA, not a dependency: DEMASCAS never downloads or ships these.
The recommender prefers models the user ALREADY has installed and only falls
back to this list when suggesting what to install.  Newer models can be added
here (or simply installed — any installed model is usable).

ram_gb = approximate resident memory at 4-bit quantization with a 4k-token
context (weights + KV cache + runtime overhead).  Keep these conservative:
under-estimating causes swapping, which is exactly the lag we must avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogModel:
    key: str
    label: str
    params_b: float
    ram_gb: float
    vision: bool = False
    notes: str = ""
    # runtime → identifier understood by that runtime
    ids: dict = field(default_factory=dict)


# Ordered by preference WITHIN a size class (JSON/tool-following reliability
# first).  Thinking/reasoning-by-default models are deliberately excluded:
# long hidden reasoning adds seconds of latency to every command.
CATALOG: list[CatalogModel] = [
    CatalogModel(
        "qwen2.5-1.5b", "Qwen2.5 1.5B Instruct", 1.5, 1.3,
        notes="Smallest usable option for low-RAM or CPU-only laptops.",
        ids={"ollama": "qwen2.5:1.5b",
             "mlx": "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
             "gguf": "Qwen/Qwen2.5-1.5B-Instruct-GGUF"}),
    CatalogModel(
        "qwen2.5-3b", "Qwen2.5 3B Instruct", 3.0, 2.3,
        notes="Best speed/quality balance for 8 GB laptops; reliable JSON.",
        ids={"ollama": "qwen2.5:3b",
             "mlx": "mlx-community/Qwen2.5-3B-Instruct-4bit",
             "gguf": "Qwen/Qwen2.5-3B-Instruct-GGUF"}),
    CatalogModel(
        "llama3.2-3b", "Llama 3.2 3B Instruct", 3.2, 2.4,
        ids={"ollama": "llama3.2:3b",
             "mlx": "mlx-community/Llama-3.2-3B-Instruct-4bit",
             "gguf": "bartowski/Llama-3.2-3B-Instruct-GGUF"}),
    CatalogModel(
        "phi4-mini", "Phi-4 mini Instruct (3.8B)", 3.8, 2.9,
        notes="Previous DEMASCAS default; strong reasoning for its size.",
        ids={"ollama": "phi4-mini",
             "mlx": "mlx-community/Phi-4-mini-instruct-4bit",
             "gguf": "bartowski/microsoft_Phi-4-mini-instruct-GGUF"}),
    CatalogModel(
        "qwen2.5-7b", "Qwen2.5 7B Instruct", 7.6, 5.2,
        notes="Recommended for 16 GB laptops or 6 GB+ GPUs.",
        ids={"ollama": "qwen2.5:7b",
             "mlx": "mlx-community/Qwen2.5-7B-Instruct-4bit",
             "gguf": "Qwen/Qwen2.5-7B-Instruct-GGUF"}),
    CatalogModel(
        "llama3.1-8b", "Llama 3.1 8B Instruct", 8.0, 5.6,
        ids={"ollama": "llama3.1:8b",
             "mlx": "mlx-community/Meta-Llama-3.1-8B-Instruct-4bit",
             "gguf": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"}),
    CatalogModel(
        "qwen2.5-14b", "Qwen2.5 14B Instruct", 14.7, 9.6,
        notes="Quality option for 32 GB+ machines; noticeably slower.",
        ids={"ollama": "qwen2.5:14b",
             "mlx": "mlx-community/Qwen2.5-14B-Instruct-4bit",
             "gguf": "Qwen/Qwen2.5-14B-Instruct-GGUF"}),
    # ── Vision (optional, for screenshot understanding) ──────────────
    CatalogModel(
        "qwen2.5vl-3b", "Qwen2.5-VL 3B", 3.0, 3.3, vision=True,
        ids={"ollama": "qwen2.5vl:3b",
             "mlx": "mlx-community/Qwen2.5-VL-3B-Instruct-4bit"}),
    CatalogModel(
        "qwen2.5vl-7b", "Qwen2.5-VL 7B", 7.0, 6.5, vision=True,
        ids={"ollama": "qwen2.5vl:7b",
             "mlx": "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"}),
]


def text_models() -> list[CatalogModel]:
    return [m for m in CATALOG if not m.vision]


def vision_models() -> list[CatalogModel]:
    return [m for m in CATALOG if m.vision]


def find(model_id: str) -> CatalogModel | None:
    """Match an installed model id (any runtime) to a catalog entry."""
    norm = _norm(model_id)
    for m in CATALOG:
        for rid in m.ids.values():
            if _norm(rid) == norm or norm.startswith(_norm(rid)):
                return m
    return None


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    if s.endswith(":latest"):
        s = s[: -len(":latest")]
    return s
