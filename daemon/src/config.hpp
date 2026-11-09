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

// ─── LLM (plug-in — DEMASCAS ships without a model) ────────────
// The endpoint, model and context size come from the tool server at
// startup (GET /llm), which reads ~/.demascas/config.json written by
// `python main.py setup`.  Any OpenAI-compatible local runtime works:
// Ollama, LM Studio, llama.cpp server, MLX, Jan.
constexpr int            TOOL_PREDICT     = 200;      // max tokens for tool-calling responses
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
// End-of-utterance: how long you must be silent before DEMASCAS starts
// working.  This is added to EVERY command's response time — it was 2.0 s.
// Raise it (1.2–1.5) if you pause mid-sentence and get cut off.
constexpr float SILENCE_DURATION     = 1.0f;
constexpr int   CAPTURE_POLL_MS      = 50;      // mic polling granularity (end-of-speech precision)
constexpr float WAKE_LISTEN_WINDOW   = 2.0f;    // seconds of audio in the sliding wake-word window
constexpr float WAKE_HOP_SEC         = 0.75f;   // re-check the window every hop (was a fixed 2 s chunk)
constexpr int   POST_TTS_GUARD_MS    = 250;     // ignore mic this long after DEMASCAS stops talking (echo tail)
constexpr float COMMAND_TIMEOUT      = 10.0f;   // max seconds to wait for first command
constexpr float FOLLOWUP_TIMEOUT     = 15.0f;   // max seconds between follow-ups

// ─── Screen stability ──────────────────────────────────────────
constexpr float SCREEN_STABLE_THRESHOLD = 0.025f;  // fraction of pixels changed to consider "unstable"

// ─── Tool execution limits ─────────────────────────────────────
constexpr int   TOOL_TIMEOUT_MS      = 8000;    // max ms for a single tool call
constexpr int   REACT_MAX_ERRORS     = 3;       // consecutive tool errors before hard stop
constexpr long  TASK_EXEC_TIMEOUT_SEC = 60L;    // max seconds for STE /plan_and_execute (longer due to multi-step waits)
// Max seconds to wait for POST /agent.  Must be LONGER than the agent's own
// wall-clock budget (AGENT_TIME_BUDGET = 80 s in server/agents/base_agent.py)
// so the agent always finishes/aborts before the daemon gives up on it.
constexpr long  AGENT_TIMEOUT_SEC    = 100L;

// ─── Hallucination / sleep-back ────────────────────────────────
constexpr int   MAX_CONSECUTIVE_BLANKS = 2;     // blanks before returning to SLEEPING
constexpr float TTS_COOLDOWN_SEC     = 1.0f;    // seconds to ignore mic after TTS finishes (anti-echo)

// ─── TTS (macOS `say` command — fallback) ──────────────────────
inline const std::string TTS_VOICE = "Samantha";  // American female — clear + natural
constexpr int            TTS_RATE  = 190;          // words per minute

// ─── Whisper speed ─────────────────────────────────────────────
// whisper.cpp silently returns NOTHING for clips shorter than ~1 s
// ("yes", "stop"), so short clips are padded with silence to this length.
constexpr float WHISPER_MIN_SEC      = 1.5f;
// Size the command model's encoder window to the clip instead of a fixed
// 30 s (measured on M1: base 219 ms → 89 ms, same transcripts).  Falls back
// to the full window if the short pass returns nothing.
constexpr bool  WHISPER_ADAPTIVE_CTX = true;

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
constexpr int            KOKORO_THREADS     = 4;       // measured on M1: ~30% faster synthesis than 2

// Wake acknowledgement.  Set to "" for chime-only (saves ~1.1 s per wake).
inline const std::string WAKE_ACK_PHRASE = "At your service.";

// Fixed phrases synthesized once in the background at startup and then
// played instantly (Kokoro needs ~0.5-0.9 s to synthesize each of these).
inline const char* const TTS_CACHED_PHRASES[] = {
    "At your service.", "Standing by.", "Done.", "Okay.", "Cancelled.",
    "Okay, cancelled.", "I'm here. What can I do for you?",
    "I didn't hear a clear yes, so I cancelled it.", "Should I go ahead?",
    "That shortcut didn't work.", "I wasn't able to finish that.",
    "Sorry, I couldn't reach my language model.",
    "Please check that the MLX server is running.",
    "I couldn't get that to work.",
};

// ─── Input device selection ────────────────────────────────────
// Run the daemon once to see the device list, then set this index.
// -1 = system default input device.
constexpr int INPUT_DEVICE_INDEX = -1;

// ─── Conversation management ───────────────────────────────────
constexpr int MAX_HISTORY    = 4;
constexpr int MAX_TOOL_DEPTH = 5;

// ─── Session persistence ───────────────────────────────────────
// Persist conversation context across daemon restarts.
// Stored at $HOME/.demascas/session.json with 0600 permissions (it holds
// conversation text, so it must not live in world-readable /tmp).
inline const std::string SESSION_FILE_NAME = "session.json";
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
