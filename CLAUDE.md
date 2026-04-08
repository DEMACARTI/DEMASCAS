# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start

```bash
# Build the C++ daemon (first time or after C++ changes)
./scripts/build.sh

# Run full stack (4 processes: MLX text server + tool server + C++ daemon)
source ../venv/bin/activate
./scripts/run.sh
```

## Architecture Overview

DEMASCAS is a **polyglot C++/Python** voice-controlled macOS agent — like JARVIS for your Mac. All processing runs 100% locally on Apple Silicon.

### Four Processes

| Process | Port | Role |
|---------|------|------|
| **MLX text server** | 8081 | Qwen2.5-3B-Instruct-4bit (~1.8GB RAM) |
| **MLX vision server** | 8082 (optional) | Qwen2.5-VL-3B-Instruct-4bit (~1.6GB RAM) |
| **Python tool server** | 5001 | 88 tools: OS control, browser automation, web agent, memory |
| **C++ daemon** | — | Audio capture → whisper.cpp STT → MLX LLM → Kokoro TTS |

### Processing Paths (latency-optimized)

The C++ daemon routes commands through 10 paths:
- **PATH 1** (<1µs): 72 keyboard shortcuts (⌘W, ⌘Q, etc.) — bypasses LLM
- **PATH 1.5-1.8** (<5ms): URL detection, app open/close, macros — deterministic
- **PATH 2** (~500ms): Simple Q&A — streaming LLM, no tools
- **PATH 2.5** (~1-3s): Multi-step planner — decomposes complex commands
- **PATH 4** (~2-5s): Deep intent — email, research with task-specific prompts
- **PATH 3** (~1-4s): Full ReAct tool-calling loop

### Key Components

```
core/
  toolbox.py        — 88 tool schemas (OpenAI-compatible JSON)
  memory.py         — ChromaDB RAG persistent memory
  learning.py       — Continuous learning (episodes, profile, people graph)
  knowledge.py      — Procedures, facts, corrections via ChromaDB
  screen_context.py — VLM + accessibility tree digest
  prompt_templates.py — Task-specific prompts for deep intent

actuators/
  system_control.py — Click, type, shortcuts, drag, scroll, clipboard
  browser.py        — Brave Browser automation via JS injection (~720 lines)
  web_agent.py      — Autonomous web agent (~430 lines)
  file_system.py    — PDF summary, file create/delete/find/open
  communication.py  — Email compose/read, smart reply

daemon/src/
  main.cpp          — State machine: SLEEPING→LISTENING→PROCESSING→CONFIRMING
  config.hpp        — 37 tuning knobs (audio, LLM, TTS)
  llm_client.cpp    — Streaming MLX client, ReAct loop, deep intent
  tool_bridge.cpp   — HTTP bridge to Python tool server
  tts.cpp           — Kokoro neural TTS via sherpa-onnx
```

## Tool Server Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/tool` | POST | Execute a named tool |
| `/tools` | GET | Return all tool schemas |
| `/tools_for_query` | POST | Return filtered tools (3-8 per query) |
| `/health` | GET | Liveness probe |
| `/context` | GET | Fetch learned context for a query |
| `/learn` | POST | Log interaction for learning |
| `/screen_context` | GET | VLM + a11y screen digest |
| `/deep_intent` | POST | Task classification + prompt |

## Adding a New Tool

1. Write the Python function in `actuators/` or `sensors/`
2. Add JSON schema to `core/toolbox.py` → `TOOLS` list
3. Register in `server/tool_server.py` → `TOOL_MAP`
4. Add keywords to `server/tool_router.py` for smart filtering
5. Restart tool server

## Configuration

All 37 tuning knobs live in `daemon/src/config.hpp`:
- MLX endpoints, model names, context windows
- Audio: sample rate, VAD threshold, silence duration
- TTS: Kokoro speaker, speed, fallback voice
- Conversation: max history turns, tool depth

After changing config, rebuild: `./scripts/build.sh`

## Persistent Storage (~/.demascas/)

| File/Dir | Purpose |
|----------|---------|
| `memory/` | ChromaDB vectors (episodes, knowledge) |
| `user_profile.json` | Learned preferences |
| `people.json` | People graph (≤100 contacts) |
| `web_memory.json` | Per-domain web memory |
| `vlm_endpoint.txt` | VLM endpoint cache |
| `config.json` | API keys (Google CSE, etc.) |

## RAM Budget (8GB Apple Silicon)

| Component | RAM |
|-----------|-----|
| Qwen 2.5 3B (text) | ~1.8GB |
| Qwen 2.5 VL 3B (vision, optional) | ~1.6GB |
| whisper.cpp models | ~222MB |
| Kokoro TTS | ~335MB |
| ChromaDB + Python server | ~110MB |
| **Total** | **~4.4GB** (with VLM) / **~2.8GB** (without) |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Port 8081/5001 in use | `lsof -ti:PORT | xargs kill` |
| Daemon not built | `./scripts/build.sh` |
| No mic input | System Settings → Privacy → Microphone → Terminal |
| Accessibility denied | System Settings → Privacy → Accessibility → Terminal |
| TTS falls back to `say` | Check `USE_KOKORO=true` in `config.hpp`, verify model files |
| PDF summarization fails | Requires Ollama: `brew install ollama && ollama pull qwen2.5:3b` |
