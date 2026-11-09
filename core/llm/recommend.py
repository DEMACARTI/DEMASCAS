"""
DEMASCAS — core/llm/recommend.py
Pick a local model that this laptop can run WITHOUT lagging, swapping or
overheating — and that answers fast enough for a voice assistant.

Rules
-----
* Memory budget
    - Apple Silicon (unified memory) and CPU-only machines:
          budget = min(40 % of RAM, RAM − reserve),  reserve = min(4.5 GB, 60 % of RAM)
      (keeps ~4.5 GB for macOS/Windows/Linux, the user's apps and DEMASCAS's
       own whisper + TTS + tool server ≈ 1 GB; scaled down on tiny machines)
    - NVIDIA GPU: budget = 90 % of VRAM (model fully on the GPU).  If that is
      smaller than the RAM budget we still use the GPU budget — partial
      offload is much slower.
* Speed cap (latency-first)
    - "balanced" (default) caps the recommendation at 8B parameters.
    - CPU-only machines (no Apple/NVIDIA acceleration) are capped at ~3B:
      bigger models generate too slowly on CPU for a voice loop.
    - "quality" lifts the 8B cap (still within the memory budget).
* Already-installed models that fit win over downloads.
* The benchmark (core/llm/benchmark.py) has the final word: if the chosen
  model is too slow on this machine, step down a size.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.llm import catalog
from core.llm.hardware import HardwareProfile
from core.llm.runtimes import ModelInfo, RuntimeInfo

RESERVE_GB = 4.5
UNIFIED_FRACTION = 0.40
GPU_FRACTION = 0.90
BALANCED_MAX_PARAMS_B = 8.5
CPU_ONLY_MAX_PARAMS_B = 3.3


@dataclass
class Budget:
    memory_gb: float
    max_params_b: float
    accel: str          # apple | nvidia | cpu
    reason: str


@dataclass
class Pick:
    model_id: str
    runtime: str               # ollama | mlx | lmstudio | llamacpp | jan | openai
    label: str
    ram_gb: float | None
    installed: bool
    base_url: str = ""         # set when installed (runtime already running)
    why: str = ""
    install_steps: list[str] = field(default_factory=list)


def budget(p: HardwareProfile, mode: str = "balanced") -> Budget:
    accel = p.gpu.kind if p.gpu.kind in ("apple", "nvidia") else "cpu"
    reserve = min(RESERVE_GB, 0.6 * p.ram_gb)
    ram_budget = max(0.0, min(p.ram_gb * UNIFIED_FRACTION, p.ram_gb - reserve))
    if accel == "nvidia":
        mem = round(p.gpu.vram_gb * GPU_FRACTION, 1)
        reason = f"90% of {p.gpu.vram_gb:.0f} GB VRAM so the whole model stays on the GPU"
    else:
        mem = round(ram_budget, 1)
        reason = (f"min(40% of {p.ram_gb:.0f} GB RAM, RAM − {reserve:.1f} GB reserved for the "
                  f"OS, your apps and DEMASCAS)")
    if accel == "cpu":
        cap = CPU_ONLY_MAX_PARAMS_B
        reason += "; CPU-only inference → capped at ~3B for voice-speed replies"
    else:
        cap = 1000.0 if mode == "quality" else BALANCED_MAX_PARAMS_B
    return Budget(memory_gb=mem, max_params_b=cap, accel=accel, reason=reason)


def _model_ram(m: ModelInfo) -> float | None:
    """Estimate resident RAM of an installed model."""
    cm = catalog.find(m.id)
    if cm:
        return cm.ram_gb
    if m.size_gb:
        return round(m.size_gb * 1.15 + 0.4, 1)     # weights + KV/overhead
    if m.params_b:
        return round(m.params_b * 0.62 + 0.5, 1)    # ~4-bit estimate
    return None


def _model_params(m: ModelInfo) -> float | None:
    cm = catalog.find(m.id)
    return cm.params_b if cm else m.params_b


def _fits(ram: float | None, params: float | None, b: Budget) -> bool:
    if ram is not None and ram > b.memory_gb:
        return False
    if params is not None and params > b.max_params_b:
        return False
    return ram is not None or params is not None


def _size_class(params_b: float) -> int:
    return 0 if params_b <= 2 else 1 if params_b <= 4.5 else 2 if params_b <= 9 else 3


def preferred_runtime(p: HardwareProfile, mlx_available: bool) -> str:
    """Runtime to suggest when nothing is installed."""
    if p.apple_silicon and mlx_available:
        return "mlx"      # native Apple Silicon inference
    return "ollama"       # one consistent path on macOS, Windows and Linux


def install_steps(model: catalog.CatalogModel, runtime: str, os_name: str) -> list[str]:
    if runtime == "ollama":
        steps = {
            "macos": ["Install Ollama: brew install ollama   (or download from https://ollama.com/download)",
                      "Start it: open the Ollama app (or run: ollama serve)"],
            "windows": ["Install Ollama: winget install Ollama.Ollama   (or download from https://ollama.com/download)",
                        "Ollama starts automatically in the system tray"],
            "linux": ["Install Ollama: curl -fsSL https://ollama.com/install.sh | sh",
                      "It runs as a service (check: systemctl status ollama)"],
        }[os_name]
        return steps + [f"Download the model: ollama pull {model.ids['ollama']}"]
    if runtime == "mlx":
        return ["Install MLX: pip install mlx-lm",
                f"Start the server (downloads the model the first time): "
                f"python -m mlx_lm server --model {model.ids['mlx']} --port 8081"]
    if runtime == "lmstudio":
        return ["Install LM Studio from https://lmstudio.ai",
                f"In LM Studio, search for and download: {model.ids.get('gguf', model.label)} (Q4_K_M)",
                "Developer tab → Start Server (port 1234)"]
    if runtime == "llamacpp":
        return ["Install llama.cpp (https://github.com/ggml-org/llama.cpp/releases, or: brew install llama.cpp)",
                f"Run: llama-server -hf {model.ids.get('gguf', '')}:Q4_K_M --port 9931"]
    return []


def recommend(p: HardwareProfile, runtimes: list[RuntimeInfo], mode: str = "balanced",
              mlx_available: bool = False) -> tuple[Budget, list[Pick]]:
    """Ranked picks: installed models that fit first, then models to install."""
    b = budget(p, mode)
    picks: list[Pick] = []

    # 1) Installed text models that fit, largest-that-fits first.
    installed: list[tuple[float, Pick]] = []
    for rt in runtimes:
        for m in rt.models:
            if m.vision:
                continue
            ram, params = _model_ram(m), _model_params(m)
            if not _fits(ram, params, b):
                continue
            cm = catalog.find(m.id)
            # Size class first; a model that's already loaded wins ties
            # (no load delay on the first command).
            score = _size_class(params or (ram or 0) / 0.62) * 10 + (1 if m.loaded else 0)
            installed.append((score, Pick(
                model_id=m.id, runtime=rt.kind, label=cm.label if cm else m.id,
                ram_gb=ram, installed=True, base_url=rt.base_url,
                why=("already installed" + (" and loaded" if m.loaded else "")
                     + f" in {rt.label}; ~{ram} GB fits your {b.memory_gb} GB budget"
                     if ram else f"already installed in {rt.label}"),
            )))
    installed.sort(key=lambda t: -t[0])
    picks.extend(pk for _, pk in installed)

    # 2) Catalog suggestions for the runtime(s) the user has, else the
    #    preferred runtime for this platform.
    runtime_kinds = [rt.kind for rt in runtimes if rt.kind in ("ollama", "mlx")]
    target_rt = runtime_kinds[0] if runtime_kinds else preferred_runtime(p, mlx_available)
    have = {pk.model_id for pk in picks}
    fitting = [m for m in catalog.text_models()
               if m.ram_gb <= b.memory_gb and m.params_b <= b.max_params_b and target_rt in m.ids]
    # Largest size class first; within a class keep catalog order, which
    # ranks JSON/tool-following reliability (e.g. Qwen2.5 7B before Llama 8B).
    order = {m.key: i for i, m in enumerate(catalog.CATALOG)}
    fitting.sort(key=lambda m: (-_size_class(m.params_b), order[m.key]))
    for m in fitting[:3]:
        if m.ids[target_rt] in have:
            continue
        picks.append(Pick(
            model_id=m.ids[target_rt], runtime=target_rt, label=m.label, ram_gb=m.ram_gb,
            installed=False,
            why=f"~{m.ram_gb} GB fits your {b.memory_gb} GB budget. {m.notes}".strip(),
            install_steps=install_steps(m, target_rt, p.os),
        ))
    return b, picks


def vision_pick(p: HardwareProfile, text_ram_gb: float | None, runtime: str,
                mode: str = "balanced") -> catalog.CatalogModel | None:
    """Optional vision model — only when it fits ALONGSIDE the text model."""
    b = budget(p, mode)
    room = b.memory_gb - (text_ram_gb or 0)
    for m in sorted(catalog.vision_models(), key=lambda m: -m.params_b):
        if m.ram_gb <= room and runtime in m.ids:
            return m
    return None
