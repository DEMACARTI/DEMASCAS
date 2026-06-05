# DEMASCAS — Full Technical Context Document

> **Purpose:** Comprehensive handoff document for AI/developer onboarding.
> **Last Updated:** 2025-01-XX (Session 7 — web agent + 7 bug fixes shipped)
> **Repository:** https://github.com/DEMACARTI/DEMASCAS (master branch)
> **Workspace:** `/Users/dakshrathore/Desktop/OS_RAG/DEMASCAS/`

---

## Table of Contents

1. [What Is DEMASCAS](#1-what-is-demascas)
2. [Hardware Target & Constraints](#2-hardware-target--constraints)
3. [Architecture Overview](#3-architecture-overview)
4. [Process Topology (4 Processes)](#4-process-topology-4-processes)
5. [C++ Daemon — Deep Dive](#5-c-daemon--deep-dive)
   - 5.1 [State Machine](#51-state-machine)
   - 5.2 [Audio Pipeline](#52-audio-pipeline)
   - 5.3 [Whisper Engine (ASR)](#53-whisper-engine-asr)
   - 5.4 [Transcription Normalization](#54-transcription-normalization)
   - 5.5 [Command Routing (8 Paths)](#55-command-routing-8-paths)
   - 5.6 [LLM Client — System Prompts & Rules](#56-llm-client--system-prompts--rules)
   - 5.7 [ReAct Tool Loop (PATH 3)](#57-react-tool-loop-path-3)
   - 5.8 [Multi-Step Planner (PATH 2.5)](#58-multi-step-planner-path-25)
   - 5.9 [Deep Intent (PATH 4)](#59-deep-intent-path-4)
   - 5.10 [TTS Engine (Kokoro)](#510-tts-engine-kokoro)
   - 5.11 [Tool Bridge (HTTP Client)](#511-tool-bridge-http-client)
   - 5.12 [Configuration](#512-configuration)
6. [Python Tool Server — Deep Dive](#6-python-tool-server--deep-dive)
   - 6.1 [Flask Server & Endpoints](#61-flask-server--endpoints)
   - 6.2 [TOOL_MAP (88 Tools)](#62-tool_map-88-tools)
   - 6.3 [ACTIVE_CONTEXT (Cross-Turn State)](#63-active_context-cross-turn-state)
   - 6.4 [Tool Router (Smart Filtering)](#64-tool-router-smart-filtering)
   - 6.5 [Tool Schemas (core/toolbox.py)](#65-tool-schemas-coretoolboxpy)
7. [Actuators — The Hands](#7-actuators--the-hands)
   - 7.1 [system_control.py — AppleScript UI Automation](#71-system_controlpy--applescript-ui-automation)
   - 7.2 [browser.py — Brave Browser JS Injection](#72-browserpy--brave-browser-js-injection)
   - 7.3 [web_agent.py — Autonomous Browsing](#73-web_agentpy--autonomous-browsing)
   - 7.4 [communication.py — JARVIS-Grade Comms](#74-communicationpy--jarvis-grade-comms)
   - 7.5 [smart_search.py — Google/DDG Search](#75-smart_searchpy--googleddg-search)
   - 7.6 [file_system.py — File Operations](#76-file_systempy--file-operations)
   - 7.7 [network.py — Web Scraping](#77-networkpy--web-scraping)
8. [Sensors — The Eyes](#8-sensors--the-eyes)
   - 8.1 [vision.py — Accessibility Tree + VLM](#81-visionpy--accessibility-tree--vlm)
9. [Core Intelligence Layer](#9-core-intelligence-layer)
   - 9.1 [memory.py — ChromaDB RAG](#91-memorypy--chromadb-rag)
   - 9.2 [learning.py — Continuous Learning Engine](#92-learningpy--continuous-learning-engine)
   - 9.3 [knowledge.py — Procedures, Facts, Corrections](#93-knowledgepy--procedures-facts-corrections)
   - 9.4 [screen_context.py — VLM Pre-Digestion](#94-screen_contextpy--vlm-pre-digestion)
   - 9.5 [prompt_templates.py — Task-Specific Prompts](#95-prompt_templatespy--task-specific-prompts)
   - 9.6 [utils.py — Path Resolution](#96-utilspy--path-resolution)
10. [Memory Subsystem](#10-memory-subsystem)
    - 10.1 [web_memory.py — Per-Domain Browsing Memory](#101-web_memorypy--per-domain-browsing-memory)
11. [MCP (Model Context Protocol)](#11-mcp-model-context-protocol)
12. [Build System](#12-build-system)
13. [Launch Sequence](#13-launch-sequence)
14. [Data Flow — End to End](#14-data-flow--end-to-end)
15. [Persistent State & File Locations](#15-persistent-state--file-locations)
16. [Known Issues & Limitations](#16-known-issues--limitations)
17. [Extension Points](#17-extension-points)
18. [Quick Reference — All Files](#18-quick-reference--all-files)

---

## 1. What Is DEMASCAS

DEMASCAS is a **voice-first AI desktop assistant** for macOS — a JARVIS-like system that listens through the microphone, understands natural language commands, controls the operating system, automates browsers, manages files, composes emails, performs web research, and learns from every interaction.

It is a **polyglot C++/Python system**: a bare-metal C++ daemon handles the real-time audio pipeline (capture → speech-to-text → LLM → text-to-speech), while a Python Flask micro-service provides 88 native tools for OS automation, browser control, email, memory, and learning.

**Key differentiator:** Everything runs locally on-device. No cloud APIs for inference. The LLM (Phi-4-mini), ASR (whisper.cpp), and TTS (Kokoro) all run on Apple Silicon with Metal GPU acceleration.

---

## 2. Hardware Target & Constraints

| Spec | Value |
|------|-------|
| **Machine** | Apple M1 MacBook Air |
| **RAM** | 8 GB unified (shared CPU/GPU) |
| **OS** | macOS 14+ (Sonoma) |
| **Architecture** | arm64 (Apple Silicon) |

**RAM Budget (approximate):**

| Component | RAM Usage |
|-----------|-----------|
| macOS + desktop apps | ~3 GB |
| Phi-4-mini-instruct-4bit (MLX) | ~2.0 GB |
| whisper.cpp tiny + base (Metal) | ~180 MB |
| Kokoro TTS (sherpa-onnx ONNX Runtime) | ~100 MB |
| ChromaDB + all-MiniLM-L6-v2 embeddings | ~80 MB |
| Python tool server + Flask | ~60 MB |
| C++ daemon runtime | ~20 MB |
| **Total** | **~5.4 GB** |
| **Remaining for VLM** | ~2.6 GB |

The Vision Language Model (Qwen2.5-VL-3B) uses ~1.7 GB and is **disabled by default** (lazy-load mode). When both text LLM and VLM are loaded simultaneously, the system swaps and inference degrades to ~13s. The VLM can be started manually when needed.

**Hard Rules:**
- Node.js MCP servers are blocked (80-150 MB each → would kill Qwen's VRAM)
- Max 3 MCP servers total
- VLM lazy-loaded, not persistent
- ChromaDB client shared across all modules (one embedding model instance)
- All audio is 16 kHz mono float32 (minimum for Whisper)

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    C++ DAEMON (bare-metal)                       │
│                                                                 │
│  PortAudio ─→ WhisperEngine ─→ LLMClient ─→ TTS (Kokoro)      │
│  (16kHz)      (tiny/base)      (MLX HTTP)    (sherpa-onnx)     │
│       │            │                │              │            │
│       │     ┌──────┘                │              │            │
│       │     │  transcription        │ SSE stream   │ PCM audio │
│       │     ▼                       ▼              ▼            │
│  ┌─────────────────────────────────────────────────────┐       │
│  │              STATE MACHINE                           │       │
│  │  SLEEPING → LISTENING → PROCESSING → LISTENING       │       │
│  │                          ↓        ↓                  │       │
│  │                    CONFIRMING  CLARIFYING             │       │
│  └─────────────────────────────────────────────────────┘       │
│                          │                                      │
│                   ToolBridge (HTTP)                              │
│                          │                                      │
└──────────────────────────┼──────────────────────────────────────┘
                           │ localhost:5001
┌──────────────────────────┼──────────────────────────────────────┐
│              PYTHON TOOL SERVER (Flask)                          │
│                          │                                      │
│  ┌───────────────────────┼───────────────────────────────┐     │
│  │              TOOL ROUTER                               │     │
│  │  16 categories × intent keywords → 3-8 tools/query    │     │
│  └───────────────────────┼───────────────────────────────┘     │
│                          │                                      │
│  ┌────────┬──────────┬───┴────┬──────────┬────────────┐       │
│  │Actuators│ Sensors  │  Core  │  Memory  │    MCP     │       │
│  │        │          │        │          │            │       │
│  │system_ │vision.py │memory  │web_      │client.py   │       │
│  │control │          │learning│memory.py │manager.py  │       │
│  │browser │          │knowl.  │          │config.py   │       │
│  │web_agt │          │screen_ │          │            │       │
│  │comms   │          │context │          │            │       │
│  │search  │          │prompt_ │          │            │       │
│  │file_sys│          │templates          │            │       │
│  │network │          │utils   │          │            │       │
│  └────────┴──────────┴────────┴──────────┴────────────┘       │
└─────────────────────────────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   MLX Text Server    MLX VLM Server    macOS APIs
   (port 8081)        (port 8082)       (AppleScript,
   Phi-4-mini        Qwen2.5-VL-3B     System Events,
   4-bit quantized    4-bit quantized    Accessibility)
```

---

## 4. Process Topology (4 Processes)

Started by `scripts/run.sh` in this order:

| # | Process | Port | Language | Purpose |
|---|---------|------|----------|---------|
| 1 | `mlx_lm.server` | 8081 | Python | Text LLM — Phi-4-mini-instruct-4bit, OpenAI-compatible SSE API |
| 2 | *(disabled)* `start_vlm_server.py` | 8082 | Python | Vision LLM — Qwen2.5-VL-3B, lazy-load mode |
| 3 | `tool_server.py` | 5001 | Python | Flask tool gateway — 88 native tools + MCP routing |
| 4 | `demascas_daemon` | — | C++ | Foreground — audio capture, whisper, state machine, TTS |

**Shutdown:** Ctrl-C on `run.sh` sends SIGINT → cleanup trap kills all PIDs.

The daemon starts last and runs in the foreground. It polls `/health` on the tool server and `/v1/models` on the MLX server before entering the main loop.

---

## 5. C++ Daemon — Deep Dive

**Source files:** `daemon/src/main.cpp` (1471 lines), `llm_client.cpp` (1347), `tool_bridge.cpp` (271), `audio_engine.cpp/hpp`, `whisper_engine.cpp/hpp`, `tts.cpp/hpp`, `config.hpp` (108), `shortcut_map.hpp` (948).

### 5.1 State Machine

The daemon runs a single-threaded `while(g_running)` loop over 5 states:

```
SLEEPING ──wake word──→ LISTENING ──command──→ PROCESSING ──done──→ LISTENING
                             ↑                      │                    │
                             │              (no speech timeout)          │
                             │                      ↓                   │
                         SLEEPING ←──────── (back to sleep)             │
                                                    │                   │
                                              CONFIRMING ──yes/no──→ LISTENING
                                              CLARIFYING ──select──→ LISTENING
```

**State: SLEEPING**
- Continuous wake-word scanning using whisper tiny (~50ms per chunk)
- Audio health check (recovers from macOS sleep/wake killing PortAudio)
- Anti-echo: skips mic while TTS is playing + cooldown period after TTS
- VAD gate: skips whisper if RMS energy < threshold × 0.5
- `is_wake_phrase()` matches 20+ transcription variants: "de mascas", "the mascus", "de mascaras", "damascus", "hey demascas", etc.
- On wake: plays "Glass" system sound → speaks "At your service." → LISTENING

**State: LISTENING**
- Captures a full spoken utterance using VAD (voice activity detection)
- **Barge-in support:** While TTS is playing, the mic stays hot. If user speaks OVER the TTS (energy ≥ BARGE_IN_THRESHOLD of 0.18), TTS is interrupted instantly
- Follow-up timeout: waits up to FOLLOWUP_TIMEOUT seconds for speech; if no speech → "Standing by." → SLEEPING
- Transcription via whisper base (~200ms on M1)
- Post-processing: `normalize_transcription()` (5 steps — see §5.4)
- Hallucination filter: `is_hallucination()` catches phantom transcriptions from silence
- Dismiss detection: "stop", "go to sleep", "bye", "never mind" → SLEEPING
- Wake-word echo: if wake word said while LISTENING, acknowledges instead of processing
- Max consecutive blanks (3) → auto-sleep

**State: PROCESSING**
- Routes the command through 8 paths (see §5.5)
- Sentence callback `on_sentence` filters JSON tool calls and low-level chatter before TTS
- SKIP_PHRASES filter: "press enter", "clicked", "what would you like to do next", etc.
- After processing: logs interaction → saves session → transitions to LISTENING

**State: CONFIRMING**
- Entered when a tool returns `{"status": "pending_confirmation"}`
- Waits for TTS to finish the confirmation question
- Captures yes/no utterance (8s timeout)
- YES_WORDS: "yes", "yeah", "sure", "go ahead", "do it", "proceed", "send it", etc.
- On confirm: executes the pending tool
- On deny or timeout: "Cancelled." → LISTENING

**State: CLARIFYING**
- Entered when `find_file` returns >1 match during planner PATH 2.5
- Choices were spoken by PROCESSING; now listens for user selection
- **Matching strategies:**
  1. Ordinal: "first", "second", "number 2", "last"
  2. Fuzzy name match: user says part of a filename
- Successful match → `open_file` with the chosen path
- **Continuation detection:** If PROCESSING sees pending_file_choices from a prior turn, it intercepts the new command and routes directly to selection resolution (without re-entering CLARIFYING state)

### 5.2 Audio Pipeline

**AudioEngine** (`audio_engine.hpp/cpp`):
- PortAudio C callback → ring buffer (lock-free write, mutex-guarded read)
- 16 kHz mono float32 (minimum for Whisper)
- `capture_chunk(seconds)` — fixed-duration recording for wake-word scanning
- `capture_utterance(timeout, silence)` — dynamic VAD: waits for speech onset, records until silence_sec of quiet
- `rms(data, n)` — root-mean-square energy calculator
- `drain()` — empties and returns all accumulated samples (used by barge-in)
- `is_stream_healthy()` / `reinit_on_wake()` — recovery from macOS sleep/wake

**VAD parameters (config.hpp):**
- `VAD_ENERGY_THRESHOLD = 0.008` — minimum RMS to consider as potential speech
- `BARGE_IN_THRESHOLD = 0.18` — user voice at 30cm ≈ 0.20-0.50, speaker bleed ≈ 0.05-0.15
- `SILENCE_DURATION = 2.0` seconds — how long to wait after speech stops
- `WAKE_LISTEN_WINDOW = 2.0` seconds — chunk size for wake-word scanning

### 5.3 Whisper Engine (ASR)

**WhisperEngine** (`whisper_engine.hpp/cpp`):
- Two whisper.cpp contexts loaded simultaneously:
  - **tiny** (~39 MB GGML) — wake-word scanning, ~50ms inference on M1 Metal
  - **base** (~140 MB GGML) — command transcription, ~200ms inference on M1 Metal
- Models stored at `daemon/models/ggml-tiny.bin` and `ggml-base.bin`
- Metal GPU acceleration (Apple Silicon ANE) — no Python, no ONNX
- `transcribe_wake(pcm)` — uses tiny context for fast wake-word detection
- `transcribe_command(pcm)` — uses base context for accurate command transcription
- `set_prompt(prompt)` — set initial prompt context for improved recognition

### 5.4 Transcription Normalization

`normalize_transcription()` in `main.cpp` applies 5 sequential steps:

1. **Strip fillers:** Removes "um", "uh", "ah", "like", "you know", "so", "well", "okay so" from start
2. **Collapse spelled letters:** `"S, I, H"` → `"SIH"`, `"P, D, F"` → `"PDF"` — Whisper spells out abbreviations when spoken quickly
3. **Collapse spelled digits:** `"2, 0, 2, 5"` → `"2025"` — same issue with year/number pronunciation
4. **[FILE] tag injection:** If the command contains file-extension keywords (pdf, ppt, doc, etc.) and action words (open, find, search), appends `" [FILE]"` tag — helps the LLM route to file tools
5. **Trim:** Strip leading/trailing whitespace

### 5.5 Command Routing (8 Paths)

After LISTENING captures and normalizes a command, PROCESSING routes it through these paths in priority order. The first match wins:

| Priority | Path | Condition | Latency | Description |
|----------|------|-----------|---------|-------------|
| **1** | Shortcut | `shortcuts.match()` returns a key combo | <1μs | Keyboard shortcut from shortcut_map.hpp — "copy", "paste", "undo", "save", "new tab" |
| **1.5** | URL | `extract_url()` finds a URL pattern | ~100ms | Deterministic URL opening — bypasses LLM (which often picks wrong tool). Guards against "why did you open..." questions |
| **1.6** | App Open | `extract_app_open()` matches | ~100ms | "open Safari", "launch Chrome" → direct `open_application()` |
| **1.7** | App Close | `extract_app_close()` matches | ~100ms | "close Safari", "quit Chrome" → direct `quit_application()`. "close window/tab" goes to shortcuts |
| **1.8** | Macro | `try_macro()` matches | ~200ms | Pre-built multi-step sequences: "open gmail" → open_url + wait, "google X" → open_url + type |
| **2** | Fast Q&A | `is_simple_question()` returns true | ~500ms | Simple questions — streamed via `fast_chat_streaming()` with NO tool schemas. JARVIS personality. **FORCE_TOOL_KEYWORDS** (compose, draft, click the, fill in, etc.) force-route to tool path |
| **2.5** | Planner | `is_multi_step_command()` returns true | ~1-3s | "open my PDF then..." — decomposes into 2-4 structured steps, executes sequentially with inter-step result passing |
| **4** | Deep Intent | `is_deep_intent_command()` returns true | ~2-4s | "reply to that email", "research X" — 3-phase: Understand → Plan → Execute. Uses task-specific prompt templates |
| **3** | Tool ReAct | Default (fallback) | ~1-4s | Full streaming ReAct loop with tool schemas. Up to MAX_TOOL_DEPTH (5) iterations |

**`is_simple_question()` logic (shortcut_map.hpp):**
1. Check FORCE_TOOL_KEYWORDS first → if match, return false (force tool path)
2. Check triggers list ("what time", "who is", "how do") → true
3. Check conversation prefixes ("hello", "thanks", "you're") → true
4. Check if command ends with "?" → true
5. Check action-verb heuristic (open, click, type, find, search, etc.) → false
6. Default → false (goes to tool path)

**`is_deep_intent_command()` logic:**
- EXCLUDED: "compose", "draft", "click the", "click on", "fill in", "navigate to" (these go to PATH 3)
- INCLUDED: "reply to", "respond to", "research and", "summarize", "schedule", "send a message", "extract contact"

### 5.6 LLM Client — System Prompts & Rules

**`build_tool_prompt()`** constructs the system prompt for PATH 3 (ReAct):

```
You are DEMASCAS, a macOS AI assistant (JARVIS-like).
Current time: {IST time} | Date: {IST date}

AVAILABLE TOOLS:
- press_keyboard_shortcut(keys) — Press macOS keyboard shortcut
- find_file(folder, partial_name, extension) — Search for files by name
  ... (3-8 filtered tools per query)

RULES:
1. Respond with JSON: {"tool":"...","args":{...}} or {"done":true,"say":"..."}
2. NEVER invent tool names. Only use tools from the list above.
3. One tool call per response. Wait for the result before calling another.
4. For file operations: find_file FIRST, then open_file with the result path.
5. NEVER open a browser to find local files.
6. NEVER create a file when asked to find/open one.
7. Use ~/Downloads, ~/Documents, ~/Desktop — NEVER /Users/<name>/...
8. [LEARNED CONTEXT] {past episode results if available}
9. [MEMORY] {ChromaDB retrieval results if relevant}
10. When Brave is active, accessibility tree CANNOT see web content. Use browser_get_dom_summary instead.
11. When on Gmail (mail.google.com), use gmail_compose_draft directly.
12. To compose email, FIRST ask for recipient email if not provided.
```

**`build_simple_prompt()`** for PATH 2 (fast Q&A):

```
You are DEMASCAS, a JARVIS-like AI assistant. Helpful, witty, concise.
Speak naturally like a personal assistant. Keep answers under 2-3 sentences.
Current time: {IST time} | Date: {IST date}

CRITICAL: You have NO tools. You CANNOT perform actions.
If the user asks you to DO something (click, open, search, type, etc.),
say: "I'll need to process that as a command. Let me handle it."
NEVER pretend to perform actions or make up results.
```

### 5.7 ReAct Tool Loop (PATH 3)

`chat_with_tools_streaming()` in `llm_client.cpp`:

1. **Hard-reset history** for each new tool command (prevents stale context pollution)
2. **Fetch filtered tools** via tool router (3-8 relevant tools instead of all 88)
3. **Memory auto-retrieval:** Queries ChromaDB for relevant past memories
4. **Learned context injection:** Fetches relevant past episodes
5. **ReAct loop** (up to MAX_TOOL_DEPTH = 5 iterations):
   - Build messages = [system prompt with tools] + history
   - Call MLX streaming (silent callback — no TTS during tool selection)
   - Parse response via `parse_llm_action()` — extracts JSON from free-form text using `extract_json_objects()` (handles prose + fenced JSON)
   - If `{"done": true, "say": "..."}` → stream to TTS, break
   - If `{"tool": "...", "args": {...}}` → execute via ToolBridge
   - **Dedup detection:** If same tool+args repeated → break
   - **Error handling:** `is_tool_error()` checks for [-], Failed:, Error:, Timeout:
   - **Recovery hints:** Injected into feedback on error (specific to tool type):
     - `find_file` folder not found → "use tilde ~/Downloads, not /Users/..."
     - `find_file` no matches → "tell user, never create file, never open browser"
     - General → "try get_active_window_tree, use keyboard shortcut, or report"
   - **Screen state injection:** After UI-mutating tools, screen state is auto-captured and appended to feedback
   - **Hard stop:** After REACT_MAX_ERRORS (3) consecutive errors → abort with explanation
6. **Session history:** Tool results fed back as "user" role messages (MLX has no "tool" role)

### 5.8 Multi-Step Planner (PATH 2.5)

`plan_steps_structured()` in `llm_client.cpp`:

1. Sends the command to MLX with a planner-specific system prompt (constrained tool set, approved shortcuts only, file-search rules)
2. Parses the JSON array response into structured steps
3. **Validation:** Only allows tools from a VALID_TOOLS whitelist
4. **Sanitization:** Blocks generic partial_name values ("file", "pdf", "a", "the"); fixes /Users/ paths to ~/
5. **Result substitution:** `{result_of_step_N}` tokens in args are replaced with actual results from previous steps
6. **find_file handling:**
   - Single match → extract path → pass to next step
   - Multiple matches → enter CLARIFYING state (speak choices, wait for selection)
   - No matches → report error and stop
7. **Tool error detection** → stop and report
8. Cap at 4 steps maximum

**`is_multi_step_command()` triggers on:**
- Conjunctions: "and then", "after that", "then", "next", "also"
- Step words: "step 1", "first", "second"
- Action words: "search for", "go to", "navigate to"
- 2+ action verbs in one command

### 5.9 Deep Intent (PATH 4)

`deep_intent_streaming()` in `llm_client.cpp`:

**Phase 1 — Context Gathering (Python side):**
- Tool server's `/deep_intent` endpoint classifies the task type
- Fetches: task-specific prompt template, screen context, user profile, people context
- Returns a `DeepIntentResult` with all context pre-assembled

**Phase 2 — Understanding + Planning (LLM):**
- Uses the deep intent prompt (from `core/prompt_templates.py`)
- Forces structured thinking: Understand → Plan → First Action
- Output: `{"intent": "...", "task_type": "...", "plan": [...], "first_action": {"tool": "...", "args": {...}}}`
- `parse_deep_intent()` extracts the structured response

**Phase 3 — Execution:**
- If direct answer (done=true) → speak immediately
- If first_action → execute it, handle confirmations
- If multi-step plan → execute remaining steps through `chat_with_tools_streaming()` (ReAct loop)

### 5.10 TTS Engine (Kokoro)

**TTS** (`tts.hpp/cpp`):

- **Primary:** Kokoro TTS via sherpa-onnx
  - 82M parameter StyleTTS2 model
  - In-process neural inference (ONNX Runtime, CPU-only — GPU stays free for Whisper Metal)
  - Float32 PCM at 24 kHz mono → PortAudio output stream
  - Model: `daemon/models/kokoro-en-v0_19/model.onnx` (82MB)
- **Fallback:** macOS `say` command (when sherpa-onnx not available)
- **Sentence queue:** Thread-safe, dedicated worker thread
  - `enqueue(text)` — non-blocking, adds to queue
  - `speak(text)` — synchronous, blocks until done
  - `interrupt()` — kills current playback + clears queue (<5ms)
  - `is_playing()` — true if audio playing or queued
  - `wait_done()` — blocks until all sentences spoken
- **System sounds:** `play_sound("Glass")`, `play_sound("Tink")` — macOS sounds via `afplay`
- **Text sanitization:** Strips markdown, code blocks, URLs, and other non-speakable content

### 5.11 Tool Bridge (HTTP Client)

**ToolBridge** (`tool_bridge.hpp/cpp`):

All communication with the Python tool server goes through this class:

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| `wait_for_server(max_retries)` | GET | `/health` | Block until tool server ready |
| `fetch_tool_schemas()` | GET | `/tools` | All 88 tool schemas (Ollama format) |
| `execute(name, args)` | POST | `/tool` | Execute a tool, return result string |
| `execute_with_screen(name, args)` | POST | `/tool` | Execute + return result + screen_state |
| `fetch_filtered_tools(query, max)` | POST | `/tools_for_query` | Smart tool filtering (3-8 tools) |
| `fetch_learned_context(query)` | GET | `/context?query=...` | Past episodes + corrections for this command |
| `log_interaction(cmd, path, tools, result)` | POST | `/learn` | Fire-and-forget learning log |
| `fetch_screen_context(force)` | GET | `/screen_context` | Structured screen digest (VLM + tree) |
| `fetch_deep_intent(command)` | POST | `/deep_intent` | Task classify + prompt + context for PATH 4 |

Uses libcurl for all HTTP. Timeout 30s for POST, 5s for GET.

### 5.12 Configuration

**`config.hpp`** — all tuning knobs in one place:

```cpp
// LLM endpoints
MLX_TEXT_URL   = "http://127.0.0.1:8081/v1/chat/completions"
MLX_VL_URL     = "http://127.0.0.1:8082/v1/chat/completions"
MLX_TEXT_MODEL  = "mlx-community/Phi-4-mini-instruct-4bit"

// Context windows & prediction limits
TOOL_CTX       = 2048    // ReAct loop context window
TOOL_PREDICT   = 200     // Max tokens for tool response
FAST_CTX       = 1024    // Fast Q&A context window
FAST_PREDICT   = 80      // Max tokens for fast answer
LLM_TEMPERATURE = 0.1    // Low temp for deterministic tool selection

// Audio
SAMPLE_RATE        = 16000   // 16 kHz (Whisper requirement)
VAD_ENERGY_THRESHOLD = 0.008
BARGE_IN_THRESHOLD   = 0.18
SILENCE_DURATION     = 2.0   // seconds after last speech
WAKE_LISTEN_WINDOW   = 2.0   // seconds per wake-word chunk
FOLLOWUP_TIMEOUT     = 15.0  // seconds before auto-sleep

// TTS
USE_KOKORO         = true
KOKORO_SAMPLE_RATE = 24000
TTS_COOLDOWN_SEC   = 0.5    // anti-echo cooldown after TTS

// Whisper
WHISPER_TINY_MODEL = "models/ggml-tiny.bin"
WHISPER_BASE_MODEL = "models/ggml-base.bin"

// Tool server
TOOL_SERVER_BASE  = "http://127.0.0.1:5001"
TOOL_EXEC_URL     = TOOL_SERVER_BASE + "/tool"
TOOL_SCHEMA_URL   = TOOL_SERVER_BASE + "/tools"
TOOL_HEALTH_URL   = TOOL_SERVER_BASE + "/health"

// Limits
MAX_HISTORY           = 4    // conversation turns kept in LLM context
MAX_TOOL_DEPTH        = 5    // max ReAct iterations
MAX_CONSECUTIVE_BLANKS = 3   // before auto-sleep
REACT_MAX_ERRORS      = 3    // consecutive tool errors before abort
SESSION_MAX_TURNS     = 6    // max turns persisted to disk
SESSION_FILE = "/tmp/demascas_session.json"

// Wake word
WAKE_WORD = "demascas"
```

---

## 6. Python Tool Server — Deep Dive

### 6.1 Flask Server & Endpoints

**`server/tool_server.py`** (1091 lines) — single Flask app:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/tool` | POST | Execute a tool: `{"name": "...", "arguments": {...}}` → `{"result": "..."}` |
| `/tools` | GET | Return all 88 tool schemas (Ollama format JSON array) |
| `/tools_for_query` | POST | Smart-filtered subset: `{"query": "...", "max_tools": 8}` → filtered schemas |
| `/health` | GET | `{"status": "ok", "tools": 88, "native": N, "mcp": M}` |
| `/screen_context` | GET | Structured screen digest (cached 3s TTL) |
| `/deep_intent` | POST | Task classification + prompt template + context assembly |
| `/context` | GET | Learned context for a query (episodes + corrections + profile) |
| `/learn` | POST | Log an interaction (command, path, tools, result, success) |
| `/session` | GET | Current ACTIVE_CONTEXT state |
| `/mcp/status` | GET | MCP server status (RSS, tool list per server) |

**Key behaviors:**
- Tool name normalization via `_normalize_tool_call()` — maps aliases, fixes common LLM mistakes:
  - `"keyboard_shortcut"` → `"press_keyboard_shortcut"`
  - `"click"` → `"click_ui_element"`
  - `"find"` → `"find_file"` (when args contain file-related params)
  - `"open_browser"` → `"open_url"`
  - `"gmail_compose"` → `"gmail_compose_draft"`
  - `"web_click"`, `"smart_click"` → `"web_smart_click"`
  - etc.
- Browser tool arg normalization: `"css_selector"` → `"selector"`, `"text_content"` → `"text"`, `"input_text"` → `"text"`
- Gmail compose arg normalization: `"recipient"` → `"to"`, `"email_body"` → `"body"`, `"email_address"` → `"to"`
- ACTIVE_CONTEXT updated after every tool call
- Screen state auto-capture: For UI-mutating tools (click, type, open, browser_*, etc.), the server auto-captures the screen state after execution and returns it alongside the result. The C++ daemon injects this into the LLM's feedback.
- MCP routing: If tool name not in TOOL_MAP, tries MCP manager

### 6.2 TOOL_MAP (88 Tools)

The `TOOL_MAP` dictionary maps tool names → Python callables. Categories:

| Category | Count | Tools |
|----------|-------|-------|
| UI Interaction | 10 | click_ui_element (with 3-tier safe wrapper), type_text, type_text_into_element, scroll_direction, drag_element, find_and_click, right_click_element, type_and_submit, scroll_to_element, wait_for_element |
| App Management | 5 | open_application, quit_application, activate_application, list_running_applications, wait_for_app_ready |
| Keyboard | 1 | press_keyboard_shortcut |
| Navigation | 2 | open_url, open_url_and_wait |
| File System | 6 | list_files_in_folder, create_file, delete_file, find_file, open_file, summarize_latest_pdf |
| Web Search | 3 | search_web, read_webpage, smart_search |
| Memory | 2 | save_memory, retrieve_memories |
| Clipboard | 2 | get_clipboard, set_clipboard |
| Vision | 3 | get_active_window_tree, list_open_windows, vision_execute |
| Browser (30+) | 30 | browser_execute_js, browser_get_url, browser_navigate, browser_click, browser_click_text, browser_type, browser_get_dom_summary, gmail_compose_draft, gmail_read_email, ... |
| Web Agent | 11 | web_analyze_page, web_smart_click, web_search, web_read_article, web_research, web_execute_goal, web_youtube_search, web_youtube_play_first, web_ecommerce_search, web_scan_for_obstacles, web_handle_obstacle |
| Communication | 6 | read_current_email, compose_email, smart_reply, confirm_and_execute, extract_contact, web_research_and_report |
| Realtime | 2 | get_weather, get_news |
| Tab Management | 1 | switch_tab |

**`click_ui_element_safe()` — 3-tier fallback:**
1. `wait_for_element()` + `click_ui_element()` (accessibility tree)
2. If failed → `vision_execute()` (VLM screenshot-based click)
3. If failed → structured error message with recovery suggestions

### 6.3 ACTIVE_CONTEXT (Cross-Turn State)

Persisted in-memory across tool calls within a session:

```python
ACTIVE_CONTEXT = {
    "active_app": None,          # Currently focused app
    "active_folder": None,       # Last navigated folder
    "active_file": None,         # Last opened file
    "active_url": None,          # Current browser URL

    "last_tool": None,           # Last tool executed
    "last_tool_args": None,      # Last tool arguments
    "last_tool_result": None,    # Last tool result
    "last_action_summary": None, # Human-readable last action
    "last_failed_action": None,  # Last failed action

    "pending_goal": None,        # Multi-turn goal tracking
    "pending_clarification": None, # "Which file did you mean?"
    "pending_file_choices": None,  # [{name, path}, ...]
    "awaiting_user_choice": False, # Flag for continuation

    "home_dir": "~",
    "downloads_dir": "~/Downloads",
    "documents_dir": "~/Documents",
    "desktop_dir": "~/Desktop",

    "session_start": "ISO timestamp",
    "turn_count": 0,
    "last_updated": None,
}
```

**Auto-updated by `_update_context()`** after every tool call:
- `open_application` → sets `active_app`
- `open_url` → sets `active_app = "Safari"`, `active_url`
- `browser_*` / `gmail_*` → sets `active_app = "Brave Browser"`
- `press_keyboard_shortcut` with cmd+option+l → sets `active_folder = ~/Downloads`
- `find_file` with >1 match → sets `pending_file_choices`, `awaiting_user_choice = True`
- `open_file` → clears pending state, sets `active_file`

### 6.4 Tool Router (Smart Filtering)

**`server/tool_router.py`** (405 lines):

Qwen 3B performs dramatically better with fewer tool schemas. The router reduces 88 tools to 3-8 relevant ones per query.

**Mechanism:**
1. Match query keywords against `INTENT_KEYWORDS` dict (150+ keyword → category mappings)
2. Collect tool names from matched `TOOL_CATEGORIES` (16 categories)
3. Apply contextual overrides:
   - Brave Browser active → auto-include browser + web_agent categories, exclude `get_active_window_tree` (can't see web content)
   - Gmail URL active → auto-include gmail_browser, prioritize `gmail_compose_draft` and `gmail_read_email`
   - File intent detected → always include `find_file` + `open_file`, exclude `open_url`
4. Cap at `max_tools` (default 8)

**16 Categories:** ui_interaction, app_management, web_browsing, browser, web_agent, gmail_browser, youtube, ecommerce, file_management, screen_reading, vision, keyboard, memory, clipboard, typing, weather, realtime.

### 6.5 Tool Schemas (core/toolbox.py)

**`core/toolbox.py`** (1782 lines) — `TOOLS` list with 80 OpenAI-format schemas.

Each schema has:
```python
{
    "type": "function",
    "function": {
        "name": "tool_name",
        "description": "What this tool does. Includes LLM steering hints.",
        "parameters": {
            "type": "object",
            "properties": { ... },
            "required": [...]
        }
    }
}
```

**Notable steering hints in descriptions:**
- `browser_click` description steers toward `browser_click_text` for text-based clicks
- `gmail_compose_draft` has `required: []` (all args optional — fixes multi-turn compose)
- `find_file` description emphasizes `~/Downloads` tilde syntax, never `/Users/...`

---

## 7. Actuators — The Hands

### 7.1 system_control.py — AppleScript UI Automation

**`actuators/system_control.py`** (1357 lines) — 20+ functions for native macOS automation:

**Element interaction:**
- `click_ui_element(name)` — recursive AppleScript search through accessibility tree, with element name normalization (snake_case → alias expansion → title-case)
- `ELEMENT_NAME_ALIASES` map: "compose_button" → ["Compose", "Compose email", "New message"], etc.
- `type_text(text)` — types text into focused field via System Events keystroke
- `type_text_into_element(element_name, text)` — finds element, focuses it, types into it
- `scroll_direction(direction, amount)` — scroll up/down/left/right
- `drag_element(from, to)` — drag from one element to another
- `find_and_click(text)` — search accessibility tree for text, click first match
- `right_click_element(name)` — right-click context menu
- `type_and_submit(text)` — type + press Enter

**Application control:**
- `open_application(app_name)` — `open -a "App Name"`
- `quit_application(app_name)` — AppleScript quit
- `activate_application(app_name)` — bring to front
- `list_running_applications()` — list all running apps
- `open_url(url, browser)` — opens in specified browser or default
- `open_url_and_wait(url, wait_time)` — opens + waits for page load
- `press_keyboard_shortcut(keys)` — parses "cmd+shift+3" into AppleScript key commands
- `wait_for_element(name, timeout)` — polls accessibility tree
- `wait_for_app_ready(app_name, timeout)` — waits for app to have a window

**Keyboard shortcut parsing:**
- Modifier map: cmd/command, shift, option/alt, control/ctrl
- Special keys: return, escape, tab, space, delete, arrow keys, F1-F12, volume, power
- Format: `"cmd+shift+3"` → `keystroke "3" using {command down, shift down}`
- Key codes for special keys: return=36, escape=53, tab=48, etc.

**Safety:**
- All AppleScript strings sanitized via `_sanitize_for_applescript()` (strips backslashes, quotes, newlines)
- 10-second subprocess timeout on all actions

### 7.2 browser.py — Brave Browser JS Injection

**`actuators/browser.py`** (1207 lines) — 30+ browser automation functions:

**Architecture:** JavaScript injection via AppleScript `tell application "Brave Browser" to execute ... javascript`. All JS runs in the active tab's page context.

**Core pattern:**
```python
def browser_action(args):
    js = "..."  # JS code to execute
    safe_js = _sanitize_js(js)  # escape for AppleScript embedding
    applescript = f'tell application "Brave Browser" to execute active tab of window 1 javascript "{safe_js}"'
    result = subprocess.run(['osascript', '-e', applescript], ...)
```

**Navigation:** browser_navigate, browser_navigate_and_wait, browser_back, browser_forward, browser_reload, browser_new_tab, browser_close_tab, browser_list_tabs, browser_switch_to_tab

**Reading:** browser_get_url, browser_get_title, browser_get_page_text, browser_get_page_html, browser_get_links, browser_get_inputs, browser_get_dom_summary

**Interaction:**
- `browser_click(selector)` — CSS selector click with null guards + smart CSS redirect if element not found
- `browser_click_text(text)` — finds visible element by text content, clicks it. Suffix stripping for trailing colons etc.
- `browser_click_xpath(xpath)` — XPath-based click
- `browser_type(selector, text)` — types into element. Handles both `value` setter and `contenteditable` (execCommand). Dispatches `input` + `change` events for React compatibility
- `browser_type_by_label(label, text)` — finds input by associated label
- `browser_press_key(key)` — dispatches keyboard event
- `browser_scroll(direction, amount)` — window.scrollBy
- `browser_fill_form(fields)` — fills multiple form fields by name/id/label
- `browser_select_option(selector, value)` — select dropdown option
- `browser_check_checkbox(selector, checked)` — set checkbox state
- `browser_submit_form(selector)` — submit a form

**Gmail-specific:**
- `gmail_compose_draft(to, subject, body, cc, bcc)` — all args optional. Opens compose window, fills fields. Never auto-sends.
- `gmail_read_email()` — extracts email content from DOM

**DOM summary:** `browser_get_dom_summary()` — returns compact DOM structure. Filters hidden elements (display:none, visibility:hidden, opacity:0, zero-size). Limits to 50 anchor, 30 button, 15 input, 10 select elements.

**Bug fixes applied:**
- Null guards on all browser_click/type functions (NoneType crash fix)
- Smart CSS redirect when selector not found (tries alternative selectors)
- gmail_compose_draft all args optional (multi-turn fix)
- Suffix stripping on browser_click_text
- Hidden element filtering in DOM summary

### 7.3 web_agent.py — Autonomous Browsing

**`actuators/web_agent.py`** (678 lines) — higher-level browsing intelligence:

- `web_analyze_page()` — deep page analysis: classify site type, extract interactive elements, detect obstacles, record visit in web memory
- `web_smart_click(target)` — 4-tier click fallback:
  1. Web memory (learned selector for this domain)
  2. CSS selector (direct)
  3. Text-based click (browser_click_text)
  4. DOM scan (find closest match in DOM summary)
- `web_scan_for_obstacles()` — detects cookie banners, popups, login walls, paywalls, age gates
- `web_handle_obstacle(obstacle_type)` — auto-dismiss cookies, report paywalls/logins
- `web_search(query)` — opens DuckDuckGo, types query, returns results
- `web_read_article(url)` — navigates to URL, extracts article text
- `web_research(topic, num_sources)` — multi-source research: search → read top results → compile report
- `web_execute_goal(goal)` — autonomous goal execution using page analysis + plan
- `web_youtube_search(query)` — YouTube-specific search
- `web_youtube_play_first()` — play first YouTube search result
- `web_ecommerce_search(product)` — e-commerce product search

**Safety constraints:**
- CAPTCHA, paywall, login wall → PAUSE and report (never bypass)
- E-commerce purchase, social media post → require user confirmation
- Never auto-send emails

### 7.4 communication.py — JARVIS-Grade Comms

**`actuators/communication.py`** (479 lines):

- `read_current_email()` — VLM screenshot → extract {from, subject, body_preview, timestamp}
- `compose_email(recipient, subject, key_points, tone)` — MLX text model with compose_email prompt template → returns draft JSON for confirmation
- `smart_reply(context)` — reads screen via VLM, generates reply matching original tone
- `confirm_and_execute(action, description)` — safety gate for destructive actions
- `extract_contact(name)` — extracts contact info from screen/conversation
- `web_research_and_report(topic)` — multi-source web research → structured report

### 7.5 smart_search.py — Google/DDG Search

**`actuators/smart_search.py`** (317 lines):

**Search chain:**
1. Check fact cache (ChromaDB knowledge base) — instant, zero API cost
2. Google Custom Search API — if API key configured in `~/.demascas/config.json`
3. DuckDuckGo fallback — always works, no key needed (uses `ddgs` package)
4. Cache results in knowledge base for future queries

**Convenience wrappers:**
- `get_weather(location)` — wttr.in API (free, no key)
- `get_news(topic)` — DuckDuckGo news

### 7.6 file_system.py — File Operations

**`actuators/file_system.py`**:

- `find_file(folder, partial_name, extension)` — glob search in a folder. Returns `{found, count, matches: [{name, path, size, modified}], message}`. Multi-match returns disambiguation message.
- `open_file(file_path)` — `open "path"` via macOS open command
- `list_files_in_folder(folder_path)` — lists folder contents with sizes/dates
- `create_file(file_path, content)` — creates a new file
- `delete_file(file_path)` — deletes a file (with safety checks)
- `summarize_latest_pdf(folder)` — finds latest PDF in folder, extracts text via PyPDF2

### 7.7 network.py — Web Scraping

**`actuators/network.py`**:

- `search_web(query)` — DuckDuckGo web search via `ddgs` package
- `read_webpage(url)` — fetches URL, extracts text with BeautifulSoup (html2text-style)

---

## 8. Sensors — The Eyes

### 8.1 vision.py — Accessibility Tree + VLM

**`sensors/vision.py`** (567 lines):

**Accessibility tree (native macOS):**
- `get_active_window_tree(app_name=None)` — AppleScript recursive walk of UI elements via System Events. 6-level depth, 600 node cap. Returns formatted text: `ACTIVE APP: ... \n UI TREE MAP: \n - [button] : 'Compose' \n ...`
- `list_open_windows()` — all visible windows across all apps
- `compress_tree(raw_tree)` — compresses the verbose tree to compact format

**VLM integration (optional):**
- `take_screenshot_base64()` — captures screen via `screencapture` command, returns base64
- `vision_query(prompt, image_base64)` — sends image + text prompt to VLM endpoint
- `vision_execute(command)` — screenshot → VLM analysis → extract coordinates → click/scroll action
- Endpoint auto-discovery: Checks `~/.demascas/vlm_endpoint.txt` → port 8082 → disabled

**VLM server (`scripts/start_vlm_server.py`):**
- Starts mlx-vlm.server with Qwen2.5-VL-3B
- Probe thread writes discovered endpoint to `~/.demascas/vlm_endpoint.txt`
- Disabled by default in run.sh (8GB RAM constraint)

---

## 9. Core Intelligence Layer

### 9.1 memory.py — ChromaDB RAG

**`core/memory.py`** (~170 lines):

- Persistent storage at `~/.demascas/memory/`
- ChromaDB PersistentClient with all-MiniLM-L6-v2 embeddings (~80 MB)
- `save_memory(text)` — content-hash dedup, IST timestamp, "user" source
- `retrieve_memories(query, top_k=5)` — cosine similarity semantic search
- Lazy initialization — ChromaDB client created on first use only
- Collection: `demascas_memory`

### 9.2 learning.py — Continuous Learning Engine

**`core/learning.py`** (1017 lines):

**Three layers all persisted to `~/.demascas/`:**

**Layer 1: Episodic Memory** (ChromaDB collection: `demascas_episodes`)
- Auto-logs every command + outcome after each interaction
- Metadata: timestamp, date, hour, path_taken, tools_used, result, success
- Semantic search finds what worked for similar past commands
- Capped at 500 episodes; compressed when hitting 550
- `log_episode()` — called after every command (fire-and-forget)
- `get_relevant_episodes(query, top_k=3)` — returns similar past interactions

**Layer 2: User Profile** (JSON: `~/.demascas/user_profile.json`)
- Extracted preferences, personal facts, app usage stats, corrections, workflow patterns
- Updated after every interaction via lightweight heuristics (zero extra LLM calls)
- In-memory cache with dirty flag; flushed to disk every 5 commands
- Tracks: name, preferred apps, common actions, time-of-day patterns, corrections

**Layer 3: Context Builder** (pre-command injection)
- Before each LLM call: queries episodes, profile, corrections
- Builds compact `learned_context` string
- Injected into system prompt so LLM sees personalized context
- Avoids repeating past mistakes (corrections from knowledge.py)

**Critical optimization:** Reuses the SAME ChromaDB PersistentClient as memory.py — two clients would waste 80 MB for the duplicate embedding model.

### 9.3 knowledge.py — Procedures, Facts, Corrections

**`core/knowledge.py`** (464 lines):

Three ChromaDB collections sharing the same client:

**1. Procedures** (`demascas_procedures`):
- Step-by-step how-tos learned from successful multi-step tasks
- e.g. "How to send an email to Aman" → [open Mail, compose, type_text...]
- `save_procedure(title, steps, trigger_phrase)`
- `get_procedure(query, top_k)`

**2. Facts** (`demascas_facts`):
- Atomic facts about the world and user's contacts
- e.g. "Aman Gupta's email is aman@example.com"
- `save_fact(text, category, source)`
- `get_fact(query, top_k)`
- Used by smart_search fact cache

**3. Corrections** (`demascas_corrections`):
- Past mistakes and their fixes
- e.g. "DON'T open_url for local files — use find_file + open_file"
- `save_correction(mistake, correction, context)`
- `get_corrections(query, top_k)`
- Auto-populated by `detect_and_save_correction()` when success=false in /learn

**Auto-learning hooks:**
- `auto_save_from_tool_result()` — extracts facts/procedures from tool results
- `detect_and_save_correction()` — called on failed interactions

### 9.4 screen_context.py — VLM Pre-Digestion

**`core/screen_context.py`** (254 lines):

Makes the 3B model dramatically smarter by pre-digesting screen state:

1. Captures accessibility tree (fast, ~200ms)
2. Optionally captures screenshot + VLM analysis (~1-2s)
3. Returns compact ~100 token context block:
   ```json
   {
     "app": "Brave Browser",
     "page_type": "email inbox",
     "url": "https://mail.google.com/...",
     "key_elements": ["Compose (button)", "Inbox (link)", ...],
     "summary": "Gmail inbox with 3 unread emails",
     "raw_tree_compact": "..."
   }
   ```
4. Cached for 3 seconds (no repeated VLM calls within TTL)

**Functions:**
- `get_screen_digest(force=False)` — returns full digest dict
- `get_screen_context_block()` — returns formatted text block for system prompt injection

### 9.5 prompt_templates.py — Task-Specific Prompts

**`core/prompt_templates.py`** (352 lines):

Task-specific prompts that make a 3B model perform like a 70B by:
- Narrowing output space (model knows EXACTLY what to produce)
- Providing examples (few-shot in-context)
- Setting hard constraints (length, format, tone)
- Injecting screen context

**Templates:**
1. `compose_email_prompt()` — 3-phase: Understand recipient+tone → Draft → Format JSON
2. `smart_reply_prompt()` — Match original tone, address key points, shorter than original
3. `research_report_prompt()` — Search → Read → Synthesize in 3-5 spoken sentences
4. `multi_step_action_prompt()` — Execute pre-planned steps, one tool per response
5. `confirm_action_prompt()` — Natural spoken confirmation by risk level
6. `deep_intent_prompt()` — PATH 4 master prompt. 3-phase: Understand → Plan → First Action. Accepts `tool_names` list to constrain LLM to real tools only.

**Task classification:**
- `classify_task(command)` → "compose_email" | "smart_reply" | "research" | "schedule" | "file_management" | "communication" | "general"

**Template registry:**
```python
TEMPLATE_REGISTRY = {
    "compose_email": compose_email_prompt,
    "smart_reply": smart_reply_prompt,
    "research": research_report_prompt,
    "schedule": multi_step_action_prompt,
    "file_management": multi_step_action_prompt,
    "communication": compose_email_prompt,
    "general": None,  # use default tool prompt
}
```

### 9.6 utils.py — Path Resolution

**`core/utils.py`**:

- `resolve_safe_path(path)` — resolves `~`, `$HOME`, relative paths to absolute. Prevents path traversal outside home directory.

---

## 10. Memory Subsystem

### 10.1 web_memory.py — Per-Domain Browsing Memory

**`memory/web_memory.py`** (276 lines):

JSON-backed per-domain memory at `~/.demascas/web_memory.json`:

```json
{
  "mail.google.com": {
    "domain": "mail.google.com",
    "site_type": "email",
    "known_elements": {"compose_button": {"selector": "div.T-I", "label": "Compose"}},
    "page_flows": {"compose_email": ["click compose", "fill to", "fill subject", ...]},
    "dom_fingerprint": "...",
    "selectors": {"search_box": "input[name=q]"},
    "obstacles": {"cookie_banner": {"selector": "...", "action": "click dismiss"}},
    "visit_count": 47,
    "last_visited": 1706000000.0,
    "notes": ["Gmail uses React, need input+change events"]
  }
}
```

**Functions:**
- `get_site_memory(url)` — returns full memory blob for domain
- `save_site_memory(url, data)` — persist changes
- `record_visit(url)` — increment visit count + timestamp
- `learn_element(url, key, selector, label, role)` — record a learned element
- `get_learned_selector(url, key)` — retrieve a previously learned CSS selector
- `learn_obstacle(url, type, selector, action)` — record obstacle handling
- `set_site_type(url, type)` — classify domain (email, ecommerce, social, etc.)
- `web_memory_summary(url)` — compact text summary for LLM injection

---

## 11. MCP (Model Context Protocol)

**Built-in pure-Python MCP client** — zero external dependencies.

### Architecture:

```
~/.demascas/mcp_servers.json → MCPManager → MCPClient(s) → subprocess stdio
```

**`mcp/client.py`** (282 lines):
- JSON-RPC 2.0 over stdio pipes
- `select.select()` for read timeouts (no threads)
- stderr → /dev/null (no hidden RAM buffering)
- Compact JSON serialization
- Methods: `start()`, `shutdown()`, `list_tools()`, `call_tool(name, args)`, `rss_mb()`

**`mcp/manager.py`** (307 lines):
- Boots configured servers at startup
- RSS watchdog — kills servers exceeding budget
- Ollama schema cache — built once at boot
- Max 1 restart attempt per server
- Node.js blocker — rejects `npx`/`node` commands
- Hard cap: 3 servers total
- Methods: `boot_all()`, `shutdown_all()`, `call_tool(name, args)`, `get_all_schemas_ollama()`, `status()`

**`mcp/config.py`** (96 lines):
- Config file: `~/.demascas/mcp_servers.json`
- Default: one SQLite server example (disabled)
- Fields: name, command, args, env, enabled, max_rss_mb, timeout, description

**`mcp/bridge.py`**:
- Converts between MCP tool format and Ollama/OpenAI tool format
- `mcp_to_ollama(mcp_tool)` — schema conversion

---

## 12. Build System

**`scripts/build.sh`** (159 lines):

```bash
# Prerequisites: brew, cmake, pkg-config, portaudio
# Downloads:
#   - whisper.cpp GGML models (tiny + base)
#   - sherpa-onnx pre-built library (native arm64 TTS runtime)
#   - Kokoro TTS voice model (82MB ONNX)
#   - Piper TTS as additional fallback
# Compiles: CMake Release build

cd daemon/build && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(sysctl -n hw.ncpu)
```

**`daemon/CMakeLists.txt`** (104 lines):

Dependencies:
- **libcurl** — ships with macOS (for MLX HTTP + tool server HTTP)
- **PortAudio** — `brew install portaudio` (audio capture + playback)
- **whisper.cpp** — FetchContent from GitHub v1.7.3 (Metal GPU inference)
- **nlohmann/json** — FetchContent from GitHub v3.11.3 (header-only JSON)
- **sherpa-onnx** — pre-built shared library at `daemon/models/sherpa-onnx/` (Kokoro TTS runtime)
- Apple frameworks: Accelerate (BLAS), CoreAudio, AudioToolbox

Architecture: arm64 only (Apple Silicon native).

**Rebuild command:**
```bash
cd daemon/build && make -j$(sysctl -n hw.ncpu)
```

---

## 13. Launch Sequence

```bash
# Full start (from project root):
./scripts/run.sh

# Manual start (step by step):
source ../venv/bin/activate

# 1. MLX text server
python -m mlx_lm.server --model mlx-community/Phi-4-mini-instruct-4bit --port 8081 &

# 2. (Optional) VLM server
# python scripts/start_vlm_server.py &

# 3. Tool server
python server/tool_server.py &

# 4. Daemon (foreground)
cd daemon
export DYLD_LIBRARY_PATH="${PWD}/models/sherpa-onnx/lib:${DYLD_LIBRARY_PATH:-}"
./build/demascas_daemon
```

**Boot sequence inside daemon:**
1. ToolBridge::wait_for_server() — polls `/health` until tool server ready
2. ToolBridge::fetch_tool_schemas() — loads all 88 schemas
3. LLMClient::prewarm() — verifies MLX server at port 8081
4. LLMClient::load_session() — restores session from `/tmp/demascas_session.json`
5. ShortcutMap construction — builds shortcut lookup tables
6. TTS initialization — tries Kokoro (sherpa-onnx), falls back to macOS `say`
7. AudioEngine::start() — opens PortAudio input stream
8. WhisperEngine::init() — loads ggml-tiny.bin + ggml-base.bin
9. "At your service." → enters SLEEPING state

---

## 14. Data Flow — End to End

### Example: "open my SIH 2025 PDF"

```
1. SLEEPING: Mic captures 2s chunk → whisper tiny → "demascas" detected
2. → LISTENING: "At your service." + Glass sound
3. User speaks: "open my SIH 2025 PDF"
4. capture_utterance() returns audio when 2s silence detected
5. whisper base → "open my S, I, H 2, 0, 2, 5 P, D, F"
6. normalize_transcription():
   - collapse letters: "S, I, H" → "SIH"
   - collapse digits: "2, 0, 2, 5" → "2025"
   - [FILE] tag: "SIH 2025 PDF [FILE]"
   - final: "open my SIH 2025 PDF [FILE]"
7. → PROCESSING
8. PATH routing:
   - Not a shortcut (no match)
   - Not a URL (no URL pattern)
   - Not app_open (not "open [app name]")
   - Not a macro (no macro match)
   - is_simple_question()? → No (has "open" action verb + [FILE] tag)
   - is_multi_step_command()? → Yes ("open" + "find" implied by [FILE])
9. PATH 2.5 (Planner):
   - plan_steps_structured() → [{"tool":"find_file","args":{"folder":"~/Downloads","partial_name":"SIH 2025","extension":"pdf"}},
                                  {"tool":"open_file","args":{"file_path":"{result_of_step_1}"}}]
   - Execute step 1: find_file → {"found": true, "count": 1, "matches": [{"name": "SIH_2025_Problem.pdf", "path": "/Users/.../SIH_2025_Problem.pdf"}]}
   - extract_first_file_path() → "/Users/.../SIH_2025_Problem.pdf"
   - Substitute into step 2: {"file_path": "/Users/.../SIH_2025_Problem.pdf"}
   - Execute step 2: open_file → "Success"
10. on_sentence("Done.") → TTS speaks "Done."
11. log_interaction() → learning engine records episode
12. save_session() → persist to /tmp/demascas_session.json
13. → LISTENING (wait for follow-up command)
```

### Example: "compose an email to Aman"

```
1. Already in LISTENING (follow-up)
2. whisper base → "compose an email to Aman"
3. → PROCESSING
4. PATH routing:
   - FORCE_TOOL_KEYWORDS: "compose" → NOT simple question
   - is_deep_intent_command()? → No ("compose" in TOOL_PATH_KEYWORDS exclusion list)
   - → PATH 3 (Tool ReAct)
5. Tool Router: query contains "compose" + "email" → gmail_browser category
   - Active context shows Brave Browser + mail.google.com URL
   - Returns: [gmail_compose_draft, browser_type, browser_click_text, ...]
6. ReAct iteration 1:
   - LLM sees Rule 12 ("FIRST ask for recipient email if not provided")
   - But "Aman" is in the command...
   - LLM → {"tool": "gmail_compose_draft", "args": {"to": "", "subject": "", "body": ""}}
   - Or LLM might ask: {"done": true, "say": "What's Aman's email address?"}
7. If LLM asks → TTS speaks question → LISTENING for follow-up
8. If LLM calls gmail_compose_draft → browser automation opens compose, fills fields
```

---

## 15. Persistent State & File Locations

| Path | Purpose |
|------|---------|
| `~/.demascas/` | Root config/data directory |
| `~/.demascas/memory/` | ChromaDB persistent storage (all collections) |
| `~/.demascas/user_profile.json` | Learning engine user profile |
| `~/.demascas/web_memory.json` | Per-domain browsing memory |
| `~/.demascas/config.json` | API keys (Google Search) + settings |
| `~/.demascas/mcp_servers.json` | MCP server configuration |
| `~/.demascas/vlm_endpoint.txt` | Discovered VLM server endpoint |
| `/tmp/demascas_session.json` | Session persistence (conversation history) |
| `daemon/models/ggml-tiny.bin` | Whisper tiny GGML model (~39 MB) |
| `daemon/models/ggml-base.bin` | Whisper base GGML model (~140 MB) |
| `daemon/models/kokoro-en-v0_19/` | Kokoro TTS model + tokens + espeak-ng-data |
| `daemon/models/sherpa-onnx/` | sherpa-onnx shared libraries (ONNX Runtime) |
| `daemon/models/piper/` | Piper TTS fallback |

**ChromaDB Collections (all in `~/.demascas/memory/`):**
- `demascas_memory` — user-facing save/retrieve memory
- `demascas_episodes` — learning engine episodic memory (500 cap)
- `demascas_procedures` — knowledge system procedures
- `demascas_facts` — knowledge system facts (+ search cache)
- `demascas_corrections` — knowledge system corrections

---

## 16. Known Issues & Limitations

1. **VLM RAM conflict:** Running VLM (Qwen2.5-VL-3B, ~1.7 GB) alongside text LLM on 8 GB causes swap → 13s inference. Currently disabled by default.

2. **Brave-only browser automation:** All browser tools target "Brave Browser" via AppleScript JS injection. Safari/Chrome would need different AppleScript commands. The app name is hardcoded as `_APP_NAME = "Brave Browser"`.

3. **Accessibility tree blind spots:** `get_active_window_tree()` cannot see web page content inside browsers (only native UI chrome). When Brave is active, the system uses `browser_get_dom_summary()` instead. This is handled by Rule 10 in the system prompt and the tool router.

4. **Whisper hallucinations:** Whisper generates phantom transcriptions from silence/noise ("Thank you", "Subscribe", "Bye", "Subtitles by..."). Filtered by `is_hallucination()` with an extensive blocklist, but edge cases still leak through.

5. **No hardware echo cancellation:** Laptop speakers bleed into mic at 0.15-0.30 RMS. Barge-in threshold (0.18) works for close-range speech but can false-trigger with loud TTS. Anti-echo cooldown helps but isn't perfect.

6. **Single browser tab assumption:** Most browser tools operate on `active tab of window 1`. Multi-tab workflows require explicit `browser_switch_to_tab()`.

7. **Qwen 3B tool hallucinations:** The model sometimes invents nonexistent tools or hallucinates keyboard shortcuts. Mitigated by: filtered tool sets (3-8 per query), strict RULES in system prompt, VALID_TOOLS whitelist in planner, recovery hints on error, REACT_MAX_ERRORS hard stop.

8. **No undo/rollback:** Destructive actions (delete_file, quit_application) are irreversible. The CONFIRMING state provides a safety gate, but only when tools explicitly request it.

9. **macOS permissions required:** Accessibility permission (System Events), Screen Recording (screenshots), and Automation (AppleScript) must be granted to the terminal/daemon.

10. **MLX server cold start:** First inference after boot takes 3-5s (model loading). Prewarm helps but doesn't eliminate it.

---

## 17. Extension Points

### Adding a new tool:

1. **Implement the function** in the appropriate actuator file (or create a new one):
   ```python
   # actuators/new_tool.py
   def my_new_tool(param1: str, param2: int = 0) -> str:
       """Does something useful."""
       ...
       return "Success: did the thing"
   ```

2. **Add schema** to `core/toolbox.py` TOOLS list:
   ```python
   {
       "type": "function",
       "function": {
           "name": "my_new_tool",
           "description": "Does something useful. Use when...",
           "parameters": {
               "type": "object",
               "properties": {
                   "param1": {"type": "string", "description": "..."},
                   "param2": {"type": "integer", "description": "..."}
               },
               "required": ["param1"]
           }
       }
   }
   ```

3. **Register in TOOL_MAP** in `server/tool_server.py`:
   ```python
   from actuators.new_tool import my_new_tool
   TOOL_MAP["my_new_tool"] = my_new_tool
   ```

4. **Add to tool router** category in `server/tool_router.py`:
   ```python
   TOOL_CATEGORIES["relevant_category"].append("my_new_tool")
   INTENT_KEYWORDS["trigger_word"].append("relevant_category")
   ```

### Adding a new PATH (command routing):

In `daemon/src/main.cpp`, add a new `else if` block in the PROCESSING state between the existing paths. You'll need:
- A detection function (like `is_deep_intent_command()`)
- A handler function (like `deep_intent_streaming()`)
- Optionally a new LLM system prompt

### Adding a new MCP server:

Edit `~/.demascas/mcp_servers.json`:
```json
{
    "name": "my_server",
    "command": "python3",
    "args": ["path/to/server.py"],
    "env": {},
    "enabled": true,
    "max_rss_mb": 50,
    "timeout": 5
}
```
Must be Python/Rust (Node.js blocked). Max 3 servers total.

### Switching the LLM model:

1. Update `config.hpp`: `MLX_TEXT_MODEL` constant
2. Update `scripts/run.sh`: `MLX_TEXT_MODEL` variable
3. Adjust `TOOL_CTX`, `TOOL_PREDICT`, `FAST_CTX`, `FAST_PREDICT` for the new model's capabilities
4. May need to adjust RULES in `build_tool_prompt()` for the new model's tendencies

### Adding a new browser:

1. Duplicate browser functions in `actuators/browser.py` with new `_APP_NAME`
2. Or make `_APP_NAME` configurable per-function
3. Update tool router to recognize the new browser name

---

## 18. Quick Reference — All Files

### C++ Daemon (`daemon/src/`)

| File | Lines | Purpose |
|------|-------|---------|
| `main.cpp` | 1471 | State machine entry point — SLEEPING/LISTENING/PROCESSING/CONFIRMING/CLARIFYING |
| `llm_client.cpp` | 1347 | MLX HTTP, system prompts (12 RULES), SSE streaming, ReAct loop, planner, deep intent |
| `llm_client.hpp` | 162 | LLMClient class, LLMAction, DeepIntentAction, SentenceCallback |
| `tool_bridge.cpp` | 271 | libcurl HTTP bridge to Python tool server (port 5001) |
| `tool_bridge.hpp` | ~95 | ToolBridge class, ToolResult, DeepIntentResult |
| `audio_engine.cpp/hpp` | ~250 | PortAudio capture, ring buffer, VAD, barge-in drain |
| `whisper_engine.cpp/hpp` | ~150 | whisper.cpp dual-context (tiny + base), Metal GPU |
| `tts.cpp/hpp` | ~400 | Kokoro (sherpa-onnx) + macOS `say` fallback, sentence queue |
| `shortcut_map.hpp` | 948 | Fast-path routing, FORCE_TOOL_KEYWORDS, is_simple_question, macros |
| `config.hpp` | 108 | All tuning knobs — URLs, models, thresholds, limits |

### Python Server (`server/`)

| File | Lines | Purpose |
|------|-------|---------|
| `tool_server.py` | 1091 | Flask gateway — /tool, /tools, /health, ACTIVE_CONTEXT, normalization |
| `tool_router.py` | 405 | 16-category intent → tool filtering (3-8 per query) |

### Actuators (`actuators/`)

| File | Lines | Purpose |
|------|-------|---------|
| `system_control.py` | 1357 | AppleScript UI automation — click, type, shortcuts, apps |
| `browser.py` | 1207 | Brave Browser JS injection — 30+ browser tools, Gmail |
| `web_agent.py` | 678 | Autonomous browsing — DOM analysis, obstacles, research |
| `communication.py` | 479 | Email compose/reply, contacts, research reports (VLM-powered) |
| `smart_search.py` | 317 | Google/DDG search with fact cache and weather/news |
| `file_system.py` | ~200 | find_file, open_file, list_files, create/delete, PDF summary |
| `network.py` | ~150 | search_web (DDG), read_webpage (BeautifulSoup) |

### Core (`core/`)

| File | Lines | Purpose |
|------|-------|---------|
| `toolbox.py` | 1782 | 80 OpenAI-format tool schemas (TOOLS list) |
| `learning.py` | 1017 | Episodic memory + user profile + context builder |
| `knowledge.py` | 464 | Procedures, facts, corrections (3 ChromaDB collections) |
| `prompt_templates.py` | 352 | Task-specific prompts (compose, reply, research, deep intent) |
| `screen_context.py` | 254 | VLM pre-digestion → compact screen digest (cached 3s) |
| `memory.py` | ~170 | ChromaDB RAG — save/retrieve memories |
| `utils.py` | ~50 | Safe path resolution |

### Memory (`memory/`)

| File | Lines | Purpose |
|------|-------|---------|
| `web_memory.py` | 276 | Per-domain browsing memory (JSON-backed) |

### MCP (`mcp/`)

| File | Lines | Purpose |
|------|-------|---------|
| `client.py` | 282 | Pure-Python MCP client (JSON-RPC 2.0 over stdio) |
| `manager.py` | 307 | Server lifecycle, RSS watchdog, Ollama cache |
| `config.py` | 96 | Server config loader (~/.demascas/mcp_servers.json) |
| `bridge.py` | ~80 | MCP ↔ Ollama schema conversion |

### Scripts

| File | Lines | Purpose |
|------|-------|---------|
| `run.sh` | 184 | Launch all 4 processes, VLM lazy-load |
| `build.sh` | 159 | Prerequisites, model downloads, CMake build |
| `start_vlm_server.py` | ~100 | VLM server with endpoint auto-discovery |

### Config

| File | Purpose |
|------|---------|
| `requirements.txt` | Python deps: flask, chromadb, requests, ddgs, beautifulsoup4, PyPDF2, numpy, ollama |
| `daemon/CMakeLists.txt` | C++ build config: whisper.cpp, nlohmann/json, PortAudio, sherpa-onnx, libcurl |
| `README.md` | User-facing docs (1039 lines) |
| `docs/CLAUDE_CONTEXT.md` | AI context doc (older version) |
| `docs/COPILOT_CONTEXT.md` | AI context doc (older version) |

---

*This document covers the complete DEMASCAS system as of Session 7. Every file, every function, every data flow has been documented. Use this as the definitive reference for continuing development.*
