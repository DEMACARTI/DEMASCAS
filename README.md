# DEMASCAS

> **Decision Engineering Machine Algorithm for System Control and Action Sequencing**

A fully local, privacy-first, autonomous OS-level AI agent for macOS — like JARVIS for your Mac. DEMASCAS can **see** the screen via VLM-powered vision + accessibility trees, **hear** commands via local speech-to-text, **browse the web** autonomously via Brave Browser JS injection, and **execute** actions via native AppleScript function calling. All processing runs 100 % on-device — no data ever leaves your machine.

**Architecture:** Polyglot C++ / Python — bare-metal C++ daemon for the hot path (audio → STT → LLM → TTS) with a lightweight Python tool server for OS actions, browser automation, memory, and learning.

**Bring your own model:** DEMASCAS ships as a tool/harness **without an LLM**. It profiles your laptop, detects the local runtime you already use (Ollama, LM Studio, llama.cpp, MLX, Jan or any OpenAI-compatible server), recommends a model that fits your RAM/GPU so the machine never lags or overheats, benchmarks it, and plugs it in — `python main.py setup`.

> **Platform status:** the plug-in LLM layer works on macOS, Windows and Linux. The OS-control layer (clicking, typing, apps, browser automation) is **macOS-only today**; Windows/Linux backends are the next phase — see [docs/HARNESS_ROADMAP.md](docs/HARNESS_ROADMAP.md).

---

## Latest Update — Plug-in Local LLM (Phase 0 of the harness)

DEMASCAS no longer ships or assumes a model:

- **`python main.py setup`** — profiles the laptop (CPU, RAM, Apple/NVIDIA GPU, battery), detects running runtimes in ~10 ms, recommends a model that fits a no-lag memory budget (installed models first), measures its real speed (time-to-first-token, tokens/s) and saves the choice to `~/.demascas/config.json`.
- **`python main.py doctor [--bench]`** — status of hardware, model fit, runtime and speed.
- **Any OpenAI-compatible runtime** — the daemon reads the endpoint/model/context window from the tool server at startup instead of compile-time MLX constants.
- **Resource governor** — keeps the model loaded while you use DEMASCAS (Ollama otherwise unloads after 5 idle minutes → slow next command), skips preloading when free RAM is short, and sizes whisper/TTS threads to the CPU.
- **Context-safe prompts** — the per-command tool list is 65% smaller (2.6k → 0.9k chars) and long tasks are trimmed to the runtime's context window while keeping a record of earlier steps.

---

## Stability & Security Pass

A full audit fixed broken features, unsafe defaults and documentation drift:

- **Security** — `run_shell_command` no longer uses a shell (allowlist + per-command argument checks, simple pipes only); the tool server rejects browser-originated requests (Origin / Host checks) and `/agent` requires real JSON; Docker binds to `127.0.0.1` only; values injected into AppleScript / JavaScript are properly escaped.
- **Deletes need your voice approval** — `delete_file` never runs straight from the LLM: the server holds the action behind a one-time token, the daemon asks "Move X to the Trash?", and only a clear *yes* runs it ("not sure", "don't", silence → cancelled). Files always go to the Trash — never a permanent delete.
- **Daemon** — shortcuts fire only when the whole utterance is the shortcut (no more ⌘⇧Q from "check the logs about the outage"); dismiss/wake words need the whole utterance; the file-choice ("which one?") and confirmation states are wired up; failures are reported instead of "Done."; barge-in no longer plays the next sentence over you.
- **Agents** — email drafts now include subject + body; PDF summaries route to the File agent and use the MLX server (Ollama no longer required); agents stop before the daemon's timeout and run one at a time.
- **Docker** — the image starts again (the `memory/` package was being excluded) and reaches the host's MLX servers via `host.docker.internal`.

---

## Session 7 — Autonomous Web Agent

DEMASCAS gained a full autonomous web agent with Brave Browser automation via JS injection, per-domain learning memory, obstacle detection (CAPTCHAs, paywalls, cookie banners), and intelligent web research — all without any new pip dependencies. Tool count at the time: **88 tools** (up from 43; 97 today).

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
  - [Manual Start (Separate Terminals)](#manual-start-separate-terminals)
- [Usage Guide](#usage-guide)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [97 Built-in Tools](#97-built-in-tools)
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

- 🎤 **Voice loop with barge-in** — Interrupt DEMASCAS while it is speaking, streaming TTS, energy-based VAD
- 🧠 **Bring-your-own local LLM** — any OpenAI-compatible runtime (Ollama, LM Studio, llama.cpp, MLX, Jan); `python main.py setup` picks a model that fits your laptop
- 👁️ **Vision-language model** — MLX `Qwen2.5-VL-3B-Instruct-4bit` (~1.6 GB) on port 8082; screenshot-based screen understanding
- 🗣️ **Neural TTS** — Kokoro 82M via sherpa-onnx (native arm64); sentence-queued streaming to PortAudio
- 🖥️ **Screen awareness** — Dual mode: macOS accessibility trees + VLM screenshot analysis
- 🛠️ **97 OS-level tools** — Click, type, wait, clipboard, right-click, switch tabs, open apps/URLs, browse the web, create/delete files, vision queries, compose/read email, smart reply, web research, contact extraction, full browser automation, autonomous web agent
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
- ✅ **Confirmation flow** — File deletes (and any action the model wraps in `confirm_and_execute`) wait for a spoken *yes*; enforced server-side with one-time tokens
- 🛡️ **Locked-down tool server** — Localhost-only, rejects browser requests, read-only shell allowlist (no shell interpreter)
- 🌐 **Web browsing** — Google + DuckDuckGo dual search + webpage reading + weather + news
- 🔌 **MCP support** — Extensible via Model Context Protocol servers (SQLite, GitHub, etc.)
- 🔒 **100 % local** — Zero cloud dependencies, total privacy (models are downloaded once on first run)
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
| **Brave Browser** (for web tools) | [brave.com](https://brave.com) — then enable **View → Developer → Allow JavaScript from Apple Events** (all `browser_*` / web-agent tools need it) |

> **Note:** Ollama is **not** required. PDF summaries use the MLX text server; if the optional `ollama` package is installed it is only used as a fallback when MLX is unreachable.

### macOS Permissions Required

Go to **System Settings → Privacy & Security** and grant:

| Permission | Why |
|---|---|
| **Accessibility** → Terminal / iTerm | UI automation (click, type, read screen) |
| **Microphone** → Terminal / iTerm | Voice capture |
| **Screen Recording** → Terminal / iTerm | Screenshots for VLM vision |
| **Automation → Finder** (and Brave Browser) → Terminal / iTerm | Moving files to the Trash; driving Brave. If Finder automation is denied, deletes fail safely (nothing is removed). |

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

> `requirements.txt` includes `pyautogui` (macOS only) for the vision fallback's coordinate clicks/typing.

### 3. Plug in a local LLM

DEMASCAS doesn't include a model. Run the guided setup — it checks your hardware, finds any runtime you already have, recommends a model that fits, and measures its speed:

```bash
python main.py setup            # interactive
python main.py setup --yes      # accept the recommended installed model
python main.py setup --url http://127.0.0.1:11434 --model qwen2.5:3b   # explicit
python main.py doctor --bench   # check it any time
```

Typical recommendations (balanced mode, latency-first):

| Laptop | Model budget | Suggested |
|---|---|---|
| 8 GB Apple Silicon | 3.2 GB | Qwen2.5 3B (MLX or Ollama) |
| 16–18 GB Apple Silicon | 6–7 GB | Qwen2.5 7B |
| NVIDIA 8 GB VRAM | 7.2 GB | Qwen2.5 7B (Ollama) |
| CPU-only (Intel Mac, most Windows/Linux laptops) | capped at ~3B | Qwen2.5 3B |
| 4 GB machine | 1.6 GB | Qwen2.5 1.5B |

No runtime yet? The setup prints exact install steps — e.g. Ollama (`brew install ollama` / `winget install Ollama.Ollama` / `curl -fsSL https://ollama.com/install.sh | sh`, then `ollama pull qwen2.5:3b`) or, on Apple Silicon, MLX (`pip install mlx-lm`; `scripts/run.sh` then starts the MLX server for you).

> **Context window:** DEMASCAS's tool prompt needs ≥ 4096 tokens. Ollama's default is 4096; 8192 is better for long tasks (`OLLAMA_CONTEXT_LENGTH=8192`). `setup`/`doctor` warn if yours is smaller.

### 4. Build the C++ daemon

The build script handles everything — dependencies, model downloads, and compilation:

```bash
chmod +x scripts/build.sh scripts/run.sh
./scripts/build.sh
```

This will:
1. Check / install CMake, PortAudio, pkg-config via Homebrew
2. Download whisper GGML models (~75 MB tiny + ~147 MB base) to `daemon/models/`
3. Download sherpa-onnx pre-built library (universal2 arm64+x86\_64)
4. Download Kokoro-en-v0\_19 neural TTS model (~335 MB) to `daemon/models/`
5. Install Flask for the Python tool server
6. Compile the C++ daemon via CMake (Release, ARM64)

On success you'll see:

```
✅  Build complete!
Binary:  daemon/build/demascas_daemon
```

---

## Docker

Docker is supported for the **Python tool server only**, and only for the tools that don't need macOS: memory, learning, web search/reading, weather/news, file listing/creation, MCP. Everything that drives the Mac — AppleScript UI control, Brave automation, screenshots/vision, opening/trashing files — returns a clear "needs macOS" error inside the container. The C++ voice daemon and the MLX servers always run on the Mac host.

For normal use, run the tool server natively (`python main.py` or `./scripts/run.sh`). Docker is mainly useful for developing the non-macOS parts.

### Run the tool server in Docker

```bash
docker compose up --build tool-server
```

- The port is published on **`127.0.0.1:5001` only** — the server has no authentication, so it must never be reachable from your network.
- The container reaches your LLM runtime on the host through `host.docker.internal`. Tell it which model to use: `DEMASCAS_LLM_MODEL=qwen2.5:3b docker compose up --build tool-server` (override `DEMASCAS_LLM_URL` for non-Ollama runtimes). On Linux the runtime must listen on a non-loopback address (e.g. `OLLAMA_HOST=0.0.0.0`).
- Persistent data lives in the `demascas-home` volume at `/home/demascas/.demascas`.
- `./scripts/run.sh` detects a tool server already listening on :5001 and reuses it.

### Configuration via environment

| Variable | Default | Purpose |
|---|---|---|
| `DEMASCAS_HOST` / `DEMASCAS_PORT` | `127.0.0.1` / `5001` | Tool server bind address |
| `DEMASCAS_LLM_URL` + `DEMASCAS_LLM_MODEL` | *(from `setup`)* | Override the plugged-in LLM (base or chat URL + model id); `DEMASCAS_LLM_CONTEXT` sets its context window |
| `DEMASCAS_MLX_VL_BASE` | `http://127.0.0.1:8082` | MLX vision server base URL |
| `DEMASCAS_TOOL_SERVER_URL` | `http://127.0.0.1:$DEMASCAS_PORT` | How agents call back into the tool server |
| `DEMASCAS_ALLOWED_HOSTS` | *(empty)* | Extra `Host` header names the server accepts (comma-separated) |
| `DEMASCAS_SKIP_MCP_BOOT` | *(unset)* | `1` disables MCP server startup |
| `DEMASCAS_MEMORY_MAX_DISTANCE` | `0.75` | How similar a memory must be to be recalled (lower = stricter) |

---

## Running DEMASCAS

DEMASCAS runs as **three processes** — an MLX text server, a Python tool server, and a C++ daemon — plus an **optional** MLX vision server you start manually (it is off by default to save RAM on 8 GB Macs).

### Quick Start (One Command)

```bash
# Activate the venv
source ../venv/bin/activate

# Check/start your LLM + tool server + daemon
./scripts/run.sh
```

The run script starts:
1. **Your plugged-in LLM** — checked for reachability; if you chose MLX it is started automatically (waits up to `MLX_STARTUP_TIMEOUT`=300 s on the first run while the model downloads). Ollama / LM Studio / llama.cpp run as their own apps/services. With no model configured, DEMASCAS still runs shortcuts and non-LLM actions and tells you to run setup.
2. **Python tool server** on `localhost:5001` — 97 tools + MCP + learning (reused if already running)
3. **C++ daemon** in foreground — audio → whisper.cpp → your LLM → Kokoro TTS

The **vision server** (Qwen2.5-VL-3B, port 8082) is not started automatically; run `python scripts/start_vlm_server.py` in another terminal if you want screenshot-based vision.

```
[*] Checking your local LLM...
[✓] LLM runtime reachable.
[*] VLM server disabled (lazy-load mode for 8GB RAM). Start manually if needed.
[*] Starting Python tool server (localhost:5001)...
[*] Starting C++ daemon...

╔══════════════════════════════════════════╗
║   DEMASCAS — Bare-Metal C++ Daemon       ║
║   Plug-in local LLM • Kokoro TTS         ║
╚══════════════════════════════════════════╝

[✓] Microphone opened (16 kHz mono, PortAudio)
[✓] Whisper models loaded

[*] DEMASCAS is ready.  Say "demascas" to activate!
```

**Stop:** Press `Ctrl+C` — the script stops every process it started (the tool server flushes learning data on shutdown).

---

### Manual Start (Separate Terminals)

If you prefer to run the processes separately (useful for debugging):

**Terminal 1 — your LLM runtime** (whatever you plugged in), e.g.:

```bash
ollama serve                         # Ollama (usually already running as an app/service)
python -m mlx_lm server --model mlx-community/Qwen2.5-3B-Instruct-4bit --port 8081   # MLX
```

**Terminal 2 — MLX vision server (optional, off by default):**

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
3. **Interrupt while it speaks** — Talk over DEMASCAS while it is speaking; it stops and listens (it can't be interrupted while it is still thinking or running a tool)
4. **Approve risky actions** — For deletes it asks e.g. *"Move report.pdf to the Trash?"*; only a clear *yes* goes ahead
5. **Pick from several matches** — If a search finds several files it lists them; answer *"the second one"* or part of the name
6. **Multi-turn conversation** — Keep talking; it stays awake for 15 seconds between turns
7. **It goes back to sleep** — After 15 seconds of silence (or "that's all", "goodbye", "stop"), it says "Standing by." and sleeps

### Example Commands

| What you say | What happens |
|---|---|
| "Open Safari" | System agent launches Safari via AppleScript |
| "Close this window" | Sends ⌘W to the active app (shortcut path) |
| "Take a screenshot" | Sends ⌘⇧3 (shortcut path) |
| "Search the web for best restaurants in Delhi" | Browser agent searches and reads results aloud |
| "What's on my screen right now?" | Tool path reads the accessibility tree (or VLM if running) |
| "Remember that my meeting is at 3 PM tomorrow" | Saves to persistent ChromaDB memory |
| "What did I tell you about my meeting?" | Retrieves from memory |
| "Open Instagram in Safari" | Browser agent opens instagram.com |
| "Open Gmail" | Opens Gmail in Brave |
| "Google best restaurants in Delhi" | Browser agent runs a Google search |
| "Search YouTube for lo-fi beats" | Opens YouTube in Brave and searches (web agent) |
| "Play the first video" | Clicks the first YouTube result (web agent) |
| "Write an email to aman@example.com about the Q3 deadline" | Drafts subject + body with the LLM, opens Gmail, fills To/Subject/Body — never sends (comms agent). If you name someone without an address it asks for it (or uses the people graph). |
| "What's on this web page?" | Analyzes the current page via DOM summary (browser) |
| "Click the Sign In button" | JS injection click on web element (browser) |
| "Fill in the search box with machine learning" | Types into web form field (browser) |
| "Search Amazon for wireless headphones" | Navigates to Amazon, searches product (web agent) |
| "Research quantum computing" | Multi-source web research: search → read → synthesize (web agent) |
| "Handle the cookie popup" | Auto-dismisses cookie consent banner (web agent) |
| "Summarize my latest PDF" / "Summarize the SIH PDF" | File agent summarizes the newest PDF in ~/Downloads, or the named one |
| "Delete the old notes file" | Finds it, then asks "Move notes.txt to the Trash?" — needs a spoken yes |
| "Right-click that button" | Uses `right_click_element` tool |
| "Copy what's on the clipboard" | Uses `get_clipboard` tool |
| "Wait for the download to finish" | Uses `wait_for_element` tool |
| "Undo that" | Sends ⌘Z instantly (shortcut path, no LLM) |
| "Minimize" | Sends ⌘M instantly |
| "What time is it?" | Answers directly (fast path, no tools) |
| "My name is Daksh" | Saved to user profile — all future responses personalized |
| "Greet my friend Saf" | Speaks directly: "Hey Saf! Great to meet you!" (fast Q&A) |
| "Tell me a joke" | Tells a joke directly (fast Q&A, no tools) |

### Processing Paths

Every command is routed by the C++ daemon in this order:

| # | Path | Latency | When |
|---|------|---------|------|
| 0 | **Continuation** | <1 ms | You are answering "which one?" after a multi-match file search |
| 1 | **Shortcut** | <1 µs | The whole utterance is a keyboard shortcut ("close this window", "undo that"). Questions never fire shortcuts. |
| 2 | **Domain agent** | ~1–80 s | Email, browsing, files, research, workflows, app learning/building — `POST /agent` to the Python agents (falls back to path 4 if agents are unavailable) |
| 3 | **Fast Q&A** | ~300–800 ms | Conversation and general-knowledge questions (no tools). If the model replies "Let me handle that for you", the command is re-run on path 4. |
| 4 | **Tool ReAct loop** | ~1–10 s | Everything else: the LLM picks tools one at a time (3–8 relevant tools per query) |

Paths 2 and 4 can hand off to two follow-up states: **CONFIRMING** (a destructive action is waiting for a spoken yes/no) and **CLARIFYING** (several files matched — pick one).

> The planner (`plan_steps*`), deep-intent (`deep_intent_streaming`) and structured-task-executor (`/plan_and_execute`) code still exists but is **not wired into the daemon's routing**.

---

## Architecture

### Plug-in LLM Layer (`core/llm/`)

DEMASCAS talks to **any OpenAI-compatible local runtime** — no model is bundled, no cloud API is used.

| Module | Role |
|---|---|
| `hardware.py` | CPU / RAM / Apple-or-NVIDIA GPU / battery profile (macOS, Windows, Linux; psutil or OS-native) |
| `runtimes.py` | Parallel detection of Ollama (11434), LM Studio (1234), llama.cpp (9931/8080), MLX (8081/8080), Jan (1337), custom URLs; model lists, loaded state, context size |
| `catalog.py` | Curated models with per-runtime IDs and memory needs (data, not a dependency) |
| `recommend.py` | No-lag memory budget + latency cap (≤8B by default, ~3B on CPU-only) — installed models first |
| `benchmark.py` | Real time-to-first-token (cold/warm) and tokens/s on a DEMASCAS-shaped prompt → fast / ok / slow |
| `governor.py` | Keeps the model loaded during use, skips preload when RAM is short |
| `settings.py` / `cli.py` | `~/.demascas/config.json` → `"llm"`; `setup` / `doctor` / `detect` |

The tool server exposes the choice at `GET /llm`; the C++ daemon reads it once at startup (endpoint, model, context window, whisper/TTS thread counts). Vision is optional and configured the same way.

### Processing Pipeline

```
User speaks → PortAudio → whisper.cpp (Metal GPU)
                                │
                         ┌──────┴──────┐
                         │   Router     │  (C++)
                         └──────┬──────┘
          ┌───────────┬─────────┼──────────┬───────────┐
          ▼           ▼         ▼          ▼           ▼
     Continuation  Shortcut   Agent     Fast Q&A    Tool ReAct
     (file choice) (<1 µs)   (/agent)   (no tools)  (3-8 tools)
          │           │         │          │           │
          └───────────┴─────────┼──────────┴───────────┘
                                │
                   ┌────────────┴────────────┐
                   │ CONFIRMING / CLARIFYING │  (yes/no · "which one?")
                   └────────────┬────────────┘
                                │
                        ┌───────┴───────┐
                        │   Kokoro TTS  │  (sentence-by-sentence)
                        │  24kHz neural │
                        └───────────────┘
```

### System Diagram

```
┌────────────────────────────────────────────────────────────────┐
│        YOUR LOCAL LLM RUNTIME (plugged in, not bundled)         │
│                                                                │
│  Ollama · LM Studio · llama.cpp server · MLX · Jan · …         │
│  text model sized to this laptop (+ optional vision model)     │
│  OpenAI-compatible /v1/chat/completions                       │
└───────────────────────────┬────────────────────────────────────┘
                            │ HTTP streaming
┌───────────────────────────┼────────────────────────────────────┐
│             C++ DAEMON (bare metal, hot path)                  │
│                                                                │
│  PortAudio ──→ VAD ──→ whisper.cpp ──→ Router ──────────────┐ │
│  ring buffer   energy   tiny+base      continuation /       │ │
│  16 kHz mono   ~32ms    Metal GPU      shortcut / agent /   │ │
│                                        fast / tool           │ │
│                                                              │ │
│  ┌── TTS (streaming, sentence queue) ←───────────────────────┘ │
│  │   Kokoro 82M (sherpa-onnx) │ float32 24kHz │ barge-in kill │
│  │   macOS `say` fallback     │ PID tracking                  │
│  └───────────────────┘                                        │
│       │                                                       │
│  CONFIRMING: asks "Move X to the Trash?" → listens 8 s →      │
│    clear yes → POST /confirm {token} ; anything else → cancel │
│       │                                                       │
│       │ HTTP localhost:5001                                   │
└───────┼───────────────────────────────────────────────────────┘
        ▼
┌───────────────────────────────────────────────────────────────┐
│          PYTHON TOOL SERVER (Flask, 97 tools)                  │
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
│  /agent           → domain agents (email, files, web, ...)     │
│  /confirm         → approve/deny a pending destructive action  │
│  /mcp/status      → MCP server monitoring                      │
│                                                                │
│  ┌──────────────┐  ┌──────────┐  ┌─────────────────────┐      │
│  │ Native Tools │  │ MCP      │  │ Learning Engine     │      │
│  │ (97 tools:   │  │ Bridge   │  │ (episodes, profile, │      │
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
    │  session.json        → last turns (0600)     │
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
│   ├── config.py                #   Service endpoints (env-overridable: MLX, tool server)
│   ├── toolbox.py               #   97 tool schemas (OpenAI-compatible)
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
│       ├── llm_client.hpp/cpp   #   Streaming MLX (rules, ReAct loop, confirmations, session)
│       ├── tool_bridge.hpp/cpp  #   HTTP bridge to Python tool server (+ screen context + deep intent)
│       ├── tts.hpp/cpp          #   Kokoro neural TTS (sherpa-onnx) + macOS `say` fallback
│       └── shortcut_map.hpp     #   72 shortcuts + macros (header-only)
│
├── server/                      # Python Tool Micro-service
│   ├── tool_server.py           #   Flask on localhost:5001: tools, agents, confirmations, learning
│   ├── tool_router.py           #   Smart query-aware tool filtering (16 categories)
│   └── agents/                  #   Domain agents: browser, file, system, research, comms,
│                                #   workflow, app learner, app builder (+ keyword dispatcher)
│
├── scripts/                     # Build & Run helpers
│   ├── build.sh                 #   One-command build (deps + models + compile)
│   ├── run.sh                   #   One-command run (MLX text + tool server + daemon)
│   └── start_vlm_server.py      #   MLX VLM server launcher (auto-discovers endpoint)
│
└── docs/
    ├── CLAUDE_CONTEXT.md        #   Detailed project context for AI assistants
    └── COPILOT_CONTEXT.md       #   AI coding assistant context (legacy)
```

---

## 97 Built-in Tools

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
| 69 | `create_file` | file_system | Create a new file (never overwrites) |
| 70 | `delete_file` | file_system | Move a file to Trash — always asks for a spoken yes first (server-enforced) |
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
| 84 | `confirm_and_execute` | tool_server | Queue any action for a spoken yes/no (CONFIRMING state) |
| 85 | `extract_contact` | communication | VLM extracts contact info → saves to people graph |
| 86 | `web_research_and_report` | communication | 3-phase: search → read → synthesize report |
| 87 | `gmail_read_email` | browser | Read email content via Brave JS injection |
| 88 | `open_url_and_wait` | system_control | Open URL and wait for page load |

### System, Preferences & Planning (9 tools)

| # | Tool | Module | Description |
|---|------|--------|-------------|
| 89 | `run_shell_command` | system_commands | Read-only system info commands (allowlisted, no shell; simple pipes) |
| 90 | `get_system_info` | system_commands | macOS version, uptime, user, disk space |
| 91 | `set_default_browser` | system_commands | Set the default browser (needs `brew install defaultbrowser`) |
| 92 | `set_preference` | preferences | Store a user rule/preference |
| 93 | `remove_preference` | preferences | Remove a stored preference |
| 94 | `set_humor_level` | preferences | Adjust the assistant's humor |
| 95 | `save_user_preference` | learning | Save a categorized preference to the profile |
| 96 | `architect_plan` | orchestrator | Generate a step-by-step plan for a complex goal |
| 97 | `summarize_selected_pdf` | file_system | Summarize the PDF selected in Finder (or a given path) |

---

## 72 macOS Shortcuts

Shortcuts are matched in C++ and executed **instantly** without touching the LLM. Examples:

| Voice Command | Shortcut Fired |
|---|---|
| "Close this window" | ⌘W |
| "Close the tab" | ⌘W |
| "Quit this app" | ⌘Q |
| "Take a screenshot" | ⌘⇧3 |
| "Screenshot region" | ⌘⇧4 |
| "Screenshot window" | ⌘⇧4, then Space |
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
| "Show desktop" | ⌘F3 |

Shortcuts are organized into 5 categories: Window/App (21), Editing (14), File (5), System (23), Browser (9).

### How shortcut matching works

A shortcut fires only when the **whole utterance** is the shortcut phrase plus filler words ("please", "the", "this", "window", "now", …):

- ✅ "close this window", "please undo that", "take a screenshot", "refresh the page"
- ❌ "check the logs about the outage" (does **not** log you out), "how much disk space is left" (does **not** switch Spaces), "show me trash" (does **not** trash the selected file)
- ❌ Questions never fire shortcuts: "how do I lock my screen?" is answered, not executed.

Commands that used to be C++ macros ("Open Gmail", "Google X", "Open Downloads folder") are now handled by the domain agents.

---

## Smart Tool Routing

The `server/tool_router.py` module provides intelligent query-aware tool filtering. Instead of sending all 97 tools to the LLM (wasting context window), it classifies each query and returns only **3–8 relevant tools**.

**16 tool categories** with 110+ intent keywords:
- UI interaction, app management, web browsing, browser automation, web agent, Gmail, YouTube, e-commerce, file management, screen reading, vision, keyboard, memory, clipboard, typing, realtime/weather

The C++ daemon calls `POST /tools_for_query` before each tool-calling LLM invocation.

**Tool alias normalisation** runs server-side (real tool names always win over aliases): if the LLM hallucinates a tool name (e.g. `click` instead of `click_ui_element`, `navigate` instead of `browser_navigate`), the server remaps it transparently via a 45+ entry alias table — no round-trip wasted. Argument aliases are also normalized (e.g. `element` → `selector`, `website` → `url`).

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
| **Knowledge Base** | Procedures, facts (e.g. single-match file locations), corrections — web search results are **not** stored | ChromaDB `demascas_knowledge` | Unbounded |
| **Web Memory** | Per-domain selectors, flows, obstacles, site types | `~/.demascas/web_memory.json` | Per-domain |
| **Context Builder** | Injects top-3 similar episodes + profile + people + knowledge into LLM prompt | Transient (in-memory) | ~200 tokens |

**Zero overhead:** Reuses the same ChromaDB client as RAG memory. Profile writes are batched (1 flush per 5 commands) and flushed on shutdown (Ctrl+C / SIGTERM). Only memories that are actually related to the command are injected into prompts. No extra LLM calls, no background threads.

The C++ daemon calls `GET /context?query=...` before each LLM invocation and `POST /learn` after each command completes.

---

## Configuration & Tuning

All daemon tuning knobs live in a single header: `daemon/src/config.hpp`

### LLM Configuration

The model, endpoint and context window are **not** compile-time settings — they come from `python main.py setup` (`~/.demascas/config.json` → `"llm"`) via `GET /llm`. The daemon knobs that remain:

| Knob | Default | Description |
|------|---------|-------------|
| `TOOL_PREDICT` | `200` | Max tokens for tool-calling responses |
| `FAST_PREDICT` | `80` | Max tokens for simple Q&A |
| `AGENT_TIMEOUT_SEC` | `100` | Max wait for `/agent` (agents stop themselves after 80 s) |
| `LLM_TEMPERATURE` | `0.1f` | Low temp = deterministic tool calls |

### Audio & Voice

| Knob | Default | Description |
|------|---------|-------------|
| `SAMPLE_RATE` | `16000` | Audio sample rate (Hz) |
| `FRAMES_PER_BUFFER` | `512` | Samples per audio buffer |
| `VAD_ENERGY_THRESHOLD` | `0.008f` | Voice activity detection sensitivity |
| `BARGE_IN_THRESHOLD` | `0.18f` | Speech-over-TTS detection (avoids self-listening) |
| `SILENCE_DURATION` | `1.0f` | Seconds of silence before end-of-utterance (added to every command — raise to 1.2–1.5 if you get cut off mid-sentence) |
| `CAPTURE_POLL_MS` | `50` | Mic polling granularity |
| `WAKE_LISTEN_WINDOW` | `2.0f` | Seconds of audio in the sliding wake-word window |
| `WAKE_HOP_SEC` | `0.75f` | How often the wake window is re-checked (only while there is sound) |
| `POST_TTS_GUARD_MS` | `250` | Mic ignored this long after DEMASCAS stops talking (echo tail) |
| `WHISPER_MIN_SEC` | `1.5f` | Short clips ("yes", "stop") are padded to this — whisper.cpp ignores clips under ~1 s |
| `WHISPER_ADAPTIVE_CTX` | `true` | Size the command model's encoder window to the clip (base: ~220 → ~90 ms) |
| `COMMAND_TIMEOUT` | `10.0f` | Max seconds to wait for first command |
| `FOLLOWUP_TIMEOUT` | `15.0f` | Max seconds between follow-ups |
| `TTS_COOLDOWN_SEC` | `1.0f` | Seconds to ignore mic after TTS finishes (anti-echo) |
| `INPUT_DEVICE_INDEX` | `-1` | Audio input device (-1 = system default) |
| `WAKE_WORD` | `"demascas"` | Wake word shown in the banner (detection uses the variants in `main.cpp`) |

### TTS Configuration

| Knob | Default | Description |
|------|---------|-------------|
| `USE_KOKORO` | `true` | Enable Kokoro neural TTS (set `false` for macOS `say`) |
| `KOKORO_MODEL_DIR` | `"models/kokoro-en-v0_19"` | Path to Kokoro model directory |
| `KOKORO_SPEAKER_ID` | `0` | Speaker (0=af default female; 11 voices available) |
| `KOKORO_SPEED` | `1.0f` | Speech speed (< 1 faster, > 1 slower) |
| `KOKORO_SAMPLE_RATE` | `24000` | Kokoro output sample rate (Hz) |
| `KOKORO_THREADS` | `4` | Synthesis threads (~30% faster than 2 on M1) |
| `WAKE_ACK_PHRASE` | `"At your service."` | Spoken after the wake word; `""` = chime only (saves ~1.1 s per wake) |
| `TTS_CACHED_PHRASES` | *(list)* | Fixed phrases pre-synthesized at startup and played instantly |
| `TTS_VOICE` | `"Samantha"` | macOS `say` fallback voice |
| `TTS_RATE` | `190` | macOS `say` words per minute |

### Conversation & Whisper

| Knob | Default | Description |
|------|---------|-------------|
| `MAX_HISTORY` | `4` | Conversation turns kept in context |
| `MAX_TOOL_DEPTH` | `5` | Max ReAct loop iterations |
| `MAX_CONSECUTIVE_BLANKS` | `2` | Blank transcriptions before returning to sleep |
| `SESSION_MAX_TURNS` | `6` | Turns persisted to `~/.demascas/session.json` (0600) |
| `WHISPER_PROMPT` | *(vocabulary hint)* | Expected vocabulary to reduce hallucinations |

After changing config, rebuild with `./scripts/build.sh` (or `cmake --build daemon/build`).

---

## Performance & Optimization

### Latency Budget (per command)

Measured on an M1 / 8 GB (Python + C++ components; LLM time depends on the model and prompt length):

| Phase | Time | Notes |
|---|---|---|
| End-of-speech detection | `SILENCE_DURATION` (1.0 s) | Biggest fixed cost — was 2.0 s |
| Wake-word check | ~140 ms per 0.75 s hop | whisper tiny, sliding 2 s window, skipped during silence |
| Command transcription | ~90 ms | whisper base, clip-sized encoder window (was ~220–260 ms) |
| Context + tools + memory lookups | ~5–10 ms | Persistent embedder + cache (was ~280 ms, ~1 s on the first command) |
| LLM first token | model-dependent | Static prompt prefix is reused from the MLX prompt cache; warmed at startup |
| TTS first audio | ~0 ms for fixed phrases; ~0.5–1.3 s for a new sentence | Pre-synthesized phrase cache; answers split into sentences; 4 threads |
| UI tool + screen read | +1–2 s only when the next step needs element names | Screen read skipped for browser/web/shortcut tools and for agents |
| Learning (`/learn`) | ~3 ms | Recorded on a background worker |

**Tuning for even lower latency:** set `WAKE_ACK_PHRASE = ""` (chime only), and keep the VLM server off unless you need screenshot vision.

### RAM Budget (8 GB Apple Silicon)

| Component | RAM |
|---|---|
| Phi-4 mini (MLX text server) | ~1.8 GB |
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
- **Smart tool filtering** — Only 3–8 relevant tools sent per query (vs. all 97), saving ~60% context
- **Single ChromaDB client** — memory.py, learning.py, and knowledge.py share one embedding model (~80 MB saved)
- **Streaming everything** — LLM tokens stream to TTS sentence queue; no waiting for full response
- **No cold starts** — embedding model, ChromaDB, whisper and the MLX prompt cache are warmed at startup
- **Cached embeddings** — one persistent ONNX session; a command is embedded once for memory, episodes and knowledge (~1.5 ms)
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
| **"I don't have a language model set up yet"** | Run `python main.py setup` (DEMASCAS ships without an LLM) |
| **LLM runtime not reachable** | Start your runtime (Ollama app, LM Studio server, llama-server, MLX); `python main.py doctor` shows what's configured |
| **Replies feel slow** | `python main.py doctor --bench`; if the verdict is SLOW, re-run `setup` and pick a smaller model |
| **MLX text server won't start** | Check `pip install mlx-lm` is installed; verify the port is free. The first run downloads the model — `run.sh` waits up to `MLX_STARTUP_TIMEOUT` (300 s). |
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
| **LLM says "you can say X" instead of speaking** | System prompt defines DEMASCAS as a character. If it persists, delete `~/.demascas/session.json` |
| **Browser tools fail with "JS error"** | In Brave enable **View → Developer → Allow JavaScript from Apple Events** |
| **"Could not move … to the Trash"** | Grant Terminal **Automation → Finder** in System Settings → Privacy & Security. DEMASCAS never falls back to a permanent delete. |
| **A tool says it "needs macOS"** | The tool server is running in Docker/Linux — run it natively for UI/browser/file tools |
| **"I'm still working on the previous task"** | Only one agent runs at a time; wait for it to finish (max ~80 s) |
| **Tool server errors in daemon** | Check tool server terminal logs; ensure `python main.py` is running |
| **Learning not working** | Check `~/.demascas/` exists; run `cat ~/.demascas/user_profile.json` |
| **PDF summarization fails** | It uses the MLX text server on :8081 — make sure it is running. (Ollama is only an optional fallback.) |
| **"Sorry, I couldn't reach my language model"** | The MLX text server on :8081 is down or still loading |

---

## Adding a New Tool

1. **Write the Python function** in the appropriate `actuators/` or `sensors/` module
2. **Add the JSON tool schema** to `core/toolbox.py`
3. **Register the function** in `server/tool_server.py` → `TOOL_MAP` (and add it to `CONFIRM_REQUIRED_TOOLS` if it is destructive)
4. **Add to tool router** (optional) — Add keywords to `server/tool_router.py` for smart filtering
5. **Restart the tool server** — `Ctrl+C` then `python main.py` again
6. The C++ daemon picks up new tools automatically from `/tools` on its next request

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| LLM (text) | Your choice — any OpenAI-compatible local runtime (Ollama, LM Studio, llama.cpp, MLX, Jan), sized by `python main.py setup` |
| LLM (vision) | Optional — any vision model on the same kind of runtime |
| STT | whisper.cpp (tiny + base, Metal GPU) |
| TTS | Kokoro-82M via sherpa-onnx (native arm64 neural) + macOS `say` fallback |
| Audio | PortAudio 16 kHz ring buffer (C++) |
| Vision | macOS Accessibility API + VLM screenshot analysis |
| Browser | Brave Browser via JS injection through AppleScript (no Selenium/WebDriver) |
| Memory | ChromaDB (persistent at `~/.demascas/memory/`, relevance-filtered recall) |
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
