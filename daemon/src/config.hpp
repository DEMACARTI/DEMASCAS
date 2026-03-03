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

// ─── Ollama (llama.cpp under the hood) ─────────────────────────
inline const std::string OLLAMA_URL       = "http://127.0.0.1:11434/api/chat";
inline const std::string OLLAMA_MODEL     = "qwen2.5:3b";
constexpr int            NUM_CTX_TOOLS    = 4096;
constexpr int            NUM_CTX_FAST     = 2048;
constexpr int            NUM_PREDICT_TOOLS = 300;
constexpr int            NUM_PREDICT_FAST = 100;
constexpr int            NUM_BATCH        = 256;
inline const std::string KEEP_ALIVE       = "5m";

// ─── Python tool micro-service ─────────────────────────────────
inline const std::string TOOL_SERVER_BASE   = "http://127.0.0.1:5001";
inline const std::string TOOL_EXEC_URL      = "http://127.0.0.1:5001/tool";
inline const std::string TOOL_SCHEMA_URL    = "http://127.0.0.1:5001/tools";
inline const std::string TOOL_HEALTH_URL    = "http://127.0.0.1:5001/health";

// ─── Audio capture (PortAudio → whisper.cpp) ───────────────────
constexpr int   SAMPLE_RATE          = 16000;   // whisper.cpp requires 16 kHz mono
constexpr int   FRAMES_PER_BUFFER    = 512;
constexpr float VAD_ENERGY_THRESHOLD = 0.005f;  // RMS threshold for "speech"
constexpr float SILENCE_DURATION     = 2.0f;    // seconds of silence → end of utterance
constexpr float WAKE_LISTEN_WINDOW   = 2.0f;    // seconds per wake-word scan
constexpr float COMMAND_TIMEOUT      = 10.0f;   // max seconds to wait for first command
constexpr float FOLLOWUP_TIMEOUT     = 15.0f;   // max seconds between follow-ups

// ─── TTS (macOS `say` command) ─────────────────────────────────
inline const std::string TTS_VOICE = "Daniel";
constexpr int            TTS_RATE  = 180;        // words per minute

// ─── Conversation management ───────────────────────────────────
constexpr int MAX_HISTORY    = 4;
constexpr int MAX_TOOL_DEPTH = 5;

// ─── Wake word ─────────────────────────────────────────────────
inline const std::string WAKE_WORD = "demascas";

}  // namespace demascas::config
