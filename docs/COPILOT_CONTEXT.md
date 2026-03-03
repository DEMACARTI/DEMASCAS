# DEMASCAS System Architecture & Copilot Guidelines

## Project Identity
**Name:** DEMASCAS (Decision Engineering Machine Algorithm for System Control and Action Sequencing).
**Goal:** To build a fully local, privacy-first, autonomous OS-level AI agent. It acts as a system daemon that can see the screen via OS accessibility trees, hear commands via local speech-to-text, and execute actions via native function calling and system scripts.

## Tech Stack
* **LLM Engine:** Ollama running `qwen2.5-coder` (optimized for local Apple Silicon execution).
* **Language:** Python 3.10+
* **Speech-to-Text:** OpenAI `whisper` (base model running locally) + `SpeechRecognition`.
* **OS Automation:** macOS native AppleScript invoked via Python `subprocess` (Avoid `pyautogui` to prevent mouse hijacking).
* **Data Parsing:** `PyPDF2`, `json`.

## Core Design Philosophy (CRITICAL INSTRUCTIONS FOR AI)
1.  **Zero-Latency Vision:** We DO NOT use screenshots or vision models (LLaVA) for standard UI interaction. We use the macOS Accessibility API to extract the UI Tree as structured text.
2.  **Native Function Calling:** The LLM does not use hardcoded `if/else` logic. It uses Ollama's native `tools` API. All new capabilities must be written as isolated Python functions and registered in the `tools` schema array.
3.  **Strict UI Extraction:** When the LLM decides to click or type, it MUST extract the exact element name from the UI tree. No hallucinated or descriptive names.
4.  **Local First:** No data leaves the machine. Do not suggest cloud APIs (like OpenAI API or Google Cloud) unless explicitly requested.

## Module Responsibilities

### `sensors/vision.py`
Extracting the active window's UI tree safely (avoiding `-1700` AppleScript crashes by using 3-level deep scans). Returns a structured text map of all named UI elements (buttons, text fields, groups, etc.) in the frontmost application window.

### `sensors/audio.py`
Handles all microphone input: wake-word detection loop (`wait_for_wake_word`) and full command transcription (`listen_and_transcribe`). Uses Whisper `base` model loaded once at module level for performance.

### `core/agent.py`
Managing the conversation history, appending tool_calls, triggering the mapped Python functions, and synthesizing final text responses. Routes LLM decisions to the correct actuator functions.

### `core/toolbox.py`
Contains JSON tool schemas for Ollama's native `tools` API. Each tool definition maps to a Python function in the actuators package. Add new capabilities here.

### `actuators/system_control.py`
Executing recursive AppleScript searches to click buttons or inject keystrokes in the background. Contains `click_ui_element` and `type_text_into_element`.

### `actuators/file_system.py`
Local RAG capabilities: PDF reading (`summarize_latest_pdf`), file searching, and the watchdog-based file observer for automated document processing.

### `actuators/network.py`
Future web scraping, API calls, and network-based tool integrations (e.g., smart home control).

## File Structure
```
DEMASCAS/
├── main.py                  # Entry point: wake-word infinite loop
├── .env                     # Hidden environment variables (API keys, paths)
├── requirements.txt         # Python dependencies
├── README.md                # Project documentation
│
├── core/                    # The "Brain"
│   ├── __init__.py
│   ├── agent.py             # Ollama API calls and tool routing
│   └── toolbox.py           # JSON schemas defining available tools
│
├── sensors/                 # The "Eyes and Ears" (Inputs)
│   ├── __init__.py
│   ├── vision.py            # macOS Accessibility Tree extraction
│   └── audio.py             # Whisper STT and wake-word detection
│
├── actuators/               # The "Hands" (Outputs and Actions)
│   ├── __init__.py
│   ├── system_control.py    # AppleScript for clicking and typing
│   ├── file_system.py       # Local RAG: PDF reading, file searching
│   └── network.py           # Future web scraping or API calls
│
└── docs/
    └── COPILOT_CONTEXT.md   # This file
```

## Adding a New Tool (Workflow)
1. Write the Python function in the appropriate `actuators/` module.
2. Add its JSON tool schema to `core/toolbox.py`.
3. Register the function mapping in `core/agent.py` → `TOOL_MAP`.
4. DEMASCAS will automatically inherit the new capability.
