# DEMASCAS — Full Project Context for Claude AI

> **Last updated:** 4 March 2026
> **Author:** Daksh Rathore
> **Status:** Actively developed, functional, being iterated on

---

## 1. What Is DEMASCAS?

**DEMASCAS** = **Decision Engineering Machine Algorithm for System Control and Action Sequencing**

It is a **fully local, privacy-first, autonomous OS-level AI voice assistant for macOS** — essentially a JARVIS for your Mac. The user speaks a command, DEMASCAS hears it, understands it, and **acts on it** by controlling the operating system: opening apps, clicking buttons, typing text, reading the screen, browsing the web, managing files, and remembering things.

**Everything runs 100% on-device.** No cloud APIs, no data leaving the machine. The LLM (Ollama), speech-to-text (whisper.cpp), text-to-speech (Kokoro), memory (ChromaDB), and all OS automation (AppleScript) are local.

**Hardware target:** Apple M1 MacBook Air, 8 GB unified RAM.

---

## 2. Architecture Overview

DEMASCAS uses a **polyglot C++/Python architecture**:

### C++ Daemon (the hot path)
Location: `daemon/src/`  
Binary: `daemon/build/demascas_daemon`

The real-time voice engine. Handles:
- **Audio capture** — PortAudio, 16 kHz mono ring buffer
- **Wake-word detection** — whisper.cpp tiny model on Metal GPU
- **Command transcription** — whisper.cpp base model on Metal GPU
- **LLM inference** — streaming HTTP to Ollama via libcurl (NDJSON parsing)
- **TTS** — Kokoro 82M neural voice via sherpa-onnx (native arm64, 24 kHz float32 PCM) with macOS `say` fallback
- **Command routing** — deterministic fast-paths in C++ for shortcuts, URLs, app open/close
- **Barge-in interruption** — Dynamic VAD Ducking (polls mic during TTS, interrupts if user speaks over speaker)

The C++ daemon NEVER touches the Python GIL. Audio, STT, LLM, and TTS are all native C++. Only tool execution crosses the IPC bridge to Python.

### Python Tool Server (the hands)
Location: `server/tool_server.py`  
Runs on: `http://127.0.0.1:5001`

A Flask micro-service that exposes 21 OS-level tools + MCP extension tools. The C++ daemon calls it over HTTP to execute actions. Endpoints:
- `POST /tool` — Execute a named tool (native or MCP)
- `GET /tools` — Return all tool schemas (Ollama format)
- `GET /health` — Liveness probe with tool counts
- `GET /context?query=...` — Fetch learned context for a query
- `POST /learn` — Log interaction for learning engine

### Communication Flow

```
User speaks → PortAudio → whisper.cpp (Metal GPU)
                                │
                    ┌───────────┴───────────┐
                    │  C++ Command Router   │  (<1 µs)
                    └───────────┬───────────┘
         ┌──────────┬──────────┼──────────┬──────────┐
         ▼          ▼          ▼          ▼          ▼
      PATH 1     PATH 1.5   PATH 1.6   PATH 2    PATH 3
     Shortcut      URL      App open   Simple Q   Tool LLM
     (72 keys)  detection   /close     (fast)    (ReAct loop)
      <1 µs       <1 µs     <1 µs     ~2-3 s     ~5-8 s
         │          │          │          │          │
         └──────────┴──────────┴──────────┴──────────┘
                                │
                     Kokoro TTS (sentence-by-sentence)
                     24 kHz neural, streamed to PortAudio
```

---

## 3. State Machine

The daemon runs an infinite state machine loop:

### SLEEPING
- Continuously captures 2-second audio chunks
- Feeds to whisper.cpp tiny for wake-word detection
- Anti-echo: skips while TTS is playing or within cooldown period
- VAD gate: skips whisper if chunk is silence (RMS < threshold × 0.5)
- Wake-word variants: "demascas", "damascus", "wake up", "the mask is", "maskers" (20+ Whisper transcription variants)
- On wake: plays Glass sound, says "At your service", transitions to LISTENING

### LISTENING
- **Barge-in monitor:** While TTS is still playing, polls mic every 80ms. If user speaks louder than speaker bleed (RMS ≥ 0.15), interrupts TTS instantly and captures new command
- Captures full utterance with energy-based VAD (2s silence = end of utterance, 15s timeout)
- Whisper base model transcribes the command
- **Hallucination filtering:** Rejects known Whisper phantoms ([blank_audio], "thank you", [Music], short junk ≤2 chars, anything in brackets)
- **Dismiss detection:** "stop", "bye", "go to sleep", "shut up" → back to SLEEPING
- On valid command → PROCESSING

### PROCESSING
Routes through 5 paths (checked in order):

1. **PATH 1 — Shortcut** (<1 µs): 72+ macOS shortcuts matched by keyword. "screenshot" → ⌘⇧3, "paste" → ⌘V, "close window" → ⌘W. Executes shortcut via tool server, then does fast LLM chat for spoken feedback.

2. **PATH 1.5 — URL detection** (<1 µs): Regex-free domain detection + "dot com" → ".com" normalization. "open instagram.com in Safari" → `open_url(url="https://instagram.com", browser="Safari")`. Deterministic, bypasses LLM.

3. **PATH 1.6 — App open** (<1 µs): "open Safari", "launch Chrome", "start VS Code" → `open_application(app_name)`. 50+ app aliases (chrome→Google Chrome, vscode→Visual Studio Code, etc.). Deterministic, bypasses LLM.

4. **PATH 1.7 — App close** (<1 µs): "close Safari", "quit Chrome" → `quit_application(app_name)`. "close window"/"close tab" go to shortcut path instead.

5. **PATH 2 — Simple question** (~2-3s): "what time is it", "introduce yourself", "tell me a joke" → fast LLM streaming (2048 ctx, 100 predict, no tool schemas). Detected by prefix matching ("what", "who", "how", "introduce", etc.) and question marks.

6. **PATH 3 — Tool LLM** (~5-8s): Everything else goes through the full ReAct loop. System prompt + 21 tool schemas + memory injection + learned context → streaming Ollama (4096 ctx, 300 predict). Multi-turn: up to 5 tool calls deep.

After PROCESSING → back to LISTENING (stays awake for follow-up commands, 15s timeout).

---

## 4. Key Source Files

### C++ Daemon (`daemon/src/`)

| File | Lines | Purpose |
|------|-------|---------|
| `main.cpp` | ~533 | State machine, wake-word, hallucination filter, dismiss detection, command routing (all 5 PATHs) |
| `config.hpp` | ~100 | All tuning knobs: model names, context sizes, thresholds, TTS config, audio config |
| `ollama_client.cpp` | ~616 | System prompts, Ollama HTTP (blocking + streaming), sentence splitting, ReAct loop, history management |
| `ollama_client.hpp` | ~80 | Class declaration, SentenceCallback typedef |
| `tts.cpp` | ~583 | Dual-backend TTS: Kokoro (sherpa-onnx) + macOS `say` fallback. Double-buffered synthesis, zero-regex sanitize, play_pcm helper |
| `tts.hpp` | ~113 | TTS class: enqueue, interrupt, wait_done, is_playing, play_sound, play_pcm |
| `shortcut_map.hpp` | ~600 | 72+ shortcut definitions, extract_url(), extract_app_open(), extract_app_close(), is_simple_question(), is_heavy_query() (dead code) |
| `audio_engine.hpp/cpp` | ~200 | PortAudio ring buffer, capture_chunk(), capture_utterance(), drain(), RMS energy, device listing |
| `whisper_engine.hpp/cpp` | ~150 | whisper.cpp wrapper: init, transcribe_wake (tiny), transcribe_command (base), prompt setting |
| `tool_bridge.hpp/cpp` | ~179 | libcurl HTTP POST/GET to Python tool server, execute(), fetch_tool_schemas(), wait_for_server(), fetch_learned_context(), log_interaction() |

### Python (`server/`, `core/`, `actuators/`, `sensors/`)

| File | Lines | Purpose |
|------|-------|---------|
| `server/tool_server.py` | ~287 | Flask app, TOOL_MAP (21 entries), /tool, /tools, /health, /context, /learn endpoints, MCP boot |
| `core/toolbox.py` | ~555 | 21 JSON tool schemas in Ollama format |
| `core/memory.py` | ~100 | ChromaDB RAG: save_memory(), retrieve_memories() |
| `core/learning.py` | ~200 | 3-layer learning: episodic memory, user profile, context builder |
| `actuators/system_control.py` | ~350 | AppleScript wrappers: click_ui_element, type_text, open_application, open_url, quit_application, etc. |
| `actuators/file_system.py` | ~100 | summarize_latest_pdf, list_files_in_folder, create_file, delete_file |
| `actuators/network.py` | ~80 | search_web (DuckDuckGo), read_webpage (BeautifulSoup) |
| `sensors/vision.py` | ~120 | get_active_window_tree, list_open_windows (macOS Accessibility API) |
| `main.py` | ~50 | Legacy Python entry point — launches tool_server.py |

---

## 5. The 21 Tools

All tools are defined as JSON schemas in `core/toolbox.py` and mapped to Python functions in `server/tool_server.py`:

| # | Tool | What It Does |
|---|------|-------------|
| 1 | `open_application` | Launch/activate a macOS app by name |
| 2 | `open_url` | Open a URL in a browser (Safari, Chrome, etc.) |
| 3 | `click_ui_element` | Click a named element from the accessibility tree |
| 4 | `type_text` | Type text into focused field (with optional Enter, focus shortcut) |
| 5 | `type_text_into_element` | Type into a specific named UI element |
| 6 | `press_keyboard_shortcut` | Press any key combo (⌘, ⌥, ⌃, ⇧) |
| 7 | `quit_application` | Gracefully quit a specific app |
| 8 | `activate_application` | Bring an app to the foreground |
| 9 | `list_running_applications` | List all running GUI apps |
| 10 | `scroll_direction` | Scroll up/down/left/right |
| 11 | `drag_element` | Drag from one element to another |
| 12 | `get_active_window_tree` | Read the accessibility tree (screen content) |
| 13 | `list_open_windows` | List all open windows across apps |
| 14 | `list_files_in_folder` | List files in a directory |
| 15 | `create_file` | Create a new file at a path |
| 16 | `delete_file` | Move a file to Trash |
| 17 | `summarize_latest_pdf` | Read and summarize the latest PDF on Desktop |
| 18 | `search_web` | DuckDuckGo search |
| 19 | `read_webpage` | Fetch and extract text from a URL |
| 20 | `save_memory` | Store a fact in persistent ChromaDB |
| 21 | `retrieve_memories` | Search stored memories by query |

---

## 6. Current Config Values (`config.hpp`)

```
Model:           gemma4:31b-cloud (Ollama, cloud-hosted, zero local RAM)
Context (tools): 4096 tokens, 300 max predict
Context (fast):  2048 tokens, 100 max predict
Batch size:      256

Audio:           16 kHz mono, 512 frames/buffer
VAD threshold:   0.005 RMS
Barge-in:        0.15 RMS
Silence duration:2.0 s
Wake window:     2.0 s
Followup timeout:15.0 s
TTS cooldown:    1.0 s

TTS:             Kokoro-en-v0_19 (82M params, 24 kHz, sherpa-onnx)
                 Speaker 0 (af, default female), speed 1.0x
                 Fallback: macOS `say` (Samantha, 190 WPM)

Whisper:         tiny (wake-word) + base (command), Metal GPU
History:         4 turns max
Tool depth:      5 iterations max
```

---

## 7. System Prompt (Current — Compact)

The LLM system prompt was deliberately stripped down to ~60 tokens because Qwen 3B ignores verbose rules. Most routing is now deterministic in C++.

```
You are DEMASCAS, a macOS voice assistant. {date}, {time} IST.
RULES:
1. To ACT on macOS (click, type, scroll, read screen, manage files) → CALL a tool. Text alone does nothing.
2. Questions, math, jokes → answer directly, no tools.
3. Reply in 1-2 spoken sentences. No markdown, no URLs, no code.
```

The fast-path (simple questions) gets an even shorter prompt:
```
You are DEMASCAS, an AI assistant. Current date: {date}. Current time (IST): {time}.
Answer concisely in 1-3 spoken sentences. No markdown, no code blocks. Be natural and conversational — like JARVIS.
```

---

## 8. TTS Engine Details

### Kokoro (primary)
- 82M-parameter StyleTTS 2 model via sherpa-onnx C API
- Native arm64 binary (no Rosetta), CPU-only (GPU stays free for Whisper)
- 24 kHz float32 PCM output → PortAudio blocking stream
- **Double-buffered synthesis:** Worker thread pre-synthesizes the next sentence while the current one plays
- **Zero-regex text sanitization:** Single-pass char loop strips markdown, URLs, code blocks
- **Clause splitting:** Splits at commas/semicolons/colons after 30+ chars to reduce time-to-first-audio
- **Sentence splitting:** Splits at .!? after 10+ chars for streaming
- 2 ONNX threads (configured in init), explicit PaStreamParameters targeting default output

### macOS `say` (fallback)
- Voice: Samantha, 190 WPM
- fork+exec, PID tracking for interrupt

---

## 9. Development History (Chronological)

1. **Python prototype** — Full Python agent with Ollama, Whisper, AppleScript tools
2. **MCP integration** — Model Context Protocol support for extensible tool servers
3. **C++ daemon migration** — Bare-metal rewrite: PortAudio, whisper.cpp, libcurl streaming, state machine
4. **RAM optimization** — Single ChromaDB client, bounded collections, batched profile writes
5. **Learning engine** — 3-layer system: episodic memory, user profile, context builder
6. **Real-time voice architecture** — Streaming LLM + sentence-by-sentence TTS pipeline
7. **Kokoro TTS integration** — sherpa-onnx native arm64, replaced macOS `say`
8. **Tool additions** — open_url, type_text, create_file, delete_file tools
9. **Sleeper Agent (dual-model)** — Vanguard 3B + Heavyweight 7B. Later removed 7B (too heavy for 8 GB M1)
10. **keep_alive bug fix** — Was sending string `"-1"` instead of integer `-1`, caused empty Ollama responses
11. **TTS optimizations** — Zero-regex sanitize, comma/clause splitting, double-buffered synthesis, explicit PaStreamParameters
12. **Barge-in (Dynamic VAD Ducking)** — Polls mic during TTS at 80ms intervals, interrupts if user speaks over speaker
13. **Agentic behavior overhaul** — System prompt slashed from ~1500 → ~60 tokens. Added deterministic C++ fast-paths for app open/close (50+ aliases). Fixed `is_simple_question()` missing "introduce" and other conversational prefixes. Tool server restarted with all 21 tools.

---

## 10. Known Issues & Current State

### Working
- Wake-word detection (20+ Whisper transcription variants)
- Shortcut fast-path (72+ macOS shortcuts, <1 µs)
- URL detection fast-path (deterministic, <1 µs)
- App open fast-path (50+ aliases, deterministic, <1 µs)
- App close fast-path (deterministic, <1 µs)
- Simple question detection (wide prefix matching + question marks)
- Streaming LLM + sentence TTS pipeline
- Barge-in interruption
- Hallucination filtering
- Dismiss detection
- Tool server with 21 tools
- ChromaDB persistent memory
- Learning engine (episodic + profile)
- Kokoro neural TTS with double-buffered synthesis

### Potential Issues
- **Tool path latency:** ~2-4s for full ReAct loop with gemma4:31b-cloud (network latency). The system prompt + 21 tool schemas + memory context consume significant context window
- **LLM tool reliability:** Qwen 3B sometimes generates text describing an action instead of emitting a `tool_calls` JSON. The compact system prompt + deterministic fast-paths mitigate this but complex multi-step commands still depend on LLM behavior
- **Bluetooth speaker latency:** JBL Clip 4 (default output) has a Bluetooth handshake delay on first audio
- **Whisper hallucinations:** Tiny model hallucinates on background noise. Extensive filter list mitigates but doesn't eliminate
- **8 GB RAM ceiling:** With cloud LLM (0 GB local) + Whisper (222 MB) + Kokoro (335 MB) + ChromaDB (80 MB), ~6.5 GB left for macOS. Cloud model provides 32B intelligence with zero local RAM footprint.

---

## 11. Project File Tree

```
DEMASCAS/
├── main.py                          # Legacy Python entry — launches tool server
├── requirements.txt                 # Python deps
├── README.md                        # Full documentation (634 lines)
├── core/
│   ├── toolbox.py                   # 21 JSON tool schemas
│   ├── memory.py                    # ChromaDB save/retrieve
│   ├── agent.py                     # Legacy Python agent (unused by daemon)
│   └── learning.py                  # Episodic memory + user profile + context builder
├── sensors/
│   ├── vision.py                    # macOS Accessibility Tree extraction
│   └── audio.py                     # Legacy Python audio (unused by daemon)
├── actuators/
│   ├── system_control.py            # 11 OS control tools (AppleScript)
│   ├── file_system.py               # 4 file tools
│   ├── network.py                   # 2 web tools
│   └── speech.py                    # Legacy Python TTS (unused by daemon)
├── server/
│   └── tool_server.py               # Flask on :5001, 21 tools + MCP + learning
├── mcp/
│   ├── client.py                    # Pure-Python MCP JSON-RPC client
│   ├── manager.py                   # Server lifecycle, RSS watchdog
│   ├── bridge.py                    # Hybrid native/MCP router
│   └── config.py                    # MCP server config loader
├── daemon/
│   ├── CMakeLists.txt               # Build config
│   ├── src/
│   │   ├── main.cpp                 # State machine + command routing
│   │   ├── config.hpp               # All tuning knobs
│   │   ├── ollama_client.cpp/hpp    # Streaming Ollama + ReAct loop
│   │   ├── tts.cpp/hpp              # Kokoro TTS + macOS say fallback
│   │   ├── shortcut_map.hpp         # 72+ shortcuts + URL/app detection + simple Q detection
│   │   ├── audio_engine.cpp/hpp     # PortAudio capture + VAD
│   │   ├── whisper_engine.cpp/hpp   # whisper.cpp wrapper
│   │   └── tool_bridge.cpp/hpp      # HTTP bridge to Python tool server
│   ├── models/                      # Auto-downloaded by build.sh
│   │   ├── ggml-tiny.bin            # 75 MB — wake-word
│   │   ├── ggml-base.bin            # 147 MB — commands
│   │   ├── kokoro-en-v0_19/         # 335 MB — Kokoro TTS
│   │   └── sherpa-onnx/             # Pre-built shared library
│   └── build/
│       └── demascas_daemon          # Compiled binary
├── scripts/
│   ├── build.sh                     # One-command build
│   └── run.sh                       # One-command run
└── docs/
    ├── COPILOT_CONTEXT.md           # Original Copilot context (outdated)
    └── CLAUDE_CONTEXT.md            # ← This file
```

---

## 12. How to Run

```bash
# Terminal 1: Start Ollama
ollama serve

# Terminal 2: Start Python tool server
cd ~/Desktop/OS_RAG/DEMASCAS
source ../venv/bin/activate  # if using venv
python3 server/tool_server.py

# Terminal 3: Start C++ daemon
cd ~/Desktop/OS_RAG/DEMASCAS/daemon
./build/demascas_daemon
```

Or use the one-command launcher: `./scripts/run.sh`

---

## 13. Key Design Decisions & Rationale

| Decision | Why |
|----------|-----|
| C++ daemon for hot path | Python GIL blocks audio. C++ gives real-time <5ms interrupt latency |
| Deterministic fast-paths over LLM | Qwen 3B unreliably calls tools. C++ matching is instant and 100% correct |
| Compact system prompt (~60 tokens) | 3B model ignores verbose rules. Fewer rules = higher compliance |
| Single model (3B only, no 7B) | 7B caused 3-4s cold-start and RAM pressure on 8 GB M1 |
| keep_alive=-1 (integer) | Pins model in RAM forever. String "-1" caused Ollama parse errors |
| Kokoro TTS over macOS `say` | Neural quality, streaming-friendly, native arm64 |
| Double-buffered TTS | Pre-synthesize next sentence during playback → lower perceived latency |
| Barge-in via VAD ducking | No hardware AEC available. Elevated RMS threshold (0.15) separates user voice from speaker bleed |
| Tool schemas fetched at startup | C++ daemon GETs /tools once, caches for session. No per-request overhead |
| Flask over FastAPI | Simpler, synchronous, adequate for localhost IPC. No async complexity needed |
| ChromaDB for memory | Lightweight vector DB, runs in-process, no server needed |

---

## 14. What Needs Work Next

- **Multi-step agentic behavior:** "Open Safari, go to Gmail, compose an email about X" — requires chaining multiple tool calls reliably
- **Streaming latency reduction:** Tool path still takes ~5-8s. Could investigate smaller context window, fewer tool schemas per request, or tool filtering
- **Screen reading integration:** `get_active_window_tree` exists but the LLM rarely uses it proactively. Could add auto-screen-read before click actions
- **Error recovery:** When a tool fails, the LLM should retry or try an alternative approach
- **Voice personalization:** Kokoro supports 11 speakers — could let user choose
- **Conversation context across sessions:** Currently resets on daemon restart. Could persist to disk
