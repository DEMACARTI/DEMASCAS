/**
 * DEMASCAS — daemon/src/main.cpp
 * The bare-metal C++ daemon entry point — Real-Time Architecture.
 *
 * Multi-threaded state machine with full-duplex audio:
 *   SLEEPING  → (wake-word detected) →
 *   LISTENING → (command captured)   →
 *   PROCESSING → (streaming LLM + sentence-by-sentence TTS) →
 *   back to LISTENING (follow-up) or SLEEPING (timeout).
 *
 * Interrupt support: if the user speaks while TTS is playing,
 * the audio stops instantly and the new command is processed.
 *
 * The Python GIL never touches this hot path.  Audio capture,
 * wake-word detection, Ollama HTTP, shortcut matching, and TTS
 * all run in native C++ threads.  Only tool execution crosses
 * the IPC bridge to the Python micro-service.
 */

#include <atomic>
#include <algorithm>
#include <csignal>
#include <iostream>

#include "config.hpp"
#include "audio_engine.hpp"
#include "whisper_engine.hpp"
#include "ollama_client.hpp"
#include "tool_bridge.hpp"
#include "tts.hpp"
#include "shortcut_map.hpp"

// ── Graceful shutdown via SIGINT / SIGTERM ───────────────────────────

static std::atomic<bool> g_running{true};

static void on_signal(int) { g_running = false; }

// ── String helpers ──────────────────────────────────────────────────

static std::string to_lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), ::tolower);
    return s;
}

static bool contains(const std::string& haystack, const std::string& needle) {
    return to_lower(haystack).find(to_lower(needle)) != std::string::npos;
}

/// Wake-word variants — whisper tiny transcribes "demascas" in many ways.
/// Mirrors the Python audio.py logic: "damascus" | "demascas" | "wake up".
static bool is_wake_phrase(const std::string& text) {
    std::string t = to_lower(text);
    // Strip common whisper hallucination wrappers
    // (whisper loves adding [Music], (music), etc.)
    static const char* HALLUCINATIONS[] = {
        "thank you", "thanks for watching", "subscribe",
        "[music]", "(music)", "you", "bye",
        "the end", "silence", "...",
        "[blank_audio]", "blank audio",
    };
    for (auto& h : HALLUCINATIONS)
        if (t == h) return false;

    // Common whisper transcriptions of "demascas" / "hey demascas"
    // Includes observed real transcriptions from whisper tiny:
    //   "maskers", "the mask is", "Damascus", etc.
    static const char* WAKE_WORDS[] = {
        "damascus", "demascas", "demascus", "damaskus",
        "de mascas", "the mascus", "damasco", "demasco",
        "dimascas", "dimascus", "damask", "da mascas",
        "hey damascus", "hey demascas",
        "maskers", "the mask is", "the mask",
        "mascas", "mascus", "demas",
        "wake up",
    };
    for (auto& w : WAKE_WORDS)
        if (t.find(w) != std::string::npos) return true;

    return false;
}

// ── State machine ───────────────────────────────────────────────────

enum class State { SLEEPING, LISTENING, PROCESSING };

int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    namespace cfg = demascas::config;

    std::cout << "\n"
              << "╔══════════════════════════════════════════════╗\n"
              << "║  DEMASCAS — Bare-Metal C++ Daemon             ║\n"
              << "║  Apple Silicon • whisper.cpp • Ollama          ║\n"
              << "║  Full-duplex • Streaming • Interruptible       ║\n"
              << "╚══════════════════════════════════════════════╝\n\n";

    // ── 1. Connect to the Python tool micro-service ─────────────
    demascas::ToolBridge bridge;
    if (!bridge.wait_for_server()) {
        std::cerr << "[FATAL] Tool server unreachable. Start it with:\n"
                  << "  python server/tool_server.py\n";
        return 1;
    }

    // ── 2. Fetch tool schemas for Ollama function-calling ───────
    auto schemas = bridge.fetch_tool_schemas();
    std::cout << "[✓] Loaded " << schemas.size() << " tool schemas\n";

    // ── 3. Init Ollama client & prewarm ─────────────────────────
    demascas::OllamaClient ollama;
    ollama.set_tools(schemas);
    ollama.prewarm();

    // ── 4. Init shortcut map (C++ fast-path, no LLM needed) ────
    demascas::ShortcutMap shortcuts;

    // ── 5. Init TTS ─────────────────────────────────────────────
    demascas::TTS tts;

    // ── 6. Init audio engine (PortAudio) ────────────────────────
    demascas::AudioEngine audio;
    if (!audio.start()) {
        std::cerr << "[FATAL] Cannot open microphone.  Check System "
                  << "Settings → Privacy → Microphone.\n";
        return 1;
    }

    // ── 7. Init whisper.cpp (tiny + base models) ────────────────
    demascas::WhisperEngine whisper;
    if (!whisper.init(cfg::WHISPER_TINY_MODEL, cfg::WHISPER_BASE_MODEL)) {
        std::cerr << "[FATAL] Cannot load Whisper models.  Run:\n"
                  << "  ./scripts/build.sh   (downloads models automatically)\n";
        return 1;
    }

    std::cout << "\n[*] DEMASCAS is ready.  Say \"" << cfg::WAKE_WORD
              << "\" to activate!\n\n";

    // ── State machine loop ──────────────────────────────────────

    State state = State::SLEEPING;
    std::string current_command;
    std::atomic<bool> cancel_llm{false};   // set to cancel streaming

    while (g_running) {
        switch (state) {

        // ────────────────────────────────────────────────────────
        // SLEEPING — continuously scan for the wake word
        // ────────────────────────────────────────────────────────
        case State::SLEEPING: {
            auto chunk = audio.capture_chunk(cfg::WAKE_LISTEN_WINDOW);
            if (chunk.empty()) break;

            // VAD gate — skip whisper if the chunk is silence
            float energy = demascas::AudioEngine::rms(
                chunk.data(), chunk.size());
            if (energy < cfg::VAD_ENERGY_THRESHOLD * 0.5f) {
                break;
            }

            auto transcript = whisper.transcribe_wake(chunk);
            std::string tl = to_lower(transcript);
            bool is_artifact = transcript.empty()
                || tl.find("blank_audio") != std::string::npos
                || tl.find("blank audio") != std::string::npos;
            if (!is_artifact) {
                std::cout << "[👂 heard] \"" << transcript
                          << "\"  (energy=" << energy << ")\n";
            }

            if (is_wake_phrase(transcript)) {
                std::cout << "[🔊 WAKE WORD] \"" << transcript << "\"\n";
                tts.play_sound("Glass");
                tts.enqueue("At your service.");
                state = State::LISTENING;
            }
            break;
        }

        // ────────────────────────────────────────────────────────
        // LISTENING — capture a full spoken command
        //   If TTS is still playing (e.g. previous response)
        //   and we detect speech, interrupt it.
        // ────────────────────────────────────────────────────────
        case State::LISTENING: {
            std::cout << "[🟢 LISTENING]  (timeout "
                      << cfg::FOLLOWUP_TIMEOUT << " s)\n";

            // If TTS is still speaking from a previous response,
            // keep listening — interrupt if user speaks
            auto utterance = audio.capture_utterance(
                cfg::FOLLOWUP_TIMEOUT, cfg::SILENCE_DURATION);

            if (utterance.empty()) {
                std::cout << "[*] No speech detected — going back to sleep.\n";
                tts.interrupt();  // stop any lingering speech
                tts.enqueue("Standing by.");
                tts.wait_done();
                ollama.reset();
                state = State::SLEEPING;
                break;
            }

            // If TTS was playing, interrupt it (user is barging in)
            if (tts.is_playing()) {
                std::cout << "[⚡ INTERRUPT] User speaking — cutting TTS.\n";
                cancel_llm = true;
                tts.interrupt();
            }

            current_command = whisper.transcribe_command(utterance);
            if (current_command.empty()) {
                std::cout << "[*] Empty transcription — back to sleep.\n";
                tts.enqueue("Standing by.");
                tts.wait_done();
                ollama.reset();
                state = State::SLEEPING;
                break;
            }

            std::cout << "[🗣️  YOU SAID]: \"" << current_command << "\"\n";
            state = State::PROCESSING;
            break;
        }

        // ────────────────────────────────────────────────────────
        // PROCESSING — route through fast-path or streaming tool loop
        // ────────────────────────────────────────────────────────
        case State::PROCESSING: {
            std::string reply;
            std::string path_taken;
            cancel_llm = false;

            // Sentence callback — streams each sentence to TTS
            auto on_sentence = [&tts](const std::string& sentence) {
                if (!sentence.empty()) {
                    std::cout << "[🔊 TTS] " << sentence << "\n";
                    tts.enqueue(sentence);
                }
            };

            // PATH 1: Shortcut fast-path (< 1 µs, bypasses LLM entirely)
            auto shortcut = shortcuts.match(current_command);
            if (shortcut) {
                path_taken = "shortcut";
                std::cout << "[⚡ SHORTCUT] "
                          << current_command << " → " << *shortcut << "\n";
                bridge.execute("press_keyboard_shortcut",
                               {{"keys", *shortcut}});
                reply = ollama.fast_chat_streaming(
                    current_command, on_sentence, cancel_llm);
            }
            // PATH 2: Simple question (streaming fast LLM, no tool schemas)
            else if (shortcuts.is_simple_question(current_command)) {
                path_taken = "fast";
                std::cout << "[⚡ FAST PATH] Simple question — streaming.\n";
                reply = ollama.fast_chat_streaming(
                    current_command, on_sentence, cancel_llm);
            }
            // PATH 3: Full streaming tool-calling ReAct loop
            else {
                path_taken = "tool";
                std::cout << "[🔧 TOOL PATH] Full processing — streaming.\n";

                std::string learned_ctx;
                try {
                    learned_ctx = bridge.fetch_learned_context(current_command);
                } catch (...) {}

                reply = ollama.chat_with_tools_streaming(
                    current_command, bridge, on_sentence, cancel_llm,
                    learned_ctx);
            }

            // Wait for TTS to finish (unless interrupted)
            tts.wait_done();
            tts.play_sound("Tink");

            if (!reply.empty()) {
                std::cout << "[DEMASCAS]: " << reply << "\n";
            }

            // Log interaction for learning (fire-and-forget)
            try {
                bridge.log_interaction(
                    current_command,
                    path_taken,
                    ollama.last_tools_used(),
                    reply.empty() ? "Done." : reply
                );
            } catch (...) {}

            // Stay awake for a follow-up command
            state = State::LISTENING;
            break;
        }

        }  // switch
    }  // while

    // ── Cleanup ─────────────────────────────────────────────────
    std::cout << "\n[*] Shutting down DEMASCAS daemon.\n";
    audio.stop();
    whisper.shutdown();
    return 0;
}
