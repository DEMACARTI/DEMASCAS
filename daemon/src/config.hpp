/**
 * DEMASCAS — daemon/src/config.hpp
 * Centralised configuration for the bare-metal C++ daemon.
 *
 * Every tuning knob lives here so the rest of the codebase is
 * free of magic numbers.  Values mirror the Python agent.py
 * configuration where applicable.
 *
 * Hardware target: Apple M1, 8 GB unified RAM.
 */

#pragma once
#include <string>

namespace demascas::config {

// ─── Whisper.cpp model paths (relative to daemon/ working dir) ──
inline const std::string WHISPER_TINY_MODEL = "models/ggml-tiny.bin";
inline const std::string WHISPER_BASE_MODEL = "models/ggml-base.bin";

// ─── LLM servers ─────────────────────────────────────────────────
// Unified cloud model: gemma4:31b-cloud (vision + text via Ollama)
// Both use OpenAI-compatible /v1/chat/completions endpoints.

#ifdef __linux__
// Linux (Ollama - Cloud model)
inline const std::string MLX_TEXT_URL     = "http://127.0.0.1:11434/v1/chat/completions";
inline const std::string MLX_VL_URL       = "http://127.0.0.1:11434/v1/chat/completions";  // unified with LLM
inline const std::string MLX_TEXT_MODEL   = "gemma4:31b-cloud";
inline const std::string MLX_VL_MODEL     = "gemma4:31b-cloud";
#else
// macOS (MLX)
inline const std::string MLX_TEXT_URL     = "http://127.0.0.1:8081/v1/chat/completions";
inline const std::string MLX_VL_URL       = "http://127.0.0.1:8082/v1/chat/completions";
inline const std::string MLX_TEXT_MODEL   = "mlx-community/Qwen2.5-3B-Instruct-4bit";
inline const std::string MLX_VL_MODEL     = "mlx-community/Qwen2.5-VL-3B-Instruct-4bit";
#endif

constexpr int            TOOL_CTX         = 2048;     // context window for tool-calling path
constexpr int            TOOL_PREDICT     = 200;      // max tokens for tool-calling responses
constexpr int            FAST_CTX         = 1024;     // context window for simple Q&A
constexpr int            FAST_PREDICT     = 80;       // max tokens for simple Q&A
constexpr float          LLM_TEMPERATURE  = 0.1f;     // low temp = deterministic tool calls

// ─── Python tool micro-service ─────────────────────────────────
inline const std::string TOOL_SERVER_BASE   = "http://127.0.0.1:5001";
inline const std::string TOOL_EXEC_URL      = "http://127.0.0.1:5001/tool";
inline const std::string TOOL_SCHEMA_URL    = "http://127.0.0.1:5001/tools";
inline const std::string TOOL_FILTER_URL    = "http://127.0.0.1:5001/tools_for_query";
inline const std::string TOOL_HEALTH_URL    = "http://127.0.0.1:5001/health";

// ─── Audio capture (PortAudio → whisper.cpp) ───────────────────
constexpr int   SAMPLE_RATE          = 16000;   // whisper.cpp requires 16 kHz mono
constexpr int   FRAMES_PER_BUFFER    = 512;
constexpr float VAD_ENERGY_THRESHOLD = 0.008f;  // RMS threshold for "speech" (calibrated: M1 built-in mic baseline ~0.003)
constexpr float BARGE_IN_THRESHOLD   = 0.18f;   // RMS threshold during TTS — must speak OVER the speaker (calibrated for laptop speakers)
constexpr float SILENCE_DURATION     = 2.0f;    // seconds of silence → end of utterance
constexpr float WAKE_LISTEN_WINDOW   = 2.0f;    // seconds per wake-word scan
constexpr float COMMAND_TIMEOUT      = 10.0f;   // max seconds to wait for first command
constexpr float FOLLOWUP_TIMEOUT     = 15.0f;   // max seconds between follow-ups

// ─── Screen stability ──────────────────────────────────────────
constexpr float SCREEN_STABLE_THRESHOLD = 0.025f;  // fraction of pixels changed to consider "unstable"

// ─── Tool execution limits ─────────────────────────────────────
constexpr int   TOOL_TIMEOUT_MS      = 8000;    // max ms for a single tool call
constexpr int   REACT_MAX_ERRORS     = 3;       // consecutive tool errors before hard stop

// ─── Hallucination / sleep-back ────────────────────────────────
constexpr int   MAX_CONSECUTIVE_BLANKS = 2;     // blanks before returning to SLEEPING
constexpr float TTS_COOLDOWN_SEC     = 1.0f;    // seconds to ignore mic after TTS finishes (anti-echo)

// ─── TTS fallback voice settings ────────────────────────────────
// macOS: uses `say -v <voice> -r <rate>`
// Linux: uses `espeak-ng -v <voice> -s <rate>`
#ifdef __linux__
// Linux default: en-us for espeak-ng, rate in wpm (100-500)
inline const std::string TTS_VOICE = "en-us";   // English (US)
constexpr int            TTS_RATE  = 160;       // words per minute (100-500 range)
#else
// macOS default: Samantha, rate in wpm (80-450)
inline const std::string TTS_VOICE = "Samantha";  // American female — clear + natural
constexpr int            TTS_RATE  = 190;          // words per minute
#endif

// ─── Kokoro TTS (via sherpa-onnx — native arm64 neural voice) ──
// Download with: ./scripts/build.sh
// 82 M-parameter StyleTTS 2 model, 24 kHz, 11 English voices.
// Runs natively on Apple Silicon (no Rosetta), CPU-only — GPU
// stays free for whisper.cpp Metal acceleration.
inline const std::string KOKORO_MODEL_DIR   = "models/kokoro-en-v0_19";
constexpr bool           USE_KOKORO         = true;    // true → Kokoro neural TTS; false → macOS `say`
constexpr int            KOKORO_SPEAKER_ID  = 0;       // 0=af (default female)  see voices.bin
constexpr float          KOKORO_SPEED       = 1.0f;    // 1.0 = normal speed
constexpr int            KOKORO_SAMPLE_RATE = 24000;   // Hz — Kokoro native output rate

// ─── Input device selection ────────────────────────────────────
// Run the daemon once to see the device list, then set this index.
// -1 = system default input device.
constexpr int INPUT_DEVICE_INDEX = -1;

// ─── Conversation management ───────────────────────────────────
constexpr int MAX_HISTORY    = 4;
constexpr int MAX_TOOL_DEPTH = 5;

// ─── Session persistence ───────────────────────────────────────
// Persist conversation context across daemon restarts.
// Stores last N turns to /tmp so the user doesn't lose context.
inline const std::string SESSION_FILE     = "/tmp/demascas_session.json";
constexpr int            SESSION_MAX_TURNS = 6;  // max turns to persist (keep it small)

// ─── Wake word ─────────────────────────────────────────────────
inline const std::string WAKE_WORD = "demascas";

// ─── Whisper prompt context (helps recognition accuracy) ───────
// Giving whisper an initial_prompt with expected vocabulary
// dramatically reduces hallucinations and improves name recognition.
inline const std::string WHISPER_PROMPT =
    "DEMASCAS, Damascus, Daksh, open, close, search, remember, "
    "screenshot, Safari, Chrome, YouTube, minimize, maximize, "
    "copy, paste, undo, redo, quit, scroll, what, how, why, "
    "my name is, stop, bye, goodbye, thank you";

}  // namespace demascas::config
