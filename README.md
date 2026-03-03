# DEMASCAS

> **Decision Engineering Machine Algorithm for System Control and Action Sequencing**

A fully local, privacy-first, autonomous OS-level AI agent for macOS — like JARVIS for your Mac. DEMASCAS can **see** the screen via OS accessibility trees, **hear** commands via local speech-to-text, and **execute** actions via native AppleScript function calling. All processing runs 100 % on-device — no data ever leaves your machine.

**Architecture:** Polyglot C++ / Python — bare-metal C++ daemon for the hot path (audio → STT → LLM → TTS) with a lightweight Python tool server for OS actions, memory, and learning.

---

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running DEMASCAS](#running-demascas)
  - [Quick Start (One Command)](#quick-start-one-command)
  - [Manual Start (Two Terminals)](#manual-start-two-terminals)
- [Usage Guide](#usage-guide)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [17 Built-in Tools](#17-built-in-tools)
- [72+ macOS Shortcuts](#72-macos-shortcuts)
- [MCP Integration](#mcp-integration)
- [Continuous Learning](#continuous-learning)
- [Configuration & Tuning](#configuration--tuning)
- [Performance & Optimization](#performance--optimization)
- [Troubleshooting](#troubleshooting)
- [Adding a New Tool](#adding-a-new-tool)
- [License](#license)

---

## Features

- 🧠 **Local LLM** — Ollama `qwen2.5:3b` with streaming inference + native tool calling
- 🗣️ **Streaming TTS** — Sentence-by-sentence speech; first words spoken while LLM still generates
- 👁️ **Screen awareness** — Reads macOS accessibility trees, not screenshots
- 🛠️ **17 OS-level tools** — Click, type, open apps, browse the web, manage files
- ⌨️ **72+ keyboard shortcuts** — Instant execution without LLM round-trip
- 🧲 **Persistent memory** — ChromaDB RAG for long-term recall
- 📚 **Continuous learning** — Episodic memory + user profile + correction tracking
- 🌐 **Web browsing** — DuckDuckGo search + webpage reading
- 🔌 **MCP support** — Extensible via Model Context Protocol servers (SQLite, GitHub, etc.)
- 🔒 **100 % local** — Zero cloud dependencies, total privacy
- ⚡ **Apple Silicon optimised** — Metal GPU, whisper.cpp, PortAudio, ~4.7 GB total RAM

---

## Prerequisites

| Requirement | How to Get It |
|-------------|--------------|
| **macOS** (Apple Silicon recommended) | — |
| **Ollama** | `brew install ollama` or [ollama.ai](https://ollama.ai) |
| **Python 3.10+** | `brew install python@3.12` |
| **Homebrew** | [brew.sh](https://brew.sh) |
| **CMake 3.20+** | `brew install cmake` |
| **PortAudio** | `brew install portaudio` |
| **pkg-config** | `brew install pkg-config` |
| **Microphone** | Built-in or external |

### macOS Permissions Required

Go to **System Settings → Privacy & Security** and grant:

| Permission | Why |
|---|---|
| **Accessibility** → Terminal / iTerm | UI automation (click, type, read screen) |
| **Microphone** → Terminal / iTerm | Voice capture |
| **Screen Recording** → Terminal / iTerm | Screenshots (optional) |

---

## Installation

### 1. Clone & enter the project

```bash
cd ~/Desktop
git clone <your-repo-url> OS_RAG   # or copy the folder
cd OS_RAG/DEMASCAS
```

### 2. Pull the Ollama model

```bash
ollama serve &                     # start Ollama in background
ollama pull qwen2.5:3b             # ~1.5 GB download
```

### 3. Create a Python virtual environment

```bash
python3 -m venv ../venv
source ../venv/bin/activate
pip install -r requirements.txt
```

### 4. Build the C++ daemon

The build script handles everything — dependencies, model downloads, and compilation:

```bash
chmod +x scripts/build.sh scripts/run.sh
./scripts/build.sh
```

This will:
1. Check / install CMake, PortAudio, pkg-config via Homebrew
2. Download whisper GGML models (~75 MB tiny + ~147 MB base) to `daemon/models/`
3. Install Flask for the Python tool server
4. Compile the C++ daemon via CMake (Release, ARM64)

On success you'll see:

```
✅  Build complete!
Binary:  daemon/build/demascas_daemon
```

---

## Running DEMASCAS

DEMASCAS runs as **two processes** — a C++ daemon (voice engine) and a Python tool server (OS actions).

### Quick Start (One Command)

```bash
# Make sure Ollama is running
ollama serve &

# Activate the venv
source ../venv/bin/activate

# Launch both processes
./scripts/run.sh
```

You'll see:

```
[*] Starting Python tool server (localhost:5001)...
[*] Starting C++ daemon...

╔══════════════════════════════════════════╗
║   DEMASCAS — Bare-Metal C++ Daemon       ║
║   Apple Silicon • whisper.cpp • Ollama    ║
╚══════════════════════════════════════════╝

[✓] Tool server ready (17 tools on localhost:5001)
[✓] Loaded 17 tool schemas
[✓] Model prewarmed in ~4 s — ready.
[✓] Microphone opened (16 kHz mono, PortAudio)
[✓] Whisper models loaded in ~14 s

[*] DEMASCAS is ready.  Say "demascas" to activate!
```

**Stop:** Press `Ctrl+C` — the script automatically kills both processes.

---

### Manual Start (Two Terminals)

If you prefer to run the processes separately (useful for debugging):

**Terminal 1 — Python tool server:**

```bash
cd ~/Desktop/OS_RAG/DEMASCAS
source ../venv/bin/activate
python main.py
```

```
========================================
   DEMASCAS SYSTEM CORE INITIALIZED
========================================
[*] Starting Python tool server...
[*] The C++ daemon connects to this server over HTTP.
 * Running on http://127.0.0.1:5001
```

**Terminal 2 — C++ daemon (voice engine):**

```bash
cd ~/Desktop/OS_RAG/DEMASCAS
./daemon/build/demascas_daemon
```

```
[✓] Microphone opened (16 kHz mono)
[✓] Whisper models loaded
[*] Listening...
```

---

## Usage Guide

### Interacting with DEMASCAS

1. **Wake it up** — Say **"Hey Damascus"**, **"demascas"**, or **"wake up"**
2. **Give a command** — Speak naturally after the greeting chime
3. **Interrupt anytime** — Start talking while DEMASCAS is speaking; it stops immediately and listens
4. **Multi-turn conversation** — Keep talking; it stays awake for 15 seconds between turns
5. **It goes back to sleep** — After 15 seconds of silence, it says goodbye and sleeps

### Example Commands

| What you say | What happens |
|---|---|
| "Open Safari" | Launches Safari via AppleScript |
| "Close this window" | Sends ⌘W to the active app |
| "Take a screenshot" | Sends ⌘⇧3 |
| "Search the web for best restaurants in Delhi" | Runs a DuckDuckGo search and reads results aloud |
| "What's on my screen right now?" | Reads the accessibility tree of the active window |
| "Remember that my meeting is at 3 PM tomorrow" | Saves to persistent ChromaDB memory |
| "What did I tell you about my meeting?" | Retrieves from memory |
| "Open YouTube in Safari" | Opens the URL in the browser |
| "Summarize my latest PDF" | Reads the most recent PDF on the Desktop |
| "List all running apps" | Reads out every running application |
| "Undo that" | Sends ⌘Z instantly (shortcut path, no LLM) |
| "Minimize" | Sends ⌘M instantly |
| "What time is it?" | Answers directly (fast path, no tools) |
| "My name is Daksh" | Saved to user profile — all future responses personalized |

### Three Processing Paths

DEMASCAS uses a tiered processing strategy for maximum speed:

1. **⚡ Shortcut path** (~0 ms) — 72+ macOS shortcuts matched by keyword in C++; bypasses the LLM entirely
2. **💬 Fast path** (~300–800 ms) — Simple questions streamed directly, no tool calls
3. **🛠️ Tool path** (~1–3 s) — Complex requests go through the full ReAct loop with streaming

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│             C++ DAEMON (bare metal, hot path)                │
│                                                              │
│  PortAudio ──→ VAD ──→ whisper.cpp ──→ Ollama (stream) ───┐ │
│  ring buffer   energy   tiny+base      libcurl NDJSON     │ │
│  16 kHz mono   ~32ms    Metal GPU      first token ~400ms │ │
│                                                            │ │
│  ┌── TTS (streaming, sentence queue) ←────────────────────┘ │
│  │   fork/exec `say` │ PID tracking │ interrupt <5ms        │
│  └───────────────────┘                                       │
│       │                                                      │
│       │ HTTP localhost:5001                                   │
└───────┼──────────────────────────────────────────────────────┘
        ▼
┌─────────────────────────────────────────────────────────────┐
│          PYTHON TOOL SERVER (Flask, 17 tools)                │
│                                                              │
│  /tool   → execute a native or MCP tool                      │
│  /tools  → list all available tool schemas                   │
│  /health → liveness probe                                    │
│  /context → learning context (episodes + profile)            │
│  /learn   → log interaction for learning                     │
│                                                              │
│  ┌──────────────┐  ┌──────────┐  ┌─────────────────────┐    │
│  │ Native Tools │  │ MCP      │  │ Learning Engine     │    │
│  │ (OS control, │  │ Bridge   │  │ (episodes, profile, │    │
│  │  files, web) │  │ (stdio)  │  │  context builder)   │    │
│  └──────────────┘  └──────────┘  └─────────────────────┘    │
│  ┌──────────────┐  ┌──────────┐                              │
│  │ ChromaDB     │  │ Vision   │                              │
│  │ (shared      │  │ (macOS   │                              │
│  │  client)     │  │  a11y)   │                              │
│  └──────────────┘  └──────────┘                              │
└─────────────────────────────────────────────────────────────┘
                          │
              ~/.demascas/ (persistent storage)
    ┌────────────────────────────────────────────┐
    │  memory/             → ChromaDB vectors     │
    │  user_profile.json   → learned preferences  │
    │  mcp_servers.json    → MCP server config    │
    └────────────────────────────────────────────┘
```

---

## Project Structure

```
DEMASCAS/
│
├── main.py                      # Entry point — launches the Python tool server
├── requirements.txt             # Python dependencies (tool server only)
├── README.md                    # ← You are here
│
├── core/                        # Core Python modules
│   ├── toolbox.py               #   17 Ollama-compatible tool schemas
│   ├── memory.py                #   ChromaDB RAG persistent memory
│   └── learning.py              #   Continuous learning engine (3 layers)
│
├── sensors/                     # The "Eyes" (Python)
│   └── vision.py                #   macOS Accessibility Tree extraction
│
├── actuators/                   # The "Hands" (Python)
│   ├── system_control.py        #   Click, type, open, shortcut, quit, scroll, drag
│   ├── file_system.py           #   PDF summary, file listing
│   └── network.py               #   Web search (DuckDuckGo) + webpage reading
│
├── mcp/                         # Model Context Protocol (extensible tools)
│   ├── client.py                #   Pure-Python MCP JSON-RPC client
│   ├── manager.py               #   Server lifecycle, RSS watchdog
│   ├── bridge.py                #   Hybrid native/MCP tool router
│   ├── config.py                #   MCP server configuration loader
│   └── servers/                 #   Built-in MCP servers
│       └── example_sqlite.py    #     SQLite search server
│
├── daemon/                      # C++ Bare-Metal Daemon (voice engine)
│   ├── CMakeLists.txt           #   Build config (whisper.cpp, nlohmann/json)
│   ├── models/                  #   Whisper GGML models (auto-downloaded)
│   │   ├── ggml-tiny.bin        #     ~75 MB — wake-word detection
│   │   └── ggml-base.bin        #     ~147 MB — command transcription
│   └── src/
│       ├── main.cpp             #   State machine (SLEEPING → LISTENING → PROCESSING)
│       ├── config.hpp           #   All tuning knobs in one place
│       ├── audio_engine.hpp/cpp #   PortAudio 16 kHz capture + energy-based VAD
│       ├── whisper_engine.hpp/cpp # whisper.cpp integration (Metal GPU)
│       ├── ollama_client.hpp/cpp #  Streaming Ollama (libcurl NDJSON, sentence callbacks)
│       ├── tool_bridge.hpp/cpp  #   HTTP bridge to Python tool server
│       ├── tts.hpp/cpp          #   Streaming TTS — sentence queue, worker thread, interrupt
│       └── shortcut_map.hpp     #   72+ shortcuts (header-only)
│
├── server/                      # Python Tool Micro-service
│   └── tool_server.py           #   Flask on localhost:5001, 17 endpoints + MCP + learning
│
├── scripts/                     # Build & Run helpers
│   ├── build.sh                 #   One-command build (deps + models + compile)
│   └── run.sh                   #   One-command run (tool server + C++ daemon)
│
└── docs/
    └── COPILOT_CONTEXT.md       #   AI coding assistant context
```

---

## 17 Built-in Tools

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 1 | `click_ui_element` | system_control | Click a UI element by name |
| 2 | `type_text_into_element` | system_control | Type text into the focused field |
| 3 | `open_application` | system_control | Launch any macOS app by name |
| 4 | `press_keyboard_shortcut` | system_control | Press any key combo (⌘, ⌥, ⌃, ⇧) |
| 5 | `quit_application` | system_control | Gracefully quit a specific app |
| 6 | `activate_application` | system_control | Bring an app to the foreground |
| 7 | `list_running_applications` | system_control | List all running GUI applications |
| 8 | `scroll_direction` | system_control | Scroll up/down/left/right in an app |
| 9 | `drag_element` | system_control | Drag from one element to another |
| 10 | `get_active_window_tree` | vision | Read the accessibility tree (screen content) |
| 11 | `list_open_windows` | vision | List all open windows across all apps |
| 12 | `list_files_in_folder` | file_system | List files in a directory |
| 13 | `summarize_latest_pdf` | file_system | Read and summarize the latest PDF on Desktop |
| 14 | `search_web` | network | Search DuckDuckGo and return results |
| 15 | `read_webpage` | network | Fetch and extract text from a URL |
| 16 | `save_memory` | memory | Store a fact in persistent ChromaDB |
| 17 | `retrieve_memories` | memory | Search stored memories by query |

---

## 72+ macOS Shortcuts

Shortcuts are matched in C++ and executed **instantly** without touching the LLM. Examples:

| Voice Command | Shortcut Fired |
|---|---|
| "Close this window" | ⌘W |
| "Close the tab" | ⌘W |
| "Quit this app" | ⌘Q |
| "Take a screenshot" | ⌘⇧3 |
| "Screenshot selection" | ⌘⇧4 |
| "Copy" / "Paste" / "Cut" | ⌘C / ⌘V / ⌘X |
| "Undo" / "Redo" | ⌘Z / ⌘⇧Z |
| "Save" / "Save as" | ⌘S / ⌘⇧S |
| "Find" / "Find and replace" | ⌘F / ⌘⌥F |
| "New tab" / "New window" | ⌘T / ⌘N |
| "Minimize" / "Full screen" | ⌘M / ⌘⌃F |
| "Select all" | ⌘A |
| "Print" | ⌘P |
| "Lock screen" | ⌘⌃Q |
| "Force quit" | ⌘⌥⎋ |
| "Switch app" | ⌘⇥ |
| "Spotlight" | ⌘Space |
| "Zoom in" / "Zoom out" | ⌘+ / ⌘- |
| "Refresh" / "Reload" | ⌘R |
| "Show desktop" | F11 |

> Commands like "Open Safari" or "Summarize my PDF" are **not** matched as shortcuts — they route through the LLM tool-calling path for proper handling.

---

## MCP Integration

DEMASCAS supports the **Model Context Protocol** for extensible tool servers.

Configure servers in `~/.demascas/mcp_servers.json`:

```json
{
  "servers": [
    {
      "name": "sqlite",
      "command": "python3",
      "args": ["mcp/servers/example_sqlite.py"],
      "max_rss_mb": 100
    }
  ]
}
```

**Constraints (bare-metal optimized):**
- Max **3** concurrent MCP servers
- RSS watchdog kills any server exceeding its `max_rss_mb` budget
- Node.js (`npx`/`node`) commands are **blocked** — Python/Rust servers only
- Native tools are always checked first (~0 µs); MCP is fallback (~5 ms)

MCP servers boot automatically when the tool server starts. Check status at `http://localhost:5001/mcp/status`.

---

## Continuous Learning

DEMASCAS learns from every interaction via three layers in `core/learning.py`:

| Layer | What It Learns | Storage | Budget |
|---|---|---|---|
| **Episodic Memory** | Every command → path taken, tools used, result | ChromaDB `demascas_episodes` | ≤500 entries |
| **User Profile** | Name, preferences, app usage, corrections | `~/.demascas/user_profile.json` | <50 KB |
| **Context Builder** | Injects top-3 similar episodes + profile into LLM prompt | Transient (in-memory) | ~200 tokens |

**Zero overhead:** Reuses the same ChromaDB client as RAG memory. Profile writes are batched (1 flush per 5 commands). No extra LLM calls, no background threads.

The C++ daemon calls `/context?query=...` before each LLM invocation and `/learn` after each command completes.

---

## Configuration & Tuning

All tuning knobs live in a single header: `daemon/src/config.hpp`

| Knob | Default | Description |
|------|---------|-------------|
| `OLLAMA_MODEL` | `"qwen2.5:3b"` | Ollama model to use |
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `VAD_ENERGY_THRESHOLD` | `0.005f` | Voice activity detection sensitivity |
| `SILENCE_DURATION` | `2.0f` | Seconds of silence before end-of-utterance |
| `WAKE_WORD` | `"demascas"` | Activation keyword |
| `TTS_VOICE` | `"Daniel"` | macOS voice (British English) |
| `TTS_RATE` | `180` | Words per minute |
| `MAX_HISTORY` | `4` | Conversation turns kept in context |
| `MAX_TOOL_DEPTH` | `5` | Max ReAct loop iterations |

After changing config, rebuild with `./scripts/build.sh`.

---

## Performance & Optimization

### Latency Budget (per command)

| Phase | Time | Implementation |
|---|---|---|
| VAD speech detection | ~32 ms | 512-sample energy threshold (C++) |
| STT transcription | ~200 ms | whisper.cpp tiny on Metal GPU |
| LLM first token | ~400 ms | Streaming Ollama via libcurl NDJSON |
| TTS first word | ~500 ms | Sentence queue starts before LLM finishes |
| Tool execution | ~5-2000 ms | AppleScript/subprocess (OS-dominated) |
| Learning overhead | <5 ms | ChromaDB search + in-memory profile update |

### RAM Budget (8 GB Apple Silicon)

| Component | RAM |
|---|---|
| Qwen 2.5 3B (Ollama) | ~2 GB |
| whisper.cpp tiny | ~75 MB |
| C++ daemon | ~15 MB |
| ChromaDB (shared client + embeddings) | ~80 MB |
| Python tool server | ~30 MB |
| MCP servers (if any, max 3) | ≤300 MB |
| **Total** | **~2.5 GB** (leaves ~5.5 GB for macOS) |

### Key Optimizations

- **Single ChromaDB client** — memory.py and learning.py share one embedding model (~80 MB saved)
- **Streaming everything** — LLM tokens stream to TTS sentence queue; no waiting for full response
- **Barge-in interrupt** — VAD detects speech → kills TTS PID in <5 ms, cancels Ollama stream
- **Batched profile writes** — 1 disk flush per 5 interactions (80% fewer SSD writes)
- **No Python on the hot path** — Audio, STT, LLM, TTS all run in C++
- **Bounded collections** — Episodes capped at 500, profile fields capped, no unbounded growth
- **5-minute keep-alive** — Ollama auto-unloads model after idle (frees ~2 GB)

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Ollama not running"** | Run `ollama serve` in a separate terminal |
| **"Model not found"** | Run `ollama pull qwen2.5:3b` |
| **No microphone input** | System Settings → Privacy → Microphone → enable Terminal |
| **"Accessibility not allowed"** | System Settings → Privacy → Accessibility → enable Terminal / VS Code |
| **Port 5001 in use** | Kill it: `lsof -ti:5001 \| xargs kill`, then retry |
| **Port 11434 in use** | Another Ollama instance; `killall ollama` then `ollama serve` |
| **Daemon not built** | Run `./scripts/build.sh` first |
| **CMake not found** | `brew install cmake` |
| **PortAudio not found** | `brew install portaudio` |
| **Whisper models missing** | `./scripts/build.sh` auto-downloads them |
| **"Permission denied" on scripts** | `chmod +x scripts/build.sh scripts/run.sh` |
| **Wrong app closed** | DEMASCAS distinguishes "close window" (⌘W) from "quit app" (⌘Q) |
| **Shortcut fires for "Open Safari"** | By design — "open" commands route to the LLM tool path |
| **Tool server errors in daemon** | Check Terminal 1 logs; ensure `python main.py` is running |
| **Learning not working** | Check `~/.demascas/` exists; run `cat ~/.demascas/user_profile.json` |

---

## Adding a New Tool

1. **Write the Python function** in the appropriate `actuators/` or `sensors/` module
2. **Add the JSON tool schema** to `core/toolbox.py`
3. **Register the function** in `server/tool_server.py` → `TOOL_MAP`
4. **Restart the tool server** — `Ctrl+C` on Terminal 1, then `python main.py` again
5. The C++ daemon picks up new tools automatically from `/tools` on its next request

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| LLM | Ollama + `qwen2.5:3b` (3.1B params, Q4_K_M) |
| STT | whisper.cpp (tiny + base, Metal GPU) |
| TTS | macOS `say` — sentence queue with interrupt |
| Audio | PortAudio 16 kHz ring buffer (C++) |
| Vision | macOS Accessibility API (JXA / AppleScript) |
| Memory | ChromaDB (persistent at `~/.demascas/memory/`) |
| Learning | Episodic memory + user profile (persistent at `~/.demascas/`) |
| Web | DuckDuckGo (ddgs) + BeautifulSoup4 |
| IPC | Flask micro-service on `localhost:5001` |
| MCP | Pure-Python JSON-RPC 2.0 client (zero deps) |
| Build | CMake + FetchContent (whisper.cpp, nlohmann/json) |
| Hardware | Apple Silicon (M1/M2/M3), unified memory |

---

## License

Private project — all rights reserved.
