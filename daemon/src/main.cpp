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
 * wake-word detection, MLX HTTP, shortcut matching, and TTS
 * all run in native C++ threads.  Only tool execution crosses
 * the IPC bridge to the Python micro-service.
 */

#include <atomic>
#include <algorithm>
#include <chrono>
#include <csignal>
#include <iostream>
#include <sstream>
#include <thread>

#include "config.hpp"
#include "audio_engine.hpp"
#include "whisper_engine.hpp"
#include "llm_client.hpp"
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

/// Extract the first file path from a find_file tool result string.
/// Handles both proper JSON and Python dict str representations.
static std::string extract_first_file_path(const std::string& tool_result) {
    if (tool_result.empty()) return "";

    // Strategy 1: Try JSON parse for "first_match_path"
    try {
        auto rj = nlohmann::json::parse(tool_result);
        if (rj.contains("first_match_path") && !rj["first_match_path"].is_null()) {
            return rj["first_match_path"].get<std::string>();
        }
        if (rj.contains("matches") && rj["matches"].is_array() && !rj["matches"].empty()) {
            return rj["matches"][0].value("path", "");
        }
    } catch (...) {}

    // Strategy 2: String-based extraction for Python dict format
    // Handles: {'first_match_path': '/Users/x/Downloads/SIH.pdf', ...}
    {
        std::string key = "first_match_path";
        auto pos = tool_result.find(key);
        if (pos != std::string::npos) {
            pos += key.size();
            // Skip past ':' and whitespace/quotes
            while (pos < tool_result.size() &&
                   (tool_result[pos] == '\'' || tool_result[pos] == '"' ||
                    tool_result[pos] == ':' || tool_result[pos] == ' '))
                ++pos;
            if (pos < tool_result.size() && tool_result[pos] != 'N') { // not "None"
                char quote = tool_result[pos - 1]; // last skipped char is the opening quote
                if (quote != '\'' && quote != '"') {
                    // look backwards for the quote
                    for (size_t q = pos; q > pos - 4 && q > 0; --q) {
                        if (tool_result[q - 1] == '\'' || tool_result[q - 1] == '"') {
                            quote = tool_result[q - 1];
                            break;
                        }
                    }
                }
                auto end = tool_result.find(quote, pos);
                if (end != std::string::npos && end > pos) {
                    std::string path = tool_result.substr(pos, end - pos);
                    if (!path.empty() && path[0] == '/') return path;
                    if (!path.empty() && path[0] == '~') return path;
                }
            }
        }

        // Fallback: try matches[0].path
        auto mpos = tool_result.find("'path':");
        if (mpos == std::string::npos) mpos = tool_result.find("\"path\":");
        if (mpos != std::string::npos) {
            mpos += 7; // skip past 'path':
            while (mpos < tool_result.size() &&
                   (tool_result[mpos] == ' ' || tool_result[mpos] == '\'' || tool_result[mpos] == '"'))
                ++mpos;
            char q = tool_result[mpos - 1];
            if (q != '\'' && q != '"') q = '\'';
            auto mend = tool_result.find(q, mpos);
            if (mend != std::string::npos && mend > mpos) {
                std::string path = tool_result.substr(mpos, mend - mpos);
                if (!path.empty() && (path[0] == '/' || path[0] == '~')) return path;
            }
        }
    }

    return ""; // extraction failed
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

/// Detect whisper.cpp hallucinations — phantom transcriptions from silence/noise.
/// Whisper tiny commonly hallucinates these when fed non-speech audio.
static bool is_hallucination(const std::string& text) {
    std::string t = to_lower(text);

    // Trim whitespace
    while (!t.empty() && std::isspace(static_cast<unsigned char>(t.front())))
        t.erase(t.begin());
    while (!t.empty() && std::isspace(static_cast<unsigned char>(t.back())))
        t.pop_back();

    if (t.empty()) return true;

    // Exact-match known hallucinations
    static const char* KNOWN[] = {
        "[blank_audio]", "blank_audio", "blank audio",
        "thank you.", "thank you", "thanks for watching.",
        "thanks for watching", "thank you for watching.",
        "thank you for watching", "like and subscribe",
        "subscribe", "you",
        "the end.", "the end", "...", "..", ".",
        "silence", "[silence]", "(silence)",
        "[music]", "(music)", "(upbeat music)", "(soft music)",
        "[no audio]", "(no audio)",
        "[inaudible]", "(inaudible)",
    };
    for (auto& h : KNOWN)
        if (t == h) return true;

    // Anything entirely in brackets or parentheses is a hallucination:
    //   [BELL RINGS], (clicking), [Clock ticking], [Dog clink], etc.
    if (t.size() >= 3 &&
        ((t.front() == '[' && t.back() == ']') ||
         (t.front() == '(' && t.back() == ')')))
        return true;

    // Very short junk (≤ 2 alphanumeric chars) — almost always hallucination
    std::string stripped;
    for (char c : t) {
        if (std::isalnum(static_cast<unsigned char>(c)))
            stripped += c;
    }
    if (stripped.size() <= 2) return true;

    return false;
}

// ── Transcription normalizer ────────────────────────────────────────
// Fixes common Whisper artifacts before routing.
// CRITICAL: NEVER modifies word boundaries of normal words.
// Only operates on explicit patterns (filler prefix, spelled-out letters/digits).

static std::string normalize_transcription(const std::string& raw) {
    std::string s = raw;
    if (s.empty()) return s;

    // ── STEP 1: Strip leading filler words ──────────────────────
    // ONLY the very first word, ONLY if followed by comma or period.
    // "alright, tell me the time" → "tell me the time"
    // "okay help me" → unchanged (no comma after "okay")
    {
        static const char* FILLERS[] = {
            "alright", "right", "okay", "so", "well", "um", "uh",
        };
        std::string sl = to_lower(s);
        for (auto& f : FILLERS) {
            std::string filler(f);
            if (sl.size() > filler.size() + 1 &&
                sl.compare(0, filler.size(), filler) == 0) {
                // Must be followed by comma or period, then a space
                char next = sl[filler.size()];
                if (next == ',' || next == '.') {
                    size_t start = filler.size() + 1;  // skip the comma/period
                    while (start < s.size() && s[start] == ' ') ++start;
                    if (start < s.size()) {
                        s = s.substr(start);
                    }
                    break;
                }
            }
        }
    }

    // ── STEP 2: Collapse ONLY explicit spelled-out letter sequences ─
    // Pattern: single letter, then (comma OR hyphen) + optional space, then
    // another single letter, repeated 2+ times.
    // "S, I, H" → "SIH"   |   "S-I-H" → "SIH"   |   "P, D, F" → "PDF"
    // NEVER touches normal words. Only matches isolated single letters
    // separated by comma/hyphen delimiters.
    {
        std::string result;
        size_t i = 0;
        while (i < s.size()) {
            // Check: are we at a word-boundary start of a potential letter sequence?
            bool at_boundary = (i == 0) ||
                               !std::isalpha(static_cast<unsigned char>(s[i - 1]));
            bool is_single_alpha = std::isalpha(static_cast<unsigned char>(s[i])) &&
                                   (i + 1 >= s.size() ||
                                    !std::isalpha(static_cast<unsigned char>(s[i + 1])));

            if (at_boundary && is_single_alpha) {
                // Try to collect a sequence of comma/hyphen-separated single letters
                std::string letters;
                letters += static_cast<char>(std::toupper(static_cast<unsigned char>(s[i])));
                size_t j = i + 1;
                while (j < s.size()) {
                    // Expect exactly: comma or hyphen, optional space, then single letter
                    if (j < s.size() && (s[j] == ',' || s[j] == '-')) {
                        size_t k = j + 1;
                        // skip optional spaces (max 1-2)
                        while (k < s.size() && s[k] == ' ' && k - j <= 2) ++k;
                        // Next must be a single alpha not followed by another alpha
                        if (k < s.size() &&
                            std::isalpha(static_cast<unsigned char>(s[k])) &&
                            (k + 1 >= s.size() || !std::isalpha(static_cast<unsigned char>(s[k + 1])))) {
                            letters += static_cast<char>(std::toupper(static_cast<unsigned char>(s[k])));
                            j = k + 1;
                            continue;
                        }
                    }
                    break;
                }
                if (letters.size() >= 2) {
                    result += letters;
                    i = j;
                } else {
                    result += s[i];
                    ++i;
                }
            } else {
                result += s[i];
                ++i;
            }
        }
        s = result;
    }

    // ── STEP 3: Collapse comma-separated single DIGITS ──────────
    //    "2, 0, 2, 5" → "2025"
    //    ONLY when every token between commas is a single digit.
    {
        std::string result;
        size_t i = 0;
        while (i < s.size()) {
            if (std::isdigit(static_cast<unsigned char>(s[i])) &&
                (i + 1 >= s.size() || !std::isdigit(static_cast<unsigned char>(s[i + 1])))) {
                std::string digits;
                digits += s[i];
                size_t j = i + 1;
                while (j < s.size()) {
                    if (s[j] == ',') {
                        size_t k = j + 1;
                        while (k < s.size() && s[k] == ' ' && k - j <= 2) ++k;
                        if (k < s.size() &&
                            std::isdigit(static_cast<unsigned char>(s[k])) &&
                            (k + 1 >= s.size() || !std::isdigit(static_cast<unsigned char>(s[k + 1])))) {
                            digits += s[k];
                            j = k + 1;
                            continue;
                        }
                    }
                    break;
                }
                if (digits.size() >= 2) {
                    result += digits;
                    i = j;
                } else {
                    result += s[i];
                    ++i;
                }
            } else {
                result += s[i];
                ++i;
            }
        }
        s = result;
    }

    // ── STEP 4: [FILE] tag — ONLY when ALL conditions are met ───
    // (a) A known file extension word appears as a standalone word
    // (b) The word before the extension looks like a filename (not "a", "the", "my", "this")
    // (c) The sentence contains "open" or "find" or "search"
    // Example CORRECT: "open SIH PDF" → "open SIH PDF [FILE]"
    // Example WRONG: "help me opening a PDF" → NO tag (preceded by "a")
    // Example WRONG: "open a PDF in downloads" → NO tag (preceded by "a")
    {
        static const std::vector<std::string> EXT_WORDS = {
            "pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx",
            "mp4", "mp3", "zip", "txt", "jpg", "png", "csv",
        };
        static const std::vector<std::string> GENERIC_PRECEDES = {
            "a", "the", "my", "this", "that", "any", "some", "an",
            "opening", "open", "find", "search",
        };
        static const std::vector<std::string> ACTION_WORDS = {
            "open", "find", "search", "locate", "look",
        };

        std::string sl = to_lower(s);

        // Check for action word presence
        bool has_action = false;
        for (auto& aw : ACTION_WORDS) {
            if (sl.find(aw) != std::string::npos) { has_action = true; break; }
        }

        if (has_action) {
            // Tokenize to find extension word and check preceding word
            std::vector<std::string> tokens;
            {
                std::string tok;
                for (char c : sl) {
                    if (c == ' ' || c == ',' || c == '.') {
                        if (!tok.empty()) { tokens.push_back(tok); tok.clear(); }
                    } else {
                        tok += c;
                    }
                }
                if (!tok.empty()) tokens.push_back(tok);
            }

            bool should_tag = false;
            for (size_t ti = 0; ti < tokens.size(); ++ti) {
                bool is_ext = false;
                for (auto& ew : EXT_WORDS)
                    if (tokens[ti] == ew) { is_ext = true; break; }
                if (!is_ext) continue;

                // Check preceding word — must exist and NOT be generic
                if (ti == 0) continue;  // no preceding word
                const std::string& prev = tokens[ti - 1];
                bool is_generic = false;
                for (auto& gw : GENERIC_PRECEDES)
                    if (prev == gw) { is_generic = true; break; }
                if (is_generic) continue;

                // Preceding word looks like a filename => tag it
                should_tag = true;
                break;
            }

            if (should_tag) {
                // Trim trailing punctuation before tagging
                while (!s.empty() && (s.back() == '.' || s.back() == ',' || s.back() == ' '))
                    s.pop_back();
                s += " [FILE]";
            }
        }
    }

    // ── STEP 5: Trim trailing period and whitespace ─────────────
    while (!s.empty() && s.back() == '.')
        s.pop_back();
    while (!s.empty() && s.front() == ' ') s.erase(s.begin());
    while (!s.empty() && s.back() == ' ')  s.pop_back();

    return s;
}

/// Detect dismissal / stop commands — ChatGPT-style.
/// When the user says "stop", "bye", "enough", respect it immediately.
static bool is_dismiss(const std::string& text) {
    std::string t = to_lower(text);
    static const char* DISMISS[] = {
        "stop", "shut up", "be quiet", "quiet",
        "go to sleep", "go sleep", "sleep",
        "goodbye", "good bye", "bye bye", "bye",
        "that's all", "that is all", "that's it",
        "never mind", "nevermind", "nah",
        "can you stop", "you can stop", "stop talking",
        "enough", "i'm done", "we're done", "done",
        "go away", "stand by", "standing by",
        "thanks that's all", "thank you goodbye",
        "no thanks", "no thank you",
    };
    for (auto& d : DISMISS)
        if (t.find(d) != std::string::npos) return true;
    return false;
}

// ── State machine ───────────────────────────────────────────────────

enum class State { SLEEPING, LISTENING, PROCESSING, CONFIRMING, CLARIFYING };

int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    namespace cfg = demascas::config;

    std::cout << "\n"
              << "╔══════════════════════════════════════════════╗\n"
              << "║  DEMASCAS — Bare-Metal C++ Daemon             ║\n"
              << "║  whisper.cpp • LLM (Ollama/MLX) • Kokoro TTS  ║\n"
              << "║  Qwen2.5-3B • cross-platform • streaming      ║\n"
              << "╚══════════════════════════════════════════════╝\n\n";

    // ── 1. Connect to the Python tool micro-service ─────────────
    demascas::ToolBridge bridge;
    if (!bridge.wait_for_server()) {
        std::cerr << "[FATAL] Tool server unreachable. Start it with:\n"
                  << "  python server/tool_server.py\n";
        return 1;
    }

    // ── 2. Fetch tool schemas (for tool router filtering) ───────
    auto schemas = bridge.fetch_tool_schemas();
    std::cout << "[✓] Loaded " << schemas.size() << " tool schemas\n";

    // ── 3. Init LLM client & verify server ─────────────────
    demascas::LLMClient llm;
    llm.prewarm();
    llm.load_session();  // restore context from previous session

    // ── 4. Init shortcut map (C++ fast-path, no LLM needed) ────
    demascas::ShortcutMap shortcuts;

    // ── 5. Init TTS ─────────────────────────────────────────────
    demascas::TTS tts;

    // ── 6. Init audio engine (PortAudio) ────────────────────────
    demascas::AudioEngine audio;
    demascas::AudioEngine::list_devices();
    if (!audio.start(cfg::INPUT_DEVICE_INDEX)) {
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
    whisper.set_prompt(cfg::WHISPER_PROMPT);

    std::cout << "\n[*] DEMASCAS is ready and listening.\n\n";

    // ── State machine loop ──────────────────────────────────────

    State state = State::LISTENING;
    std::string current_command;
    std::atomic<bool> cancel_llm{false};   // set to cancel streaming
    int consecutive_blank = 0;             // hallucinations since last real command

    // ── CONFIRMING state — pending action for user approval ─────
    std::string pending_tool;
    nlohmann::json pending_args;
    std::string pending_description;

    // ── CLARIFYING state — multi-match file disambiguation ──────
    //    When find_file returns >1 match, we speak the choices and
    //    wait for the user to say which one they want.
    std::vector<std::pair<std::string, std::string>> pending_file_choices;  // {name, path}
    std::string clarifying_goal;  // what user wanted to do with the file

    using clk = std::chrono::steady_clock;
    auto last_tts_done = clk::now() - std::chrono::seconds(5); // no cooldown at start

    // Sentence callback — streams each sentence to TTS.
    // Filters out raw JSON tool calls and low-level action chatter
    // so the user only hears meaningful responses.
    // Defined here (not inside PROCESSING) so CONFIRMING can reuse it.
    auto on_sentence = [&tts](const std::string& sentence) {
        if (sentence.empty()) return;

        // ── Skip raw JSON tool calls that leaked through ────
        // e.g. {"tool":"press_keyboard_shortcut","args":{...}}
        {
            auto pos = sentence.find_first_not_of(" \t\n");
            if (pos != std::string::npos && sentence[pos] == '{') {
                std::cout << "[🔊 TTS] (filtered JSON) " << sentence.substr(0, 80) << "...\n";
                return;
            }
        }

        // ── Skip low-level technical narration ──────────────
        // Phrases the LLM generates that describe internal actions
        // rather than providing useful info to the user.
        {
            std::string lower;
            lower.reserve(sentence.size());
            for (char c : sentence)
                lower += static_cast<char>(::tolower(static_cast<unsigned char>(c)));

            static const char* SKIP_PHRASES[] = {
                "press enter", "pressed enter",
                "pressing ", "clicked ",
                "clicking ", "typed ",
                "typing ", "scrolled ",
                "scrolling ", "keystroke",
                "key code", "keyboard shortcut",
                "the terminal should",
                "you can now search",
                "you can now browse",
                "what would you like to do next",
                "command you entered",
            };
            bool skip = false;
            for (auto& phrase : SKIP_PHRASES) {
                if (lower.find(phrase) != std::string::npos) {
                    skip = true;
                    break;
                }
            }
            if (skip) {
                std::cout << "[🔊 TTS] (filtered chatter) " << sentence.substr(0, 80) << "...\n";
                return;
            }
        }

        std::cout << "[🔊 TTS] " << sentence << "\n";
        tts.enqueue(sentence);
    };

    while (g_running) {
        switch (state) {

        // ────────────────────────────────────────────────────────
        // SLEEPING — continuously scan for the wake word
        // ────────────────────────────────────────────────────────
        case State::SLEEPING: {
            // ── Audio health check (sleep/wake recovery) ────────
            if (!audio.is_stream_healthy()) {
                std::cout << "[!] Audio stream unhealthy — reinitializing...\n";
                audio.reinit_on_wake(cfg::INPUT_DEVICE_INDEX);
                break;  // retry next iteration
            }

            // ── Anti-echo: skip while TTS is playing or within cooldown
            if (tts.is_playing()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                audio.drain();
                break;
            }
            {
                float since_tts = std::chrono::duration<float>(
                    clk::now() - last_tts_done).count();
                if (since_tts < cfg::TTS_COOLDOWN_SEC) {
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(100));
                    audio.drain();
                    break;
                }
            }

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

            // Wake-word gate preserved here for easy restoration if needed.
            // if (is_wake_phrase(transcript)) {
            //     std::cout << "[🔊 WAKE WORD] \"" << transcript << "\"\n";
            //     tts.play_sound("Glass");
            //     tts.enqueue("At your service.");
            //     tts.wait_done();
            //     // Anti-echo: wait + drain so mic doesn't hear our own voice
            //     last_tts_done = clk::now();
            //     std::this_thread::sleep_for(
            //         std::chrono::milliseconds(500));
            //     audio.drain();
            //     state = State::LISTENING;
            // }

            // Wake on any non-artifact speech while the activation-word gate
            // is disabled.
            if (!is_artifact) {
                std::cout << "[🔊 WAKE BY SPEECH] \"" << transcript << "\"\n";
                tts.play_sound("Glass");
                tts.enqueue("At your service.");
                tts.wait_done();
                // Anti-echo: wait + drain so mic doesn't hear our own voice
                last_tts_done = clk::now();
                std::this_thread::sleep_for(
                    std::chrono::milliseconds(500));
                audio.drain();
                state = State::LISTENING;
            }
            break;
        }

        // ────────────────────────────────────────────────────────
        // LISTENING — capture a full spoken command
        //   Dynamic VAD Ducking: while TTS is playing, the mic
        //   stays hot but with an elevated energy threshold.
        //   If the user speaks OVER the speaker bleed, we
        //   interrupt TTS instantly and capture the command.
        // ────────────────────────────────────────────────────────
        case State::LISTENING: {
            // ── Barge-in monitor: poll mic while TTS is speaking ────
            //    Your voice at 30 cm ≈ 0.20–0.50 RMS.
            //    Speaker bleed into mic ≈ 0.05–0.15 RMS.
            //    BARGE_IN_THRESHOLD (0.15) separates the two.
            if (tts.is_playing()) {
                std::cout << "[🟢 LISTENING]  (barge-in armed, threshold="
                          << cfg::BARGE_IN_THRESHOLD << ")\n";
                bool barged_in = false;
                while (tts.is_playing() && g_running) {
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(80));
                    auto buf = audio.drain();
                    if (buf.empty()) continue;
                    float energy = demascas::AudioEngine::rms(
                        buf.data(), buf.size());
                    if (energy >= cfg::BARGE_IN_THRESHOLD) {
                        std::cout << "[!] BARGE-IN detected (energy="
                                  << energy << ")\n";
                        tts.interrupt();
                        barged_in = true;
                        break;
                    }
                }
                if (!barged_in) {
                    // TTS finished naturally — short cooldown to let
                    // speaker residual ring out, then flush mic buffer.
                    tts.play_sound("Tink");
                    last_tts_done = clk::now();
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(400));
                    audio.drain();
                }
                // If barged in, skip cooldown — the user is already
                // speaking and we don't want to lose their words.
            }

            std::cout << "[🟢 LISTENING]  (timeout "
                      << cfg::FOLLOWUP_TIMEOUT << " s)\n";

            auto utterance = audio.capture_utterance(
                cfg::FOLLOWUP_TIMEOUT, cfg::SILENCE_DURATION);

            if (utterance.empty()) {
                std::cout << "[*] No speech detected — going back to sleep.\n";
                tts.enqueue("Standing by.");
                tts.wait_done();
                last_tts_done = clk::now();
                audio.drain();  // flush any residual TTS echo
                llm.reset();
                consecutive_blank = 0;
                state = State::SLEEPING;
                break;
            }

            current_command = whisper.transcribe_command(utterance);

            // ── Normalize transcription artifacts ─────────
            // Fixes: "S, I, H" → "SIH", "2, 0, 2, 5" → "2025",
            //        "SIH 2025 PPT" → "SIH 2025 PPT [FILE]"
            if (!current_command.empty()) {
                std::string normalized = normalize_transcription(current_command);
                if (normalized != current_command) {
                    std::cout << "[📝 NORMALIZED] \"" << current_command
                              << "\" → \"" << normalized << "\"\n";
                    current_command = normalized;
                }
            }

            // ── Filter whisper hallucinations ──────────────────
            if (current_command.empty() || is_hallucination(current_command)) {
                if (!current_command.empty()) {
                    std::cout << "[*] Hallucination filtered: \""
                              << current_command << "\"\n";
                }
                ++consecutive_blank;
                std::cout << "[*] Blank " << consecutive_blank << "/"
                          << cfg::MAX_CONSECUTIVE_BLANKS << "\n";
                if (consecutive_blank >= cfg::MAX_CONSECUTIVE_BLANKS) {
                    std::cout << "[*] Too many blanks — going back to sleep.\n";
                    tts.enqueue("Standing by.");
                    tts.wait_done();
                    last_tts_done = clk::now();
                    audio.drain();  // flush any residual TTS echo
                    llm.reset();
                    consecutive_blank = 0;
                    state = State::SLEEPING;
                }
                // else: stay in LISTENING for another try
                break;
            }

            // ── Wake-word echo in LISTENING state ──────────
            // If the user (or their friend) says the wake word while
            // we're already listening, acknowledge it instead of
            // processing it as a command.
            if (is_wake_phrase(current_command)) {
                std::cout << "[*] Wake word in LISTENING state — acknowledging.\n";
                tts.enqueue("I'm here. What can I do for you?");
                break;  // stay in LISTENING
            }

            // ── Dismiss detection (ChatGPT-style) ─────────
            if (is_dismiss(current_command)) {
                std::cout << "[*] Dismiss → \"" << current_command << "\"\n";
                tts.interrupt();
                tts.enqueue("Standing by.");
                tts.wait_done();
                last_tts_done = clk::now();
                audio.drain();  // flush any residual TTS echo
                llm.reset();
                consecutive_blank = 0;
                state = State::SLEEPING;
                break;
            }

            // Real command — reset blank counter
            consecutive_blank = 0;
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

            // ── Continuation detection ─────────────────────────
            // If we have pending file choices from a previous turn,
            // and the user's new command looks like a selection
            // (e.g. "the first one", "open SIH", "number 2"),
            // route directly to CLARIFYING state.
            if (!pending_file_choices.empty()) {
                std::string cl = to_lower(current_command);
                // Check if it's a selection-like utterance
                static const char* SELECTION_WORDS[] = {
                    "first", "second", "third", "fourth", "fifth",
                    "1st", "2nd", "3rd", "4th", "5th",
                    "one", "two", "three", "four", "five",
                    "number", "that one", "the one", "open it",
                    "yes", "last",
                };
                bool is_selection = false;
                for (auto& sw : SELECTION_WORDS) {
                    if (cl.find(sw) != std::string::npos) {
                        is_selection = true;
                        break;
                    }
                }
                // Also check if user said a partial filename matching any choice
                if (!is_selection) {
                    for (auto& [fn, fp] : pending_file_choices) {
                        std::string fn_lower = to_lower(fn);
                        // Check if any significant word overlaps
                        std::istringstream words(cl);
                        std::string word;
                        while (words >> word) {
                            if (word.size() >= 3 && fn_lower.find(word) != std::string::npos) {
                                is_selection = true;
                                break;
                            }
                        }
                        if (is_selection) break;
                    }
                }
                if (is_selection) {
                    std::cout << "[🔄 CONTINUATION] Routing to CLARIFYING for selection.\n";
                    state = State::CLARIFYING;
                    // Re-inject the user's selection as if we just heard it in CLARIFYING.
                    // We'll create a pseudo-utterance by storing the command and letting
                    // CLARIFYING handle it. But CLARIFYING expects audio — we need to
                    // directly resolve here instead.
                    // Match using the same logic as CLARIFYING
                    std::string sel_lower = cl;
                    std::string chosen_path, chosen_name;

                    // Ordinal matching
                    {
                        static const std::vector<std::pair<std::string, int>> ORDINALS = {
                            {"first", 0}, {"1st", 0}, {"one", 0}, {"number 1", 0},
                            {"second", 1}, {"2nd", 1}, {"two", 1}, {"number 2", 1},
                            {"third", 2}, {"3rd", 2}, {"three", 2}, {"number 3", 2},
                            {"fourth", 3}, {"4th", 3}, {"four", 3},
                            {"fifth", 4}, {"5th", 4}, {"five", 4},
                            {"last", -1},
                        };
                        for (auto& [word, idx] : ORDINALS) {
                            if (sel_lower.find(word) != std::string::npos) {
                                int actual = (idx == -1)
                                    ? static_cast<int>(pending_file_choices.size()) - 1
                                    : idx;
                                if (actual >= 0 && actual < static_cast<int>(pending_file_choices.size())) {
                                    chosen_name = pending_file_choices[actual].first;
                                    chosen_path = pending_file_choices[actual].second;
                                }
                                break;
                            }
                        }
                    }
                    // Fuzzy name match
                    if (chosen_path.empty()) {
                        int best_score = 0;
                        for (auto& [fn, fp] : pending_file_choices) {
                            std::string fn_lower = to_lower(fn);
                            int score = 0;
                            std::istringstream words(sel_lower);
                            std::string word;
                            while (words >> word) {
                                if (word.size() >= 2 && fn_lower.find(word) != std::string::npos)
                                    score += static_cast<int>(word.size());
                            }
                            if (score > best_score) {
                                best_score = score;
                                chosen_name = fn;
                                chosen_path = fp;
                            }
                        }
                        if (best_score < 3) { chosen_path.clear(); chosen_name.clear(); }
                    }

                    if (!chosen_path.empty()) {
                        std::cout << "[🔄 CONTINUATION] Selected: " << chosen_name << "\n";
                        auto result = bridge.execute("open_file", {{"file_path", chosen_path}});
                        std::cout << "[TOOL RESULT] " << result << "\n";
                        on_sentence("Opening " + chosen_name + ".");
                    } else {
                        tts.enqueue("I couldn't match that. Let me process your full command.");
                        tts.wait_done();
                        last_tts_done = clk::now();
                        // Clear choices and fall through to normal processing
                        pending_file_choices.clear();
                        clarifying_goal.clear();
                        // Don't break — continue with normal PROCESSING below
                        goto normal_processing;
                    }
                    pending_file_choices.clear();
                    clarifying_goal.clear();
                    state = State::LISTENING;
                    break;
                }
                // Not a selection → clear pending choices, process normally
                pending_file_choices.clear();
                clarifying_goal.clear();
            }
            normal_processing:

            // PATH 1: Shortcut fast-path (< 1 µs, bypasses LLM entirely)
            auto shortcut = shortcuts.match(current_command);
            if (shortcut) {
                path_taken = "shortcut";
                std::cout << "[⚡ SHORTCUT] "
                          << current_command << " → " << *shortcut << "\n";
                bridge.execute("press_keyboard_shortcut",
                               {{"keys", *shortcut}});
                reply = llm.fast_chat_streaming(
                    current_command, on_sentence, cancel_llm);
            }
            // PATH 1.5: URL detection — open websites deterministically
            //   Bypasses the LLM for reliability.  Qwen 3B often picks
            //   the wrong tool (open_application instead of open_url).
            //   Guard: skip if user is ASKING ABOUT a URL, not requesting navigation.
            else if ([&]() {
                // Check if this is a question ABOUT a URL, not a navigation request
                std::string cl = to_lower(current_command);
                static const char* QUESTION_ABOUT_URL[] = {
                    "why did you open", "why did you go to", "why are you on",
                    "what is", "what's", "close", "stop", "don't open",
                    "i didn't want", "i didn't ask", "go back",
                    "why did you", "i said", "that's wrong",
                    "why is", "who told you",
                };
                for (auto& q : QUESTION_ABOUT_URL) {
                    if (cl.find(q) != std::string::npos) return false;
                }
                return shortcuts.extract_url(current_command).has_value();
            }()) {
                auto url_match = shortcuts.extract_url(current_command);
                path_taken = "url";
                std::cout << "[🌐 URL PATH] " << url_match->url;
                if (!url_match->browser.empty())
                    std::cout << " (" << url_match->browser << ")";
                std::cout << "\n";

                nlohmann::json url_args = {{"url", url_match->url}};
                if (!url_match->browser.empty())
                    url_args["browser"] = url_match->browser;

                auto result = bridge.execute("open_url", url_args);
                std::cout << "[TOOL RESULT] " << result << "\n";

                // Concise spoken confirmation
                std::string domain = url_match->url;
                if (domain.rfind("https://", 0) == 0)
                    domain = domain.substr(8);
                std::string confirm = "Opening " + domain;
                if (!url_match->browser.empty())
                    confirm += " in " + url_match->browser;
                confirm += ".";
                on_sentence(confirm);
                reply = confirm;
            }
            // PATH 1.6: App open — deterministic, bypasses LLM
            //   "open Safari", "launch Chrome", etc. → instant & reliable.
            else if (auto app_open = shortcuts.extract_app_open(current_command)) {
                path_taken = "app_open";
                std::cout << "[🚀 APP OPEN] " << app_open->app_name << "\n";

                auto result = bridge.execute("open_application",
                                              {{"app_name", app_open->app_name}});
                std::cout << "[TOOL RESULT] " << result << "\n";

                std::string confirm = "Opening " + app_open->app_name + ".";
                on_sentence(confirm);
                reply = confirm;
            }
            // PATH 1.7: App close — deterministic, bypasses LLM
            //   "close Safari", "quit Chrome" → instant.
            //   "close window"/"close tab" still go through shortcut map.
            else if (auto app_close = shortcuts.extract_app_close(current_command)) {
                path_taken = "app_close";
                std::cout << "[🛑 APP CLOSE] " << app_close->app_name << "\n";

                auto result = bridge.execute("quit_application",
                                              {{"app_name", app_close->app_name}});
                std::cout << "[TOOL RESULT] " << result << "\n";

                std::string confirm = "Closing " + app_close->app_name + ".";
                on_sentence(confirm);
                reply = confirm;
            }
            // PATH 1.8: Macro fast-path — pre-built multi-step sequences
            //   "open gmail", "open youtube", "google X" → instant, no LLM.
            else if (auto macro = demascas::try_macro(current_command)) {
                path_taken = "macro";
                std::cout << "[📦 MACRO] " << macro->description << "\n";

                for (auto& step : macro->steps) {
                    try {
                        auto args = nlohmann::json::parse(step.args_json);
                        auto result = bridge.execute(step.tool, args);
                        std::cout << "[MACRO STEP] " << step.tool << " → " << result << "\n";
                    } catch (const std::exception& e) {
                        std::cerr << "[!] Macro step failed: " << e.what() << "\n";
                    }
                    if (step.delay_ms > 0)
                        std::this_thread::sleep_for(std::chrono::milliseconds(step.delay_ms));
                }
                on_sentence(macro->description);
                reply = macro->description;
            }
            // PATH 2: Simple question (streaming fast LLM, no tool schemas)
            else if (shortcuts.is_simple_question(current_command)) {
                path_taken = "fast";
                std::cout << "[⚡ FAST PATH] Simple question — streaming.\n";
                reply = llm.fast_chat_streaming(
                    current_command, on_sentence, cancel_llm);
            }
            // PATH 2.5: Multi-step planner — decompose and execute sequentially
            //   "search for cats on youtube" → open Safari → go to youtube.com → search
            else if (llm.is_multi_step_command(current_command)) {
                path_taken = "planner";
                std::cout << "[📋 PLANNER] Multi-step command detected.\n";

                std::string learned_ctx;
                try {
                    learned_ctx = bridge.fetch_learned_context(current_command);
                } catch (...) {}

                // Decompose into structured steps (JSON format)
                auto plan = llm.plan_steps_structured(current_command, bridge, cancel_llm);

                if (plan.empty() || cancel_llm.load()) {
                    // Fallback to single tool call if planner fails
                    std::cout << "[PLANNER] No steps — falling back to tool path.\n";
                    reply = llm.chat_with_tools_streaming(
                        current_command, bridge, on_sentence, cancel_llm,
                        learned_ctx);
                } else {
                    // ── Execute with inter-step result passing ──────
                    std::map<int, std::string> step_results;
                    std::string last_reply;
                    bool had_failure = false;

                    for (size_t i = 0; i < plan.size() && !cancel_llm.load(); ++i) {
                        auto& step = plan[i];
                        std::string tool = step.value("tool", "");
                        auto args = step.value("args", nlohmann::json::object());

                        // Substitute {result_of_step_N} tokens in args
                        std::string args_str = args.dump();
                        for (auto& [step_num, result_val] : step_results) {
                            std::string token = "{result_of_step_" + std::to_string(step_num) + "}";
                            size_t pos;
                            while ((pos = args_str.find(token)) != std::string::npos) {
                                args_str.replace(pos, token.size(), result_val);
                            }
                        }

                        // Re-parse substituted args
                        nlohmann::json resolved_args;
                        try {
                            resolved_args = nlohmann::json::parse(args_str);
                        } catch (...) {
                            resolved_args = args;
                        }

                        std::cout << "[PLANNER STEP " << (i + 1) << "/"
                                  << plan.size() << "] " << tool
                                  << "(" << resolved_args.dump() << ")\n";

                        auto result = bridge.execute(tool, resolved_args);
                        std::cout << "[STEP RESULT] " << result << "\n";

                        // Extract first_match_path from find_file results for chaining
                        std::string stored_result = result;
                        if (tool == "find_file") {
                            std::string extracted = extract_first_file_path(result);
                            if (!extracted.empty()) {
                                stored_result = extracted;
                                std::cout << "[PLANNER] Extracted path: " << stored_result << "\n";
                            } else {
                                // Check if count > 1 (multiple matches — need clarification)
                                bool has_matches = result.find("\"found\": true") != std::string::npos
                                                || result.find("\"found\":true") != std::string::npos
                                                || result.find("'found': True") != std::string::npos;
                                if (!has_matches) {
                                    // No matches at all — stop and report
                                    std::string msg = "No files found.";
                                    // Try to get the message from the result
                                    try {
                                        auto rj = nlohmann::json::parse(result);
                                        msg = rj.value("message", msg);
                                    } catch (...) {
                                        // Try string extraction
                                        auto mpos = result.find("'message':");
                                        if (mpos == std::string::npos) mpos = result.find("\"message\":");
                                        if (mpos != std::string::npos) {
                                            mpos += 10;
                                            while (mpos < result.size() && (result[mpos] == ' ' || result[mpos] == '\'' || result[mpos] == '"'))
                                                ++mpos;
                                            char q = '\'';
                                            auto mend = result.find(q, mpos);
                                            if (mend != std::string::npos)
                                                msg = result.substr(mpos, mend - mpos);
                                        }
                                    }
                                    on_sentence(msg);
                                    last_reply = msg;
                                    had_failure = true;
                                    break;
                                } else {
                                    // found=True but no first_match_path => multiple matches, need clarification
                                    // Extract count
                                    int result_count = 0;
                                    try {
                                        auto rj = nlohmann::json::parse(result);
                                        result_count = rj.value("count", 0);
                                    } catch (...) {}
                                    
                                    if (result_count > 1) {
                                        // Enter CLARIFYING state: speak choices, wait for user
                                        std::string msg;
                                        try {
                                            auto rj = nlohmann::json::parse(result);
                                            msg = rj.value("message", "I found multiple files. Which one did you mean?");
                                            // Extract file choices for CLARIFYING state
                                            pending_file_choices.clear();
                                            clarifying_goal = current_command;
                                            if (rj.contains("matches") && rj["matches"].is_array()) {
                                                for (auto& m : rj["matches"]) {
                                                    std::string fn = m.value("name", "?");
                                                    std::string fp = m.value("path", "?");
                                                    pending_file_choices.push_back({fn, fp});
                                                }
                                            }
                                        } catch (...) {
                                            msg = "I found multiple files. Which one did you mean?";
                                        }
                                        on_sentence(msg);
                                        last_reply = msg;
                                        if (!pending_file_choices.empty()) {
                                            state = State::CLARIFYING;
                                            reply = last_reply;
                                            // Log interaction
                                            try {
                                                bridge.log_interaction(
                                                    current_command, "planner",
                                                    llm.last_tools_used(),
                                                    reply
                                                );
                                            } catch (...) {}
                                            break;  // exit planner loop, go to CLARIFYING
                                        }
                                        had_failure = true;
                                        break;
                                    }
                                }
                            }
                        }

                        // Check for tool errors
                        if (result.find("[-]") == 0 || result.find("\"success\": false") != std::string::npos ||
                            result.find("\"success\":false") != std::string::npos) {
                            std::cout << "[PLANNER] Step " << (i + 1) << " failed — stopping.\n";
                            on_sentence("I ran into an issue. " + result.substr(0, 150));
                            last_reply = result;
                            had_failure = true;
                            break;
                        }

                        step_results[static_cast<int>(i + 1)] = stored_result;
                    }

                    if (!had_failure) {
                        on_sentence("Done.");
                        last_reply = "Done.";
                    }
                    reply = last_reply.empty() ? "Done." : last_reply;

                    // If planner transitioned to CLARIFYING, exit PROCESSING now
                    if (state == State::CLARIFYING) break;
                }
            }
            // PATH 4: Deep intent — communication, research, complex tasks
            //   3-phase: Understand → Plan → Execute
            //   "reply to that email", "compose email to John", "research X"
            else if (llm.is_deep_intent_command(current_command)) {
                path_taken = "deep_intent";
                std::cout << "[🧠 DEEP INTENT] Complex task detected.\n";

                reply = llm.deep_intent_streaming(
                    current_command, bridge, on_sentence, cancel_llm);

                // Check if deep intent returned a confirmation request
                // (pending_confirmation status from confirm_and_execute tool)
                try {
                    auto rj = nlohmann::json::parse(reply);
                    if (rj.contains("status") &&
                        rj.value("status", "") == "pending_confirmation") {
                        pending_tool = rj.value("pending_tool", "");
                        pending_args = rj.value("pending_args", nlohmann::json::object());
                        pending_description = rj.value("confirm_speech",
                            "Should I proceed?");
                        // The confirmation speech was already spoken by on_sentence
                        state = State::CONFIRMING;
                        break;  // exit PROCESSING, enter CONFIRMING
                    }
                } catch (...) {
                    // reply wasn't JSON — continue normally
                }
            }
            // PATH 3: Full streaming tool-calling ReAct loop
            else {
                path_taken = "tool";
                std::cout << "[🔧 TOOL PATH] Full processing — streaming.\n";

                std::string learned_ctx;
                try {
                    learned_ctx = bridge.fetch_learned_context(current_command);
                } catch (...) {}

                reply = llm.chat_with_tools_streaming(
                    current_command, bridge, on_sentence, cancel_llm,
                    learned_ctx);
            }

            // Stop barge-in monitor
            // (no-op — barge-in removed; laptop speakers bleed
            //  into mic at 0.15–0.30 RMS, making real-time AEC
            //  impossible without hardware echo cancellation)

            // TTS will continue playing into LISTENING state.
            // LISTENING waits for it to finish before capturing.

            // If we transitioned to CLARIFYING or CONFIRMING, skip the
            // normal LISTENING transition — those states handle themselves.
            if (state == State::CLARIFYING || state == State::CONFIRMING) {
                break;
            }

            if (!reply.empty()) {
                std::cout << "[DEMASCAS]: " << reply << "\n";
            }

            // Log interaction for learning (fire-and-forget)
            try {
                bridge.log_interaction(
                    current_command,
                    path_taken,
                    llm.last_tools_used(),
                    reply.empty() ? "Done." : reply
                );
            } catch (...) {}

            // Persist session for crash recovery
            try { llm.save_session(); } catch (...) {}

            // Stay awake for a follow-up command
            state = State::LISTENING;
            break;
        }

        // ────────────────────────────────────────────────────────
        // CONFIRMING — wait for user yes/no on a pending action
        //   Enters when a tool returns {status: "pending_confirmation"}.
        //   The confirmation speech was already played by PROCESSING.
        //   Now we listen for a brief yes/no response.
        // ────────────────────────────────────────────────────────
        case State::CONFIRMING: {
            std::cout << "[⚠️ CONFIRMING] Waiting for user approval...\n";
            std::cout << "[⚠️ CONFIRMING] Pending: " << pending_tool
                      << " — " << pending_description << "\n";

            // Wait for TTS to finish playing the confirmation question
            if (tts.is_playing()) {
                while (tts.is_playing() && g_running)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                tts.play_sound("Tink");
                last_tts_done = clk::now();
                std::this_thread::sleep_for(std::chrono::milliseconds(400));
                audio.drain();
            }

            // Listen for yes/no (shorter timeout than normal)
            auto utterance = audio.capture_utterance(8.0f, cfg::SILENCE_DURATION);

            if (utterance.empty()) {
                std::cout << "[⚠️ CONFIRMING] No response — cancelling.\n";
                tts.enqueue("Cancelled.");
                tts.wait_done();
                last_tts_done = clk::now();
                pending_tool.clear();
                pending_args = {};
                state = State::LISTENING;
                break;
            }

            std::string confirmation = whisper.transcribe_command(utterance);
            std::string conf_lower = to_lower(confirmation);
            std::cout << "[⚠️ CONFIRMING] User said: \"" << confirmation << "\"\n";

            // Check for affirmative
            bool confirmed = false;
            static const char* YES_WORDS[] = {
                "yes", "yeah", "yep", "yup", "sure",
                "go ahead", "do it", "proceed", "confirm",
                "okay", "ok", "alright", "affirmative",
                "absolutely", "of course", "please",
                "send it", "send that", "go for it",
            };
            for (auto& w : YES_WORDS) {
                if (conf_lower.find(w) != std::string::npos) {
                    confirmed = true;
                    break;
                }
            }

            if (confirmed && !pending_tool.empty()) {
                std::cout << "[⚠️ CONFIRMING] APPROVED — executing "
                          << pending_tool << "\n";
                tts.enqueue("Executing now.");

                auto result = bridge.execute(pending_tool, pending_args);
                std::cout << "[TOOL RESULT] " << result << "\n";

                // Brief confirmation
                if (result.find("[-]") == std::string::npos) {
                    on_sentence("Done.");
                } else {
                    on_sentence("There was an issue. " + result.substr(0, 100));
                }
            } else {
                std::cout << "[⚠️ CONFIRMING] DENIED — cancelling.\n";
                tts.enqueue("Cancelled.");
            }

            tts.wait_done();
            last_tts_done = clk::now();

            // Clean up pending action
            pending_tool.clear();
            pending_args = {};
            pending_description.clear();

            state = State::LISTENING;
            break;
        }

        // ────────────────────────────────────────────────────────
        // CLARIFYING — multi-match file disambiguation
        //   Enters when find_file returns >1 match during planner.
        //   Choices were already spoken by PROCESSING.
        //   Now we listen for the user's selection and open it.
        // ────────────────────────────────────────────────────────
        case State::CLARIFYING: {
            std::cout << "[❓ CLARIFYING] Waiting for user selection ("
                      << pending_file_choices.size() << " choices)...\n";
            for (size_t ci = 0; ci < pending_file_choices.size(); ++ci) {
                std::cout << "  [" << (ci + 1) << "] "
                          << pending_file_choices[ci].first << " → "
                          << pending_file_choices[ci].second << "\n";
            }

            // Wait for TTS to finish
            if (tts.is_playing()) {
                while (tts.is_playing() && g_running)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                tts.play_sound("Tink");
                last_tts_done = clk::now();
                std::this_thread::sleep_for(std::chrono::milliseconds(400));
                audio.drain();
            }

            // Listen for user selection (shorter timeout)
            auto utterance = audio.capture_utterance(10.0f, cfg::SILENCE_DURATION);

            if (utterance.empty()) {
                std::cout << "[❓ CLARIFYING] No response — cancelling.\n";
                tts.enqueue("Cancelled.");
                tts.wait_done();
                last_tts_done = clk::now();
                pending_file_choices.clear();
                clarifying_goal.clear();
                state = State::LISTENING;
                break;
            }

            std::string selection = whisper.transcribe_command(utterance);
            std::string sel_lower = to_lower(selection);
            std::cout << "[❓ CLARIFYING] User said: \"" << selection << "\"\n";

            // ── Match strategy ──────────────────────────────────
            //  1. Ordinal: "the first one", "second", "number 2"
            //  2. Fuzzy name match: user says part of the filename

            std::string chosen_path;
            std::string chosen_name;

            // Strategy 1: Ordinal matching
            {
                static const std::vector<std::pair<std::string, int>> ORDINALS = {
                    {"first", 0}, {"1st", 0}, {"one", 0}, {"number 1", 0}, {"number one", 0},
                    {"second", 1}, {"2nd", 1}, {"two", 1}, {"number 2", 1}, {"number two", 1},
                    {"third", 2}, {"3rd", 2}, {"three", 2}, {"number 3", 2},
                    {"fourth", 3}, {"4th", 3}, {"four", 3},
                    {"fifth", 4}, {"5th", 4}, {"five", 4},
                    {"last", -1},
                };
                for (auto& [word, idx] : ORDINALS) {
                    if (sel_lower.find(word) != std::string::npos) {
                        int actual = (idx == -1)
                            ? static_cast<int>(pending_file_choices.size()) - 1
                            : idx;
                        if (actual >= 0 && actual < static_cast<int>(pending_file_choices.size())) {
                            chosen_name = pending_file_choices[actual].first;
                            chosen_path = pending_file_choices[actual].second;
                        }
                        break;
                    }
                }
            }

            // Strategy 2: Fuzzy name match
            if (chosen_path.empty()) {
                int best_score = 0;
                for (auto& [fn, fp] : pending_file_choices) {
                    std::string fn_lower = to_lower(fn);
                    int score = 0;
                    // Count how many words from user's selection appear in the filename
                    std::istringstream words(sel_lower);
                    std::string word;
                    while (words >> word) {
                        if (word.size() >= 2 && fn_lower.find(word) != std::string::npos) {
                            score += static_cast<int>(word.size());
                        }
                    }
                    if (score > best_score) {
                        best_score = score;
                        chosen_name = fn;
                        chosen_path = fp;
                    }
                }
                // Require minimum match quality
                if (best_score < 3) {
                    chosen_path.clear();
                    chosen_name.clear();
                }
            }

            if (!chosen_path.empty()) {
                std::cout << "[❓ CLARIFYING] Selected: " << chosen_name
                          << " → " << chosen_path << "\n";
                auto result = bridge.execute("open_file", {{"file_path", chosen_path}});
                std::cout << "[TOOL RESULT] " << result << "\n";
                std::string msg = "Opening " + chosen_name + ".";
                on_sentence(msg);
            } else {
                std::cout << "[❓ CLARIFYING] No match — cancelling.\n";
                tts.enqueue("I couldn't match that to any of the files. Please try again.");
            }

            tts.wait_done();
            last_tts_done = clk::now();

            // Clean up
            pending_file_choices.clear();
            clarifying_goal.clear();

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
