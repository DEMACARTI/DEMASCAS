# DEMASCAS

> **Decision Engineering Machine Algorithm for System Control and Action Sequencing**

A fully local, privacy-first, autonomous OS-level AI agent for macOS — like JARVIS for your Mac. DEMASCAS can **see** the screen via VLM-powered vision + accessibility trees, **hear** commands via local speech-to-text, **browse the web** autonomously via Brave Browser JS injection, and **execute** actions via native AppleScript function calling. All processing runs 100 % on-device — no data ever leaves your machine.

**Architecture:** Polyglot C++ / Python — bare-metal C++ daemon for the hot path (audio → STT → LLM → TTS) with a lightweight Python tool server for OS actions, browser automation, memory, and learning. LLM inference runs natively on Apple Silicon via **MLX**.

---

## Latest Update — Autonomous Web Agent (Session 7)

DEMASCAS gained a full autonomous web agent with Brave Browser automation via JS injection, per-domain learning memory, obstacle detection (CAPTCHAs, paywalls, cookie banners), and intelligent web research — all without any new pip dependencies. Tool count: **88 tools** (up from 43).

### Brave Browser Automation (`actuators/browser.py` — ~720 lines)
Complete browser control via JavaScript injection through AppleScript — no WebDriver, no Selenium, no external deps:
- **Navigation** — `browser_navigate()`, `browser_new_tab()`, `browser_close_tab()`, `browser_list_tabs()`, `browser_switch_to_tab()`, `browser_back()`, `browser_forward()`, `browser_reload()`
- **Content extraction** — `browser_get_page_text()`, `browser_get_page_html()`, `browser_get_links()`, `browser_get_inputs()`, `browser_get_dom_summary()`
- **Interaction** — `browser_click()` (React-compatible full event chain), `browser_click_text()`, `browser_click_xpath()`, `browser_type()` (native value setter + input/change events for React, `execCommand` for contenteditable), `browser_type_by_label()`, `browser_press_key()`
- **Forms** — `browser_fill_form()`, `browser_select_option()`, `browser_check_checkbox()`, `browser_submit_form()`
- **Gmail** — `gmail_compose_draft()` (fills To/Subject/Body but NEVER auto-sends), `gmail_read_email()`
- **Waiting** — `browser_wait_for_element()`, `browser_wait_for_page_load()`, `browser_element_exists()`

### Autonomous Web Agent (`actuators/web_agent.py` — ~430 lines)
High-level web intelligence built on top of browser.py:
- **`web_analyze_page()`** — Classifies site type (email/video/ecommerce/social/search/docs/news), extracts interactive elements, detects obstacles, records visit to web memory
- **`web_scan_for_obstacles()`** / **`web_handle_obstacle(type)`** — Auto-dismisses cookie banners and popup modals; PAUSES and reports CAPTCHAs, login walls, and paywalls
- **`web_search(query)`** — Google search via Brave, extracts results via DOM parsing
- **`web_read_article(url)`** / **`web_research(topic, num_sources)`** — Multi-source web research
- **`web_execute_goal(goal)`** — Autonomous goal execution: analyze → handle obstacles → classify → execute
- **`web_smart_click(element_description)`** — 4-tier fallback: memory → CSS selector → text match → DOM search
- **`web_youtube_search(query)`** / **`web_youtube_play_first()`** — YouTube-specific automation
- **`web_ecommerce_search(product, site)`** — Product search on Amazon/eBay/etc.

### Per-Domain Web Memory (`memory/web_memory.py` — ~240 lines)
Persistent JSON-backed learning at `~/.demascas/web_memory.json`:
- **`learn_element(url, key, selector, label)`** — Auto-learns CSS selectors for frequently used elements
- **`learn_page_flow(url, name, steps)`** — Remembers multi-step workflows per domain
- **`learn_obstacle(url, key, selector, action)`** — Remembers how to dismiss site-specific obstacles
- **`record_visit(url)`** — Tracks visit counts and timestamps per domain
- **`set_site_type(url, type)`** — Caches site classification (email, video, ecommerce, etc.)
- Domain key normalization — strips protocol/www/path to group by bare domain

### Element Naming Fix (`actuators/system_control.py`)
- **`ELEMENT_NAME_ALIASES`** — 50+ entries mapping LLM snake_case guesses (e.g. `compose_button`) to actual visible UI labels (e.g. `Compose`, `New message`)
- **`normalize_element_name()`** — Returns ordered candidate list: exact → aliases → title-case conversion → first-word capitalized
- **`click_ui_element()`** now iterates all candidates before reporting failure

### VLM Endpoint Auto-Discovery (`sensors/vision.py`)
- Replaced hardcoded `_VLM_URL` with dynamic `_get_vlm_url()` that probes `/v1/chat/completions`, `/chat`, `/generate`
- Discovered endpoint cached to `~/.demascas/vlm_endpoint.txt`
- `start_vlm_server.py` now auto-discovers and writes the endpoint in a background thread

### System Prompt Rules 8-9 (`daemon/src/llm_client.cpp`)
- **Rule 8 (Element Naming):** Use EXACT visible labels from the accessibility tree, not snake_case
- **Rule 9 (Web Agent):** Prefer `browser_*` tools when Brave is active; report CAPTCHAs/paywalls; never auto-send emails

### Smart Tool Routing Updates (`server/tool_router.py`)
6 new categories: `browser`, `web_agent`, `gmail_browser`, `youtube`, `ecommerce` + 50+ new intent keywords for web-related queries.

---

## Previous Updates

<details>
<summary><strong>Session 6 — Knowledge Base + Smart Search</strong></summary>

- **`core/knowledge.py`** — Procedure/fact/correction storage via ChromaDB (`demascas_knowledge` collection)
- **`actuators/smart_search.py`** — Google + DuckDuckGo dual search, `get_weather()`, `get_news()` tools
- **ACTIVE_CONTEXT** — Cross-turn persistent state replacing SESSION_STATE: tracks active_app, active_folder, active_file, active_url, pending goals, file disambiguation
- Tool count reached 43

</details>

<details>
<summary><strong>Session 3 — Architecture Upgrade</strong></summary>

DEMASCAS got a major architecture upgrade — making the 3B model dramatically smarter through architecture, not brute force. All new features stay within the existing ~400 MB RAM headroom (pure Python + small C++ changes = ~0 extra RAM).

### Screen Context Pre-Processor (`core/screen_context.py`)
- **`get_screen_digest()`** — VLM + accessibility tree → structured dict `{app, page_type, url, key_elements, summary}` with 3-second TTL cache.
- **`get_screen_context_block()`** — Compact ~100 token text block injected into system prompts so the LLM always knows what's on screen.
- **`get_tree_only_context()`** — Fast tree-only context (~200 ms, no VLM call) for low-latency paths.
- **`get_intent_aware_digest()`** — Intent-enriched screen digest for PATH 4 deep intent routing.

### Task-Specific Prompt Templates (`core/prompt_templates.py`)
- **`classify_task()`** — Routes commands to task types: `compose_email`, `smart_reply`, `research`, `multi_step`, `schedule`, `message`, `general`.
- **Per-task templates** — `compose_email_prompt()` (3-phase: Understand → Draft → Format), `smart_reply_prompt()` (reads original, matches tone), `research_report_prompt()` (Search → Read → Synthesize), `multi_step_action_prompt()`, `confirm_action_prompt()`.
- **`deep_intent_prompt()`** — PATH 4 master template: Understand → Plan → Execute first action. Structured JSON output with intent/plan/first_action/needs_confirm.
- **`TEMPLATE_REGISTRY`** + `get_template_for_task()` dispatcher.

### JARVIS-Grade Communication Tools (`actuators/communication.py`)
Six new tools that compose VLM screen reading with text model reasoning:
- **`read_current_email`** — VLM reads the email on screen → structured JSON (from, to, subject, body, date).
- **`compose_email`** — Text model + `compose_email_prompt` template → professional draft JSON.
- **`smart_reply`** — VLM reads original email + text model drafts contextual reply matching tone.
- **`confirm_and_execute`** — Queues a pending action → triggers the new CONFIRMING state for human approval.
- **`extract_contact`** — VLM extracts contact info from screen → auto-saves to people graph.
- **`web_research_and_report`** — 3-phase pipeline: `search_web` → `read_webpage` → synthesize report.

### PATH 4: Deep Intent Routing (C++ daemon)
New processing path between PATH 2.5 (planner) and PATH 3 (full tool ReAct loop):
- **`is_deep_intent_command()`** — Keyword matching for email/compose/draft/reply/research/schedule/message/contact.
- **`deep_intent_streaming()`** — Phase 1: Fetches screen context + people context + task-specific prompt from Python server → sends to LLM with extra tokens. Phase 2: Executes first tool from plan, handles confirmations, chains remaining steps.
- **`/deep_intent` endpoint** — Python server returns `{task_type, task_prompt, deep_intent_prompt, screen_context, user_profile, people_context}`.
- **`/screen_context` endpoint** — Returns structured screen digest + compact context block.

### People & Context Memory (`core/learning.py` extension)
- **`add_person()`** — Add/merge contacts (name as key, 100-contact cap).
- **`get_person()`** — Lookup by name (exact + case-insensitive partial match).
- **`find_people()`** — Multi-field search across name, email, org, relationship.
- **`get_people_context()`** — Builds prompt injection block with relevant contacts for current command.
- **`extract_names_from_command()`** — Heuristic proper name extraction from voice commands.
- **Persistent storage** at `~/.demascas/people.json` (100-contact cap, JSON with deferred flush).

### CONFIRMING State (C++ daemon)
Fourth state in the state machine: `SLEEPING → LISTENING → PROCESSING → CONFIRMING`
- **Trigger** — Any action flagged as `needs_confirm` (emails, file deletes, system changes) enters CONFIRMING instead of executing.
- **Flow** — TTS says "Shall I go ahead?", listens for 8 seconds, matches against YES_WORDS (yes/yeah/sure/go ahead/do it/proceed/confirm/absolutely/definitely).
- **Confirm** → executes pending tool, speaks result, returns to LISTENING.
- **Deny / timeout** → cancels action, speaks "Cancelled", returns to LISTENING.

### Previous Session Improvements (still in effect)
- AppleScript fix (`key code 36`), tool alias normalisation (25+ entries), JSON parsing hardened, ReAct dedup + error recovery.
- JARVIS personality, dynamic conversational routing, wake-word echo handling.
- 18 site-name URL aliases, folder macros, shortcut false-positive fix.
- Deep 6-level accessibility tree, retina coordinate correction, click fallback chain.
- TTS chatter filter, JSON stripping, audio self-recovery.
- Planner prompt, session persistence, macro auto-promotion.

</details>

---

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running DEMASCAS](#running-demascas)
  - [Quick Start (One Command)](#quick-start-one-command)
  - [Manual Start (Four Terminals)](#manual-start-four-terminals)
- [Usage Guide](#usage-guide)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [88 Built-in Tools](#88-built-in-tools)
- [72 macOS Shortcuts](#72-macos-shortcuts)
- [Smart Tool Routing](#smart-tool-routing)
- [MCP Integration](#mcp-integration)
- [Continuous Learning](#continuous-learning)
- [Configuration & Tuning](#configuration--tuning)
- [Performance & Optimization](#performance--optimization)
- [Troubleshooting](#troubleshooting)
- [Adding a New Tool](#adding-a-new-tool)
- [License](#license)

---

## Features

- 🎤 **Full-duplex voice** — Barge-in interruption, streaming TTS, energy-based VAD
- 🧠 **Native Apple Silicon LLM** — MLX `Qwen2.5-3B-Instruct-4bit` (~1.8 GB) on port 8081
- 👁️ **Vision-language model** — MLX `Qwen2.5-VL-3B-Instruct-4bit` (~1.6 GB) on port 8082; screenshot-based screen understanding
- 🗣️ **Neural TTS** — Kokoro 82M via sherpa-onnx (native arm64); sentence-queued streaming to PortAudio
- 🖥️ **Screen awareness** — Dual mode: macOS accessibility trees + VLM screenshot analysis
- 🛠️ **88 OS-level tools** — Click, type, wait, clipboard, right-click, switch tabs, open apps/URLs, browse the web, create/delete files, vision queries, compose/read email, smart reply, web research, contact extraction, full browser automation, autonomous web agent
- 🌐 **Autonomous Web Agent** — Brave Browser JS injection, DOM intelligence, obstacle detection (CAPTCHA/paywall/cookie), multi-source research, per-domain learning memory
- 🔍 **Browser Automation** — 35+ Brave Browser tools: navigate, click, type, scroll, forms, Gmail drafts — all via JS injection through AppleScript (no Selenium/WebDriver)
- ⌨️ **72 keyboard shortcuts** — Instant execution without LLM round-trip
- 🎯 **Smart tool filtering** — Query-aware tool router (16 categories, 110+ intent keywords) sends only 3–8 relevant tools per request
- 🧲 **Persistent memory** — ChromaDB RAG for long-term recall + per-domain web memory
- 📚 **Continuous learning** — Episodic memory + user profile + correction tracking + people graph + web memory
- 📖 **Knowledge base** — Procedures, facts, and corrections stored in ChromaDB
- 👥 **People graph** — Auto-learned contacts with names, emails, orgs, relationships
- 🧠 **Screen context pre-processing** — VLM + accessibility tree digest injected into every prompt
- 📋 **Task-specific prompt templates** — Per-task prompts that make the 3B model perform like 70B
- ✅ **Confirmation flow** — CONFIRMING state for risky actions (emails, deletes) — asks before executing
- 🌐 **Web browsing** — Google + DuckDuckGo dual search + webpage reading + weather + news
- 🔌 **MCP support** — Extensible via Model Context Protocol servers (SQLite, GitHub, etc.)
- 🔒 **100 % local** — Zero cloud dependencies, total privacy
- ⚡ **Apple Silicon optimised** — Metal GPU for whisper.cpp, MLX for LLMs, Kokoro for TTS, ~3.4 GB model RAM

---

## Prerequisites

| Requirement | How to Get It |
|-------------|--------------|
| **macOS** (Apple Silicon required) | — |
| **Python 3.10+** | `brew install python@3.12` |
| **Homebrew** | [brew.sh](https://brew.sh) |
| **CMake 3.20+** | `brew install cmake` |
| **PortAudio** | `brew install portaudio` |
| **pkg-config** | `brew install pkg-config` |
| **Microphone** | Built-in or external |

> **Note:** Ollama is only needed for the legacy PDF summarization feature (`summarize_latest_pdf`). The main LLM backend is **MLX** — no Ollama required.

### macOS Permissions Required

Go to **System Settings → Privacy & Security** and grant:

| Permission | Why |
|---|---|
| **Accessibility** → Terminal / iTerm | UI automation (click, type, read screen) |
| **Microphone** → Terminal / iTerm | Voice capture |
| **Screen Recording** → Terminal / iTerm | Screenshots for VLM vision |

---

## Installation

### 1. Clone & enter the project

```bash
cd ~/Desktop
git clone <your-repo-url> OS_RAG   # or copy the folder
cd OS_RAG/DEMASCAS
```

### 2. Create a Python virtual environment

```bash
python3 -m venv ../venv
source ../venv/bin/activate
pip install -r requirements.txt
```

### 3. Install LLM Backend

**For Linux (Ollama - Recommended):**

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull the Gemma 4 model (unified LLM + VLM)
ollama pull gemma4:31b-cloud
```

**For macOS (MLX - Native Apple Silicon):**

```bash
pip install mlx-lm mlx-vlm
```

### 4. Build the C++ daemon

The build script handles everything — dependencies, model downloads, and compilation:

```bash
chmod +x scripts/build.sh scripts/run.sh
./scripts/build.sh
```

This will:
1. Check / install CMake, PortAudio, pkg-config (via pacman on Arch Linux, Homebrew on macOS)
2. Download whisper GGML models (~75 MB tiny + ~147 MB base) to `daemon/models/`
3. Download sherpa-onnx pre-built library (Linux x64 or macOS universal2)
4. Download Kokoro-en-v0\_19 neural TTS model (~335 MB) to `daemon/models/`
5. Install Flask for the Python tool server
6. Compile the C++ daemon via CMake (Release mode)

**Note:** For Linux, use `scripts/build_linux_arch.sh`. For macOS, use `scripts/build_mac.sh`.

On success you'll see:

```
✅  Build complete!
Binary:  daemon/build/demascas_daemon
```

---

## Running DEMASCAS

DEMASCAS runs as multiple processes — an LLM server (Ollama or MLX), a Python tool server, and a C++ daemon.

### Quick Start (One Command)

```bash
# Activate the venv
source ../venv/bin/activate

# Launch all processes (auto-detects platform)
./scripts/run.sh
```

The run script automatically detects your platform and starts:

**Linux (Ollama):**
1. **Ollama server** on `localhost:11434` — gemma4:31b-cloud (unified LLM+VLM)
2. **Python tool server** on `localhost:5001` — 88 tools + MCP + learning
3. **C++ daemon** in foreground — audio → whisper.cpp → Ollama → Kokoro TTS

**macOS (MLX):**
1. **MLX text server** on `localhost:8081` — Qwen2.5-3B-Instruct-4bit
2. **MLX vision server** on `localhost:8082` — Qwen2.5-VL-3B-Instruct-4bit (optional)
3. **Python tool server** on `localhost:5001` — 88 tools + MCP + learning
4. **C++ daemon** in foreground — audio → whisper.cpp → MLX → Kokoro TTS

```
[*] LLM server ready (Ollama/MLX)
[*] Starting Python tool server (localhost:5001)...
[*] Starting C++ daemon...

╔══════════════════════════════════════════╗
║   DEMASCAS — Bare-Metal C++ Daemon       ║
║   whisper.cpp • LLM • Kokoro TTS         ║
╚══════════════════════════════════════════╝

[✓] Microphone opened (16 kHz mono, PortAudio)
[✓] Whisper models loaded

[*] DEMASCAS is ready.  Say "demascas" to activate!
```

**Stop:** Press `Ctrl+C` — the script automatically kills all processes.

---

### Manual Start (Multiple Terminals)

If you prefer to run the processes separately (useful for debugging):

**Terminal 1 — LLM Server:**

```bash
# Linux (Ollama):
ollama serve
ollama run gemma4:31b-cloud

# macOS (MLX):
source ../venv/bin/activate
python -m mlx_lm.server --model mlx-community/Qwen2.5-3B-Instruct-4bit --port 8081
```

**Terminal 2 — VLM Server (optional, for screen analysis):**

```bash
source ../venv/bin/activate
python scripts/start_vlm_server.py
```

**Terminal 3 — Python tool server:**

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

**Terminal 4 — C++ daemon (voice engine):**

```bash
cd ~/Desktop/OS_RAG/DEMASCAS/daemon
export DYLD_LIBRARY_PATH="$PWD/models/sherpa-onnx/lib:${DYLD_LIBRARY_PATH:-}"
./build/demascas_daemon
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
| "Open Safari" | Launches Safari via AppleScript (PATH 1.6: app open) |
| "Close this window" | Sends ⌘W to the active app (shortcut path) |
| "Take a screenshot" | Sends ⌘⇧3 (shortcut path) |
| "Search the web for best restaurants in Delhi" | Runs a DuckDuckGo search and reads results aloud |
| "What's on my screen right now?" | Reads the accessibility tree or uses VLM screenshot analysis |
| "Remember that my meeting is at 3 PM tomorrow" | Saves to persistent ChromaDB memory |
| "What did I tell you about my meeting?" | Retrieves from memory |
| "Open Instagram in Safari" | Opens instagram.com in Safari (PATH 1.5: URL detection) |
| "Open Google in Safari" | Opens google.com in Safari (PATH 1.5: site-name resolution) |
| "Open Gmail" | Multi-step macro: opens Safari → navigates to gmail.com (PATH 1.8) |
| "Google best restaurants in Delhi" | Macro: opens Safari → types search → submits (PATH 1.8) |
| "Search YouTube for lo-fi beats" | Opens YouTube in Brave and searches (web agent) |
| "Play the first video" | Clicks the first YouTube result (web agent) |
| "Compose a Gmail draft to John about the meeting" | Opens Gmail, fills To/Subject/Body — never sends (browser) |
| "What's on this web page?" | Analyzes the current page via DOM summary (browser) |
| "Click the Sign In button" | JS injection click on web element (browser) |
| "Fill in the search box with machine learning" | Types into web form field (browser) |
| "Search Amazon for wireless headphones" | Navigates to Amazon, searches product (web agent) |
| "Research quantum computing" | Multi-source web research: search → read → synthesize (web agent) |
| "Handle the cookie popup" | Auto-dismisses cookie consent banner (web agent) |
| "Summarize my latest PDF" | Reads the most recent PDF on the Desktop |
| "Right-click that button" | Uses `right_click_element` tool |
| "Copy what's on the clipboard" | Uses `get_clipboard` tool |
| "Wait for the download to finish" | Uses `wait_for_element` tool |
| "Undo that" | Sends ⌘Z instantly (shortcut path, no LLM) |
| "Minimize" | Sends ⌘M instantly |
| "What time is it?" | Answers directly (fast path, no tools) |
| "My name is Daksh" | Saved to user profile — all future responses personalized |
| "Greet my friend Saf" | Speaks directly: "Hey Saf! Great to meet you!" (fast Q&A) |
| "Tell me a joke" | Tells a joke directly (fast Q&A, no tools) |

### Ten Processing Paths

DEMASCAS uses a tiered processing strategy for maximum speed:

| Path | Name | Latency | Description |
|------|------|---------|-------------|
| **1** | Shortcut | <1 µs | 72 macOS shortcuts matched by keyword in C++; bypasses LLM |
| **1.5** | URL Detection | <1 ms | Deterministic URL/site-name extraction → `open_url` (18 site aliases) |
| **1.6** | App Open | <1 ms | "Open X" → direct AppleScript launch |
| **1.7** | App Close | <1 ms | "Quit X" / "Close X" → direct AppleScript quit |
| **1.8** | Macro | <5 ms | Multi-step sequences (Gmail, YouTube, Google search) |
| **2** | Fast Q&A | ~300–800 ms | Conversation, greetings, questions (3B, no tools; action-verb heuristic) |
| **2.5** | Multi-step Planner | ~1–3 s | Complex commands decomposed into sequential steps |
| **4** | Deep Intent | ~2–5 s | Email, research, scheduling — task-specific prompts + screen context + people graph |
| **3** | Tool LLM | ~1–4 s | Full ReAct loop with dedup detection, history isolation, alias normalisation |

---

## Architecture

### Cross-Platform LLM Backend

DEMASCAS supports multiple LLM backends for cross-platform compatibility:

| Platform | Backend | Model | RAM | Port | Role |
|----------|---------|-------|-----|------|------|
| **Linux** | Ollama (Cloud) | `gemma4:31b-cloud` | 0 GB (cloud) | 11434 | All interactions: OS tools, shortcuts, Q&A, planning + Vision |
| **macOS** | MLX | `Qwen2.5-3B-Instruct-4bit` | ~1.8 GB | 8081 | All interactions: OS tools, shortcuts, Q&A, planning |
| **Both** | Ollama (optional) | `gemma4:31b-cloud` | 0 GB (cloud) | 11434 | Unified vision + text understanding (optional) |

Both backends expose OpenAI-compatible `/v1/chat/completions` endpoints. The C++ daemon communicates via streaming HTTP (libcurl SSE).

### Processing Pipeline

```
User speaks → PortAudio → whisper.cpp (Metal GPU)
                                │
                         ┌──────┴──────┐
                         │  Nano-Router │  (C++, <1 µs)
                         └──────┬──────┘
         ┌──────────┬──────────┼──────────┬──────────┬──────────┐
         ▼          ▼          ▼          ▼          ▼          ▼
      PATH 1    PATH 1.5-1.8  PATH 2    PATH 2.5   PATH 4   PATH 3
     Shortcut   URL / App /   Fast Q&A  Multi-step Deep     Tool LLM
     (72 keys)  Close / Macro (3B)      Planner    Intent   (3B+tools)
      <1 µs      <5 ms       ~500 ms    ~1-3 s     ~2-5 s   ~1-4 s
         │          │          │          │          │          │
         └──────────┴──────────┴──────────┴──────────┴──────────┘
                                                    │
                                             ┌──────┴──────┐
                                             │  CONFIRMING  │
                                             │  (yes/no)    │
                                             └─────────────┘
                                  │
                          ┌───────┴───────┐
                          │   Kokoro TTS  │  (sentence-by-sentence)
                          │  24kHz neural │
                          └───────────────┘
```

### System Diagram

```
┌────────────────────────────────────────────────────────────────┐
│                LLM SERVER (cross-platform)                      │
│                                                                │
│  Linux: Ollama (port 11434)     macOS: MLX (port 8081)        │
│  gemma4:31b-cloud              Qwen2.5-3B-Instruct-4bit       │
│  0 GB (cloud)                  ~1.8 GB (text)                  │
│  OpenAI-compatible /v1/chat/completions                        │
└───────────────────────────┬────────────────────────────────────┘
                            │ HTTP streaming
┌───────────────────────────┼────────────────────────────────────┐
│             C++ DAEMON (bare metal, hot path)                  │
│                                                                │
│  PortAudio ──→ VAD ──→ whisper.cpp ──→ Nano-Router ─────────┐ │
│  ring buffer   energy   tiny+base      8 paths: shortcut /  │ │
│  16 kHz mono   ~32ms    Metal GPU      url / app / macro /  │ │
│                                        fast / plan / tool    │ │
│                                                              │ │
│  ┌── TTS (streaming, sentence queue) ←───────────────────────┘ │
│  │   Kokoro 82M (sherpa-onnx) │ float32 24kHz │ <5ms kill     │
│  │   macOS `say` fallback     │ PID tracking                  │
│  └───────────────────┘                                        │
│       │                                                       │
│  CONFIRMING state (PATH 4): TTS asks "Shall I go ahead?"     │
│    → listens 8s → yes/no → execute or cancel → LISTENING      │
│       │                                                       │
│       │ HTTP localhost:5001                                   │
└───────┼───────────────────────────────────────────────────────┘
        ▼
┌───────────────────────────────────────────────────────────────┐
│          PYTHON TOOL SERVER (Flask, 88 tools)                  │
│                                                                │
│  /tool            → execute a native or MCP tool               │
│  /tools           → list all available tool schemas            │
│  /tools_for_query → filtered tools (3-8 per query via router)  │
│  /health          → liveness probe                             │
│  /context         → learning context (episodes + profile)      │
│  /learn           → log interaction for learning               │
│  /screen_context  → VLM + a11y screen digest                   │
│  /deep_intent     → task classification + prompt + people       │
│  /session         → cross-turn active context (ACTIVE_CONTEXT)  │
│  /mcp/status      → MCP server monitoring                      │
│                                                                │
│  ┌──────────────┐  ┌──────────┐  ┌─────────────────────┐      │
│  │ Native Tools │  │ MCP      │  │ Learning Engine     │      │
│  │ (88 tools:   │  │ Bridge   │  │ (episodes, profile, │      │
│  │  OS, files,  │  │ (stdio)  │  │  context, people    │      │
│  │  web, vision,│  └──────────┘  │  graph, knowledge)  │      │
│  │  email,      │                └─────────────────────┘      │
│  │  browser,    │                                              │
│  │  web agent,  │                                              │
│  │  research)   │                                              │
│  └──────────────┘                                              │
│  ┌──────────────┐  ┌──────────┐  ┌─────────────────────┐      │
│  │ ChromaDB     │  │ Vision   │  │ Tool Router         │      │
│  │ (shared      │  │ (a11y +  │  │ (16 categories,     │      │
│  │  client)     │  │  VLM)    │  │  110+ intent keys)  │      │
│  └──────────────┘  └──────────┘  └─────────────────────┘      │
│  ┌──────────────┐  ┌───────────────────┐                      │
│  │ Screen       │  │ Prompt Templates  │                      │
│  │ Context      │  │ (task-specific    │                      │
│  │ (VLM digest) │  │  deep intent)     │                      │
│  └──────────────┘  └───────────────────┘                      │
└───────────────────────────────────────────────────────────────┘
                          │
              ~/.demascas/ (persistent storage)
    ┌────────────────────────────────────────────┐
    │  memory/             → ChromaDB vectors     │
    │  user_profile.json   → learned preferences  │
    │  people.json         → people graph          │
    │  web_memory.json     → per-domain web memory │
    │  vlm_endpoint.txt    → VLM endpoint cache    │
    │  mcp_servers.json    → MCP server config     │
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
│   ├── toolbox.py               #   88 tool schemas (OpenAI-compatible)
│   ├── memory.py                #   ChromaDB RAG persistent memory
│   ├── learning.py              #   Continuous learning engine (3 layers) + people graph
│   ├── knowledge.py             #   Procedures, facts, corrections via ChromaDB
│   ├── screen_context.py        #   VLM + a11y screen digest with 3s TTL cache
│   ├── prompt_templates.py      #   Task-specific prompt templates + deep intent
│   └── utils.py                 #   Path resolution, safety helpers
│
├── sensors/                     # The "Eyes" (Python)
│   └── vision.py                #   Accessibility tree + VLM vision (auto-discovers endpoint)
│
├── actuators/                   # The "Hands" (Python)
│   ├── system_control.py        #   Click, type, open, shortcut, quit, scroll, drag,
│   │                            #   wait, clipboard, right-click, find-and-click,
│   │                            #   element name aliases (50+ entries)
│   ├── browser.py               #   Brave Browser automation via JS injection (~720 lines)
│   │                            #   35+ tools: navigate, click, type, scroll, forms, Gmail
│   ├── web_agent.py             #   Autonomous web agent (~430 lines) — DOM intelligence,
│   │                            #   obstacle detection, web search, research, smart click
│   ├── file_system.py           #   PDF summary, file listing, create/delete files
│   ├── network.py               #   Web search (DuckDuckGo) + webpage reading
│   ├── communication.py         #   Email compose/read, smart reply, research
│   └── smart_search.py          #   Google + DuckDuckGo dual search, weather, news
│
├── memory/                      # Persistent memory modules
│   └── web_memory.py            #   Per-domain web memory (JSON at ~/.demascas/web_memory.json)
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
│   ├── models/                  #   Models & libs (auto-downloaded by build.sh)
│   │   ├── ggml-tiny.bin        #     ~75 MB — wake-word detection
│   │   ├── ggml-base.bin        #     ~147 MB — command transcription
│   │   ├── kokoro-en-v0_19/     #     ~335 MB — Kokoro neural TTS model
│   │   │   ├── model.onnx       #       82M-param StyleTTS 2 (24 kHz, 11 voices)
│   │   │   ├── tokens.txt       #       Vocabulary
│   │   │   └── espeak-ng-data/  #       Phonemizer data
│   │   └── sherpa-onnx/         #     Pre-built shared library (universal2)
│   │       ├── lib/             #       libsherpa-onnx-c-api.dylib, libonnxruntime
│   │       └── include/         #       C API header
│   └── src/
│       ├── main.cpp             #   State machine (SLEEPING → LISTENING → PROCESSING → CONFIRMING)
│       ├── config.hpp           #   All 37 tuning knobs in one place
│       ├── audio_engine.hpp/cpp #   PortAudio 16 kHz capture + energy-based VAD
│       ├── whisper_engine.hpp/cpp # whisper.cpp integration (Metal GPU)
│       ├── llm_client.hpp/cpp   #   Streaming MLX (9 rules, ReAct loop, deep intent, session)
│       ├── tool_bridge.hpp/cpp  #   HTTP bridge to Python tool server (+ screen context + deep intent)
│       ├── tts.hpp/cpp          #   Kokoro neural TTS (sherpa-onnx) + macOS `say` fallback
│       └── shortcut_map.hpp     #   72 shortcuts + macros (header-only)
│
├── server/                      # Python Tool Micro-service
│   ├── tool_server.py           #   Flask on localhost:5001, 11 endpoints + MCP + learning
│   └── tool_router.py           #   Smart query-aware tool filtering (16 categories)
│
├── scripts/                     # Build & Run helpers
│   ├── build.sh                 #   One-command build (deps + models + compile)
│   ├── run.sh                   #   One-command run (4 processes: MLX + server + daemon)
│   └── start_vlm_server.py      #   MLX VLM server launcher (auto-discovers endpoint)
│
└── docs/
    ├── CLAUDE_CONTEXT.md        #   Detailed project context for AI assistants
    └── COPILOT_CONTEXT.md       #   AI coding assistant context (legacy)
```

---

## 88 Built-in Tools

### OS & UI Control (20 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 1 | `open_application` | system_control | Launch any macOS app by name |
| 2 | `open_url` | system_control | Open a URL/website in a browser |
| 3 | `press_keyboard_shortcut` | system_control | Press any key combo (⌘, ⌥, ⌃, ⇧) |
| 4 | `click_ui_element` | system_control | Click a UI element by name (with alias resolution) |
| 5 | `type_text` | system_control | Type text into any focused field (+ optional Enter) |
| 6 | `type_text_into_element` | system_control | Type text into a named UI tree element |
| 7 | `quit_application` | system_control | Gracefully quit a specific app |
| 8 | `activate_application` | system_control | Bring an app to the foreground |
| 9 | `list_running_applications` | system_control | List all running GUI applications |
| 10 | `scroll_direction` | system_control | Scroll up/down/left/right in an app |
| 11 | `drag_element` | system_control | Drag from one element to another |
| 12 | `wait_for_element` | system_control | Wait for a UI element to appear (polling) |
| 13 | `wait_for_app_ready` | system_control | Wait for an application to be fully responsive |
| 14 | `get_clipboard` | system_control | Read the current clipboard contents |
| 15 | `set_clipboard` | system_control | Write text to the clipboard |
| 16 | `find_and_click` | system_control | Search for and click a UI element by partial match |
| 17 | `right_click_element` | system_control | Right-click (context menu) a UI element |
| 18 | `type_and_submit` | system_control | Type text and submit (type + Enter) |
| 19 | `switch_tab` | system_control | Switch to a browser tab by name or index |
| 20 | `scroll_to_element` | system_control | Scroll until a specific element is visible |

### Browser Automation (33 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 21 | `browser_execute_js` | browser | Execute raw JavaScript in Brave Browser |
| 22 | `browser_get_url` | browser | Get the current page URL |
| 23 | `browser_get_title` | browser | Get the current page title |
| 24 | `browser_navigate` | browser | Navigate to a URL |
| 25 | `browser_navigate_and_wait` | browser | Navigate and wait for page load |
| 26 | `browser_new_tab` | browser | Open a new tab (optionally with URL) |
| 27 | `browser_close_tab` | browser | Close the current tab |
| 28 | `browser_list_tabs` | browser | List all open tabs |
| 29 | `browser_switch_to_tab` | browser | Switch to a tab by index |
| 30 | `browser_back` | browser | Navigate back |
| 31 | `browser_forward` | browser | Navigate forward |
| 32 | `browser_reload` | browser | Reload the current page |
| 33 | `browser_get_page_text` | browser | Extract visible text from the page |
| 34 | `browser_get_page_html` | browser | Get raw HTML of the page |
| 35 | `browser_get_links` | browser | Extract all links from the page |
| 36 | `browser_get_inputs` | browser | List all form inputs on the page |
| 37 | `browser_click` | browser | Click an element by CSS selector (React-compatible) |
| 38 | `browser_click_text` | browser | Click an element by its visible text |
| 39 | `browser_click_xpath` | browser | Click an element by XPath |
| 40 | `browser_type` | browser | Type into a field by CSS selector (React + contenteditable) |
| 41 | `browser_type_by_label` | browser | Type into a field by its label text |
| 42 | `browser_press_key` | browser | Dispatch a keyboard event |
| 43 | `browser_scroll` | browser | Scroll the page up/down |
| 44 | `browser_scroll_to_element` | browser | Scroll to a specific element |
| 45 | `browser_fill_form` | browser | Fill multiple form fields at once |
| 46 | `browser_select_option` | browser | Select a dropdown option |
| 47 | `browser_check_checkbox` | browser | Check or uncheck a checkbox |
| 48 | `browser_submit_form` | browser | Submit a form |
| 49 | `browser_wait_for_element` | browser | Wait for a CSS selector to appear |
| 50 | `browser_wait_for_page_load` | browser | Wait for page to finish loading |
| 51 | `browser_element_exists` | browser | Check if an element exists on the page |
| 52 | `browser_get_dom_summary` | browser | Get structured DOM summary (interactive elements) |
| 53 | `gmail_compose_draft` | browser | Compose a Gmail draft (NEVER auto-sends) |

### Web Agent (11 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 54 | `web_analyze_page` | web_agent | Classify site, extract elements, detect obstacles |
| 55 | `web_scan_for_obstacles` | web_agent | Detect cookie banners, CAPTCHAs, login walls, popups |
| 56 | `web_handle_obstacle` | web_agent | Auto-dismiss cookies/popups; pause for CAPTCHAs |
| 57 | `web_search` | web_agent | Google search via Brave with result extraction |
| 58 | `web_read_article` | web_agent | Navigate to URL and extract article text |
| 59 | `web_research` | web_agent | Multi-source research (search + read N sources) |
| 60 | `web_execute_goal` | web_agent | Autonomous goal execution on current page |
| 61 | `web_smart_click` | web_agent | Click by description (memory → selector → text → DOM) |
| 62 | `web_youtube_search` | web_agent | Search YouTube |
| 63 | `web_youtube_play_first` | web_agent | Play the first YouTube result |
| 64 | `web_ecommerce_search` | web_agent | Search for a product on e-commerce sites |

### Vision & Screen Reading (3 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 65 | `get_active_window_tree` | vision | Read the accessibility tree (screen content) |
| 66 | `list_open_windows` | vision | List all open windows across all apps |
| 67 | `vision_execute` | vision | VLM-powered screenshot analysis and action |

### File Management (6 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 68 | `list_files_in_folder` | file_system | List files in a directory |
| 69 | `create_file` | file_system | Create a new file (asks permission first) |
| 70 | `delete_file` | file_system | Move a file to Trash (asks permission first) |
| 71 | `summarize_latest_pdf` | file_system | Read and summarize the latest PDF on Desktop |
| 72 | `find_file` | file_system | Search for files by partial name |
| 73 | `open_file` | file_system | Open a file at a given path |

### Web & Search (5 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 74 | `search_web` | network | Search DuckDuckGo and return results |
| 75 | `read_webpage` | network | Fetch and extract text from a URL |
| 76 | `smart_search` | smart_search | Google + DuckDuckGo dual search |
| 77 | `get_weather` | smart_search | Get current weather for a location |
| 78 | `get_news` | smart_search | Get latest news headlines |

### Memory (2 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 79 | `save_memory` | memory | Store a fact in persistent ChromaDB |
| 80 | `retrieve_memories` | memory | Search stored memories by query |

### Communication (8 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 81 | `read_current_email` | communication | VLM reads email on screen → structured JSON |
| 82 | `compose_email` | communication | Draft a professional email using task-specific prompts |
| 83 | `smart_reply` | communication | Read original email + draft contextual reply |
| 84 | `confirm_and_execute` | communication | Queue action for human confirmation (CONFIRMING state) |
| 85 | `extract_contact` | communication | VLM extracts contact info → saves to people graph |
| 86 | `web_research_and_report` | communication | 3-phase: search → read → synthesize report |
| 87 | `gmail_read_email` | browser | Read email content via Brave JS injection |
| 88 | `open_url_and_wait` | system_control | Open URL and wait for page load |

---

## 72 macOS Shortcuts

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

Shortcuts are organized into 5 categories: Window/App (21), Editing (14), File (5), System (23), Browser (9).

### Built-in Macros

Multi-step sequences that fire without LLM involvement:

| Voice Command | What Happens |
|---|---|
| "Open Gmail" | Opens Safari → navigates to gmail.com |
| "Open YouTube" | Opens Safari → navigates to youtube.com |
| "Google \<query\>" | Opens Safari → types query in Google → submits |
| "Open Downloads folder" | Opens Finder → ⌘⌥L |
| "Open Documents folder" | Opens Finder → ⌘⇧O |
| "Open Desktop folder" | Opens Finder → ⌘⇧D |
| "New tab" | Opens a new browser tab |
| "Close tab" | Closes current browser tab |
| "Go back" | Navigates back in browser |

**18 site-name aliases** resolve without a TLD — "Open Google in Safari", "Open YouTube", "Open Reddit", etc. all hit PATH 1.5 deterministically.

> Commands like "Open Safari" or "Summarize my PDF" are **not** matched as shortcuts — they route through the LLM tool-calling path for proper handling.

---

## Smart Tool Routing

The `server/tool_router.py` module provides intelligent query-aware tool filtering. Instead of sending all 88 tools to the LLM (wasting context window), it classifies each query and returns only **3–8 relevant tools**.

**16 tool categories** with 110+ intent keywords:
- UI interaction, app management, web browsing, browser automation, web agent, Gmail, YouTube, e-commerce, file management, screen reading, vision, keyboard, memory, clipboard, typing, realtime/weather

The C++ daemon calls `POST /tools_for_query` before each tool-calling LLM invocation.

**Tool alias normalisation** runs server-side: if the LLM hallucinates a tool name (e.g. `click` instead of `click_ui_element`, `navigate` instead of `browser_navigate`), the server remaps it transparently via a 45+ entry alias table — no round-trip wasted. Argument aliases are also normalized (e.g. `element` → `selector`, `website` → `url`).

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
| **People Graph** | Contacts: name, email, phone, org, relationship | `~/.demascas/people.json` | ≤100 contacts |
| **Knowledge Base** | Procedures, facts, corrections | ChromaDB `demascas_knowledge` | Unbounded |
| **Web Memory** | Per-domain selectors, flows, obstacles, site types | `~/.demascas/web_memory.json` | Per-domain |
| **Context Builder** | Injects top-3 similar episodes + profile + people + knowledge into LLM prompt | Transient (in-memory) | ~200 tokens |

**Zero overhead:** Reuses the same ChromaDB client as RAG memory. Profile writes are batched (1 flush per 5 commands). No extra LLM calls, no background threads.

The C++ daemon calls `GET /context?query=...` before each LLM invocation and `POST /learn` after each command completes.

---

## Configuration & Tuning

All 37 tuning knobs live in a single header: `daemon/src/config.hpp`

### LLM Configuration

| Knob | Linux Default | macOS Default | Description |
|------|---------------|---------------|-------------|
| `MLX_TEXT_URL` | `"http://127.0.0.1:11434/v1/chat/completions"` | `"http://127.0.0.1:8081/v1/chat/completions"` | LLM endpoint (Ollama/MLX) |
| `MLX_VL_URL` | `"http://127.0.0.1:11434/v1/chat/completions"` | `"http://127.0.0.1:8082/v1/chat/completions"` | VLM endpoint |
| `MLX_TEXT_MODEL` | `"gemma4:31b-cloud"` | `"mlx-community/Qwen2.5-3B-Instruct-4bit"` | Text LLM model |
| `MLX_VL_MODEL` | `"gemma4:31b-cloud"` | `"mlx-community/Qwen2.5-VL-3B-Instruct-4bit"` | Vision LLM model |
| `TOOL_CTX` | `2048` | `2048` | Context window for tool-calling path |
| `TOOL_PREDICT` | `200` | `200` | Max tokens for tool-calling responses |
| `FAST_CTX` | `1024` | `1024` | Context window for simple Q&A |
| `FAST_PREDICT` | `80` | `80` | Max tokens for simple Q&A |
| `LLM_TEMPERATURE` | `0.1f` | `0.1f` | Low temp = deterministic tool calls |

**Note:** The C++ daemon uses compile-time `#ifdef __linux__` to select the correct defaults.

### Audio & Voice

| Knob | Default | Description |
|------|---------|-------------|
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `FRAMES_PER_BUFFER` | `512` | Samples per audio buffer |
| `VAD_ENERGY_THRESHOLD` | `0.005f` | Voice activity detection sensitivity |
| `BARGE_IN_THRESHOLD` | `0.15f` | Speech-over-TTS detection (avoids self-listening) |
| `SILENCE_DURATION` | `2.0f` | Seconds of silence before end-of-utterance |
| `WAKE_LISTEN_WINDOW` | `2.0f` | Seconds per wake-word scan |
| `COMMAND_TIMEOUT` | `10.0f` | Max seconds to wait for first command |
| `FOLLOWUP_TIMEOUT` | `15.0f` | Max seconds between follow-ups |
| `TTS_COOLDOWN_SEC` | `1.0f` | Seconds to ignore mic after TTS finishes (anti-echo) |
| `INPUT_DEVICE_INDEX` | `-1` | Audio input device (-1 = system default) |
| `WAKE_WORD` | `"demascas"` | Activation keyword |

### TTS Configuration

| Knob | Default | Description |
|------|---------|-------------|
| `USE_KOKORO` | `true` | Enable Kokoro neural TTS (set `false` for macOS `say`) |
| `KOKORO_MODEL_DIR` | `"models/kokoro-en-v0_19"` | Path to Kokoro model directory |
| `KOKORO_SPEAKER_ID` | `0` | Speaker (0=af default female; 11 voices available) |
| `KOKORO_SPEED` | `1.0f` | Speech speed (< 1 faster, > 1 slower) |
| `KOKORO_SAMPLE_RATE` | `24000` | Kokoro output sample rate (Hz) |
| `TTS_VOICE` | `"Samantha"` | macOS `say` fallback voice |
| `TTS_RATE` | `190` | macOS `say` words per minute |

### Conversation & Whisper

| Knob | Default | Description |
|------|---------|-------------|
| `MAX_HISTORY` | `4` | Conversation turns kept in context |
| `MAX_TOOL_DEPTH` | `5` | Max ReAct loop iterations |
| `MAX_CONSECUTIVE_BLANKS` | `2` | Blank transcriptions before returning to sleep |
| `WHISPER_PROMPT` | *(vocabulary hint)* | Expected vocabulary to reduce hallucinations |

After changing config, rebuild with `./scripts/build.sh`.

---

## Performance & Optimization

### Latency Budget (per command)

| Phase | Time | Implementation |
|---|---|---|
| VAD speech detection | ~32 ms | 512-sample energy threshold (C++) |
| STT transcription | ~200 ms | whisper.cpp tiny on Metal GPU |
| LLM first token | ~400 ms | Streaming MLX via libcurl (OpenAI-compatible) |
| TTS first word | ~500 ms | Kokoro neural inference + sentence queue |
| Tool execution | ~5–2000 ms | AppleScript/subprocess (OS-dominated) |
| Learning overhead | <5 ms | ChromaDB search + in-memory profile update |

### RAM Budget (8 GB Apple Silicon)

| Component | RAM |
|---|---|
| Qwen 2.5 3B (MLX text server) | ~1.8 GB |
| Qwen 2.5 VL 3B (MLX vision server) | ~1.6 GB |
| whisper.cpp tiny + base | ~222 MB |
| Kokoro TTS model | ~335 MB |
| sherpa-onnx runtime | ~28 MB |
| C++ daemon | ~15 MB |
| ChromaDB (shared client + embeddings) | ~80 MB |
| Python tool server | ~30 MB |
| MCP servers (if any, max 3) | ≤300 MB |
| **Total** | **~4.4 GB** (leaves ~3.6 GB for macOS) |

> Without the optional VLM server: **~2.8 GB** (leaves ~5.2 GB for macOS).

### Key Optimizations

- **MLX-native inference** — No Ollama overhead; models run directly on Apple Silicon unified memory
- **Smart tool filtering** — Only 3–8 relevant tools sent per query (vs. all 88), saving ~60% context
- **Single ChromaDB client** — memory.py, learning.py, and knowledge.py share one embedding model (~80 MB saved)
- **Streaming everything** — LLM tokens stream to TTS sentence queue; no waiting for full response
- **Barge-in interrupt** — VAD detects speech → aborts PortAudio output in <5 ms, cancels LLM stream
- **Native arm64 TTS** — Kokoro via sherpa-onnx (no Rosetta 2); CPU-only so GPU stays free for whisper Metal
- **Anti-echo suppression** — TTS cooldown + mic drain prevents self-listening loops
- **Whisper prompt context** — Initial vocabulary hint reduces hallucinations by ~60%
- **Batched profile writes** — 1 disk flush per 5 interactions (80% fewer SSD writes)
- **No Python on the hot path** — Audio, STT, LLM, TTS all run in C++
- **Bounded collections** — Episodes capped at 500, profile fields capped, no unbounded growth
- **History isolation** — Tool path clears history per command, preventing cross-command LLM confusion
- **ReAct dedup** — Duplicate tool+args detection breaks infinite loops on first repeat
- **TTS chatter filter** — Skips JSON blobs and technical narration before they reach the speaker

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **MLX text server won't start** | Check `pip install mlx-lm` is installed; verify port 8081 is free |
| **MLX VLM server won't start** | Optional — system works without it. Check `pip install mlx-vlm` |
| **"Connection refused" on port 8081** | MLX text server isn't running. Start with `./scripts/run.sh` or manually |
| **Port 8081/8082 in use** | `lsof -ti:8081 \| xargs kill` and retry |
| **No microphone input** | System Settings → Privacy → Microphone → enable Terminal |
| **"Accessibility not allowed"** | System Settings → Privacy → Accessibility → enable Terminal / VS Code |
| **Port 5001 in use** | Kill it: `lsof -ti:5001 \| xargs kill`, then retry |
| **Daemon not built** | Run `./scripts/build.sh` first |
| **CMake not found** | `brew install cmake` |
| **PortAudio not found** | `brew install portaudio` |
| **Whisper models missing** | `./scripts/build.sh` auto-downloads them |
| **Kokoro model missing** | `./scripts/build.sh` auto-downloads it (~335 MB) |
| **sherpa-onnx dylib error** | Ensure `daemon/models/sherpa-onnx/lib/` exists; re-run `./scripts/build.sh` |
| **TTS falls back to `say`** | Check that `USE_KOKORO = true` in `config.hpp` and model files are present |
| **"Permission denied" on scripts** | `chmod +x scripts/build.sh scripts/run.sh` |
| **Wrong app closed** | DEMASCAS distinguishes "close window" (⌘W) from "quit app" (⌘Q) |
| **Shortcut fires for "Open Safari"** | By design — "open" commands route to the LLM tool path |
| **LLM repeats same tool call in a loop** | Fixed — ReAct dedup breaks on first duplicate. If still occurring, check `MAX_TOOL_DEPTH` in `config.hpp` |
| **LLM says "you can say X" instead of speaking** | Fixed — system prompt defines DEMASCAS as a character. If persists, clear `/tmp/demascas_session.json` |
| **"Open Google" goes to LLM instead of URL** | Fixed — 18 site aliases resolve bare names (google, youtube, etc.) deterministically |
| **Tool server errors in daemon** | Check tool server terminal logs; ensure `python main.py` is running |
| **Learning not working** | Check `~/.demascas/` exists; run `cat ~/.demascas/user_profile.json` |
| **PDF summarization fails** | This feature requires Ollama: `brew install ollama && ollama pull gemma4:31b-cloud` |

---

## Adding a New Tool

1. **Write the Python function** in the appropriate `actuators/` or `sensors/` module
2. **Add the JSON tool schema** to `core/toolbox.py`
3. **Register the function** in `server/tool_server.py` → `TOOL_MAP`
4. **Add to tool router** (optional) — Add keywords to `server/tool_router.py` for smart filtering
5. **Restart the tool server** — `Ctrl+C` then `python main.py` again
6. The C++ daemon picks up new tools automatically from `/tools` on its next request

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| LLM (text) | MLX + `Qwen2.5-3B-Instruct-4bit` (native Apple Silicon) |
| LLM (vision) | MLX + `Qwen2.5-VL-3B-Instruct-4bit` (optional, port 8082, auto-discovered endpoint) |
| STT | whisper.cpp (tiny + base, Metal GPU) |
| TTS | Kokoro-82M via sherpa-onnx (native arm64 neural) + macOS `say` fallback |
| Audio | PortAudio 16 kHz ring buffer (C++) |
| Vision | macOS Accessibility API + VLM screenshot analysis |
| Browser | Brave Browser via JS injection through AppleScript (no Selenium/WebDriver) |
| Memory | ChromaDB (persistent at `~/.demascas/memory/`) |
| Web Memory | Per-domain JSON (persistent at `~/.demascas/web_memory.json`) |
| Learning | Episodic memory + user profile + knowledge base (persistent at `~/.demascas/`) |
| Web | Google + DuckDuckGo (ddgs) + BeautifulSoup4 |
| IPC | Flask micro-service on `localhost:5001` |
| Tool Routing | Query-aware filtering (16 categories, 110+ intent keywords) |
| MCP | Pure-Python JSON-RPC 2.0 client (zero deps) |
| Build | CMake + FetchContent (whisper.cpp, nlohmann/json) |
| Hardware | Apple Silicon (M1/M2/M3/M4), unified memory |

---

## License

Private project — all rights reserved.
