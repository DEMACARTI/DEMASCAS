"""
DEMASCAS — core/llm
Pluggable local-LLM layer.  DEMASCAS ships WITHOUT a model: this package
profiles the laptop, detects whatever local runtime the user has (Ollama,
LM Studio, llama.cpp, MLX, Jan or any OpenAI-compatible server), recommends
a model that fits the hardware, and stores the user's choice.

Modules
  hardware   — CPU / RAM / GPU / battery profile (macOS, Windows, Linux)
  runtimes   — detect running runtimes and list their models
  catalog    — curated open models with per-runtime IDs and memory needs
  recommend  — memory budget + model picks for this machine
  settings   — persisted LLM choice (~/.demascas/config.json → "llm")
  benchmark  — measure time-to-first-token and tokens/s on the real model
  governor   — keep the model loaded while in use; resource limits
"""
