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
#include <regex>
#include <set>
#include <sstream>
#include <thread>
#include <vector>

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

/// Lowercase, drop apostrophes ("that's" → "thats"), turn every other
/// non-alphanumeric run into a single space.
static std::string simplify(const std::string& text) {
    std::string out;
    for (char c : to_lower(text)) {
        if (std::isalnum(static_cast<unsigned char>(c))) out += c;
        else if (c == '\'') continue;
        else if (!out.empty() && out.back() != ' ') out += ' ';
    }
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out;
}

/// Whole-word / whole-phrase test on simplified text ("one" ∉ "someone").
static bool has_word(const std::string& simplified, const std::string& phrase) {
    return (" " + simplified + " ").find(" " + phrase + " ") != std::string::npos;
}

/// Remove filler words from simplified text.
static std::string strip_fillers(const std::string& simplified,
                                 const std::set<std::string>& fillers) {
    std::istringstream iss(simplified);
    std::string w, out;
    while (iss >> w) {
        if (fillers.count(w)) continue;
        if (!out.empty()) out += ' ';
        out += w;
    }
    return out;
}

/// Spoken reply to a yes/no question.  Any negation wins ("I'm not sure",
/// "no, please don't", "don't do it" are all NO); unclear answers are
/// treated as NO by the caller — destructive actions need a clear yes.
enum class Answer { YES, NO, UNCLEAR };

static Answer classify_answer(const std::string& text) {
    std::string s = simplify(text);
    static const char* NEG[] = {
        "no", "nope", "nah", "not", "dont", "do not", "cancel", "stop",
        "never", "wait", "hold on", "negative", "abort", "nothing", "skip",
        "leave it", "forget it", "wrong",
    };
    for (auto& n : NEG)
        if (has_word(s, n)) return Answer::NO;
    static const char* YES[] = {
        "yes", "yeah", "yep", "yup", "sure", "go ahead", "do it", "proceed",
        "confirm", "confirmed", "okay", "ok", "alright", "affirmative",
        "absolutely", "of course", "definitely", "please do", "go for it",
        "send it", "delete it", "sounds good", "correct", "right",
    };
    for (auto& y : YES)
        if (has_word(s, y)) return Answer::YES;
    return Answer::UNCLEAR;
}

/// Match a spoken selection ("the second one", "number 3", "the SIH one")
/// against offered files.  Returns true and fills name/path on success.
static bool resolve_file_choice(
    const std::string& spoken,
    const std::vector<std::pair<std::string, std::string>>& choices,
    std::string& chosen_name, std::string& chosen_path) {
    chosen_name.clear();
    chosen_path.clear();
    if (choices.empty()) return false;
    std::string s = simplify(spoken);

    // Specific ordinals first; bare number words ("one") last, so
    // "the second one" means #2, not #1.
    static const std::vector<std::pair<std::string, int>> ORDINALS = {
        {"number 1", 0}, {"number one", 0}, {"number 2", 1}, {"number two", 1},
        {"number 3", 2}, {"number three", 2}, {"number 4", 3}, {"number four", 3},
        {"number 5", 4}, {"number five", 4},
        {"first", 0}, {"1st", 0}, {"second", 1}, {"2nd", 1},
        {"third", 2}, {"3rd", 2}, {"fourth", 3}, {"4th", 3},
        {"fifth", 4}, {"5th", 4}, {"last", -1},
        {"one", 0}, {"two", 1}, {"three", 2}, {"four", 3}, {"five", 4},
    };
    for (auto& [word, idx] : ORDINALS) {
        if (has_word(s, word)) {
            int actual = (idx == -1) ? static_cast<int>(choices.size()) - 1 : idx;
            if (actual >= 0 && actual < static_cast<int>(choices.size())) {
                chosen_name = choices[actual].first;
                chosen_path = choices[actual].second;
                return true;
            }
            return false;
        }
    }

    // Fuzzy name match: words of the selection that appear in a filename
    static const std::set<std::string> IGNORE = {
        "the", "a", "an", "one", "open", "file", "that", "this", "please", "it",
    };
    int best_score = 0;
    for (auto& [fn, fp] : choices) {
        std::string fn_simple = simplify(fn);
        int score = 0;
        std::istringstream words(s);
        std::string word;
        while (words >> word) {
            if (word.size() >= 2 && !IGNORE.count(word) &&
                fn_simple.find(word) != std::string::npos)
                score += static_cast<int>(word.size());
        }
        if (score > best_score) {
            best_score = score;
            chosen_name = fn;
            chosen_path = fp;
        }
    }
    if (best_score < 3) {
        chosen_name.clear();
        chosen_path.clear();
        return false;
    }
    return true;
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

/// Strict version for when we're ALREADY awake: true only if the whole
/// utterance is just the wake word (plus "hey"/"hi"), so a command like
/// "set an alarm to wake up at 7" is still processed as a command.
static bool is_wake_only(const std::string& text) {
    static const std::set<std::string> FILLERS = {
        "hey", "hi", "hello", "ok", "okay", "yo", "um", "uh", "so",
    };
    std::string s = strip_fillers(simplify(text), FILLERS);
    static const std::set<std::string> WAKE = {
        "damascus", "demascas", "demascus", "damaskus", "de mascas",
        "the mascus", "damasco", "demasco", "dimascas", "dimascus",
        "damask", "da mascas", "maskers", "the mask is", "the mask",
        "mascas", "mascus", "demas", "wake up",
    };
    return WAKE.count(s) > 0;
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

    // ── STEP 6: Targeted speech autocorrections for common domains ──
    // Applied case-insensitively WITHOUT lowercasing the whole command, so
    // names ("Albert Einstein", "Aman"), subjects and file names keep their
    // capitalization.
    {
        static const std::regex LOFI(R"(\b(?:low\s+five|low\s+fi|lo\s+fi)\b)", std::regex::icase);
        s = std::regex_replace(s, LOFI, "lofi");

        std::string sl = to_lower(s);
        if (sl.find("lms") != std::string::npos) {
            static const std::vector<std::string> bennett_variants = {
                "bennit", "benit", "bennet", "benned", "bewewd", "penit", "bennett"
            };
            bool looks_bennett = false;
            for (const auto& v : bennett_variants) {
                if (sl.find(v) != std::string::npos) { looks_bennett = true; break; }
            }
            if (looks_bennett) {
                s = std::regex_replace(s, std::regex(R"(lms\.[a-z0-9.-]+)", std::regex::icase),
                                       "lms.bennett.edu.in");
            }
            // TLD truncations from speech models
            s = std::regex_replace(s, std::regex(R"(\.edu\.n\b)", std::regex::icase), ".edu.in");
            s = std::regex_replace(s, std::regex(R"(\.edu\.i\b)", std::regex::icase), ".edu.in");
        }
    }

    return s;
}

/// Detect dismissal / stop commands — ChatGPT-style.
/// The WHOLE utterance (minus fillers) must be a dismissal, so commands
/// that merely contain "stop"/"sleep"/"done" ("open the stopwatch",
/// "play music to sleep", "is the download done") are still processed.
static bool is_dismiss(const std::string& text) {
    static const std::set<std::string> FILLERS = {
        "okay", "ok", "alright", "thanks", "thank", "you", "please",
        "demascas", "damascus", "hey", "now", "for", "just", "so", "well",
        "um", "uh", "yeah", "right", "then",
    };
    std::string s = strip_fillers(simplify(text), FILLERS);
    if (s.empty()) return false;
    static const std::set<std::string> DISMISS = {
        "stop", "stop it", "stop talking", "shut up", "be quiet", "quiet",
        "go to sleep", "go sleep", "sleep", "goodbye", "good bye", "bye",
        "bye bye", "thats all", "that is all", "thats it", "that will be all",
        "thatll be all", "never mind", "nevermind", "nah", "can stop",
        "enough", "im done", "i am done", "were done", "we are done", "done",
        "go away", "stand by", "standing by", "no", "nope", "no more",
        "nothing", "nothing else", "see ya", "see you", "later",
        "talk to you later", "thats enough",
    };
    return DISMISS.count(s) > 0;
}

// ── State machine ───────────────────────────────────────────────────

enum class State { SLEEPING, LISTENING, PROCESSING, CONFIRMING, CLARIFYING };

int main() {
    // Flush log lines immediately (stdout is block-buffered when redirected
    // to a file, which hid all daemon output in run.sh logs).
    std::cout << std::unitbuf;

    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    namespace cfg = demascas::config;

    std::cout << "\n"
              << "╔══════════════════════════════════════════════╗\n"
              << "║  DEMASCAS — Bare-Metal C++ Daemon             ║\n"
              << "║  Apple Silicon • whisper.cpp • MLX              ║\n"
              << "║  Plug-in local LLM • Kokoro TTS • streaming    ║\n"
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

    // ── 3. Plugged-in LLM (no model ships with DEMASCAS) ───────
    auto llm_cfg = bridge.fetch_llm_config();
    demascas::LLMClient llm;
    if (llm_cfg.configured) {
        llm.configure(llm_cfg.chat_url, llm_cfg.model, llm_cfg.context_tokens);
        std::cout << "[✓] LLM: " << llm_cfg.model << " via " << llm_cfg.runtime
                  << " (context " << llm_cfg.context_tokens << " tokens)\n";
    }
    llm.prewarm(bridge);   // health check now; KV-cache/Metal warm-up in the background
    llm.load_session();  // restore context from previous session

    // ── 4. Init shortcut map (C++ fast-path, no LLM needed) ────
    demascas::ShortcutMap shortcuts;

    // ── 5. Init TTS ─────────────────────────────────────────────
    demascas::TTS tts(llm_cfg.tts_threads);

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
    whisper.set_threads(llm_cfg.whisper_threads);
    whisper.warm_up();  // build Metal graphs now, not on the first command

    std::cout << "\n[*] DEMASCAS is ready.  Say \"" << cfg::WAKE_WORD
              << "\" to activate!\n\n";

    // ── State machine loop ──────────────────────────────────────

    State state = State::SLEEPING;
    std::string current_command;
    std::atomic<bool> cancel_llm{false};   // set to cancel streaming
    int consecutive_blank = 0;             // hallucinations since last real command

    // ── CONFIRMING state — pending action for user approval ─────
    //    The tool server holds the action; we only keep its one-time
    //    token and resolve it with POST /confirm after the user answers.
    std::string pending_tool;
    std::string pending_token;
    std::string pending_description;

    // ── CLARIFYING state — multi-match file disambiguation ──────
    //    When find_file returns >1 match, we speak the choices and
    //    wait for the user to say which one they want.
    std::vector<std::pair<std::string, std::string>> pending_file_choices;  // {name, path}
    std::string clarifying_goal;  // what user wanted to do with the file

    using clk = std::chrono::steady_clock;
    auto last_tts_done = clk::now() - std::chrono::seconds(5); // no cooldown at start

    // Sliding wake-word window: the last WAKE_LISTEN_WINDOW seconds of audio,
    // re-checked every WAKE_HOP_SEC.  (A fixed 2 s chunk added up to 2 s of
    // delay, split "demas|cas" across chunks, and dropped the audio that
    // arrived while whisper was busy.)
    std::vector<float> wake_window;
    bool prev_hop_had_speech = false;
    const size_t wake_window_samples =
        static_cast<size_t>(cfg::WAKE_LISTEN_WINDOW * cfg::SAMPLE_RATE);
    const auto post_tts_guard = std::chrono::milliseconds(cfg::POST_TTS_GUARD_MS);

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

        // Filter plain-text tool-call leaks like: open_url_and_wait({'url': ...})
        static const std::regex TOOL_CALL_TEXT(R"(^\s*[a-z_]+\s*\(\{.*\}\)\s*$)", std::regex::icase);
        if (std::regex_search(sentence, TOOL_CALL_TEXT)) {
            std::cout << "[🔊 TTS] (filtered tool-call text) " << sentence.substr(0, 80) << "...\n";
            return;
        }

        // ── Skip low-level technical narration ──────────────
        // Phrases the LLM generates that describe internal actions
        // rather than providing useful info to the user.
        {
            std::string lower;
            lower.reserve(sentence.size());
            for (char c : sentence)
                lower += static_cast<char>(::tolower(static_cast<unsigned char>(c)));

            // Narration of internal steps — only when the sentence STARTS
            // with it, so real answers like "The keyboard shortcut for copy
            // is Command C" are still spoken.
            static const char* SKIP_PREFIXES[] = {
                "press enter", "pressed enter", "pressing ", "i pressed ",
                "clicked ", "clicking ", "i clicked ", "typed ", "typing ",
                "i typed ", "scrolled ", "scrolling ", "i scrolled ",
            };
            // AppleScript / meta chatter that is never useful to hear.
            static const char* SKIP_ANYWHERE[] = {
                "keystroke \"", "key code ", "the terminal should",
                "you can now search", "you can now browse",
                "what would you like to do next", "command you entered",
            };
            auto first = lower.find_first_not_of(" \t");
            std::string head = first == std::string::npos ? "" : lower.substr(first);
            bool skip = false;
            for (auto& phrase : SKIP_PREFIXES)
                if (head.rfind(phrase, 0) == 0) { skip = true; break; }
            if (!skip)
                for (auto& phrase : SKIP_ANYWHERE)
                    if (lower.find(phrase) != std::string::npos) { skip = true; break; }
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
                // PortAudio is reference-counted: TTS must release its
                // reference too, or Pa_Terminate() never really resets
                // CoreAudio and the "reinit" silently does nothing.
                tts.interrupt();
                tts.release_audio();
                bool ok = audio.reinit_on_wake(cfg::INPUT_DEVICE_INDEX);
                tts.reacquire_audio();
                if (!ok) std::this_thread::sleep_for(std::chrono::seconds(2));
                break;  // retry next iteration
            }

            // ── Anti-echo: skip while TTS is playing or within cooldown
            if (tts.is_playing()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                audio.drain();
                wake_window.clear();
                break;
            }
            {
                float since_tts = std::chrono::duration<float>(
                    clk::now() - last_tts_done).count();
                if (since_tts < cfg::TTS_COOLDOWN_SEC) {
                    std::this_thread::sleep_for(
                        std::chrono::milliseconds(100));
                    audio.drain();
                    wake_window.clear();
                    break;
                }
            }

            auto hop = audio.collect(cfg::WAKE_HOP_SEC);
            if (hop.empty()) {
                // No samples at all = the OS isn't delivering mic audio
                // (usually missing Microphone permission).  Say so instead
                // of sitting silently forever.
                static int empty_hops = 0;
                if (++empty_hops == 4)
                    std::cerr << "[!] The microphone is delivering no audio. Grant Microphone "
                                 "access to the app running DEMASCAS (System Settings → Privacy "
                                 "& Security → Microphone), then restart.\n";
                break;
            }

            // VAD gate — skip whisper while it's quiet (most of the time).
            float energy = demascas::AudioEngine::rms(hop.data(), hop.size());
            bool hop_has_speech = energy >= cfg::VAD_ENERGY_THRESHOLD * 0.3f;

            wake_window.insert(wake_window.end(), hop.begin(), hop.end());
            if (wake_window.size() > wake_window_samples)
                wake_window.erase(wake_window.begin(),
                                  wake_window.end() - static_cast<long>(wake_window_samples));

            // Transcribe while speech is present, plus one hop after it ends
            // so a wake word at the end of speech is still caught.
            bool should_check = hop_has_speech || prev_hop_had_speech;
            prev_hop_had_speech = hop_has_speech;
            if (!should_check) {
                static int vad_skip_count = 0;
                if (++vad_skip_count % 40 == 1)
                    std::cout << "[VAD] silence (energy=" << energy << ")\n";
                break;
            }

            auto transcript = whisper.transcribe_wake(wake_window);
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
                wake_window.clear();
                prev_hop_had_speech = false;
                tts.play_sound("Glass");
                if (!cfg::WAKE_ACK_PHRASE.empty()) {
                    tts.enqueue(cfg::WAKE_ACK_PHRASE);   // pre-synthesized → instant
                    tts.wait_done();
                }
                // Anti-echo: short guard + drain so mic doesn't hear our own voice
                last_tts_done = clk::now();
                std::this_thread::sleep_for(post_tts_guard);
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
                    std::this_thread::sleep_for(post_tts_guard);
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
            if (is_wake_only(current_command)) {
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
            std::vector<std::string> tools_used;
            bool success = true;
            cancel_llm = false;
            const std::string normalized_command = current_command;

            // ── Continuation: picking one of the files offered last turn
            if (!pending_file_choices.empty()) {
                std::string chosen_name, chosen_path;
                if (resolve_file_choice(current_command, pending_file_choices,
                                        chosen_name, chosen_path)) {
                    std::cout << "[🔄 CONTINUATION] Selected: " << chosen_name << "\n";
                    auto result = bridge.execute("open_file", {{"file_path", chosen_path}});
                    std::cout << "[TOOL RESULT] " << result << "\n";
                    bool ok = result.rfind("[-]", 0) != 0 &&
                              result.find("\"success\": false") == std::string::npos;
                    on_sentence(ok ? "Opening " + chosen_name + "."
                                   : "I couldn't open " + chosen_name + ".");
                    bridge.log_interaction(current_command, "clarify", {"open_file"},
                                           result.substr(0, 200), ok);
                    pending_file_choices.clear();
                    clarifying_goal.clear();
                    state = State::LISTENING;
                    break;
                }
                // Not a selection → forget the offer and process normally.
                pending_file_choices.clear();
                clarifying_goal.clear();
            }

            bool run_tool_path = false;

            // PATH 1: Shortcut fast-path (< 1 µs, bypasses LLM entirely)
            auto shortcut = shortcuts.match(current_command);
            if (shortcut) {
                path_taken = "shortcut";
                std::cout << "[⚡ SHORTCUT] "
                          << current_command << " → " << *shortcut << "\n";
                auto r = bridge.execute("press_keyboard_shortcut", {{"keys", *shortcut}});
                success = r.rfind("[-]", 0) != 0;
                tools_used = {"press_keyboard_shortcut"};
                reply = success ? "Done." : "That shortcut didn't work.";
                on_sentence(reply);
            }
            // PATH 2.5: Domain Agent (specialized, multi-step, memory-aware)
            else if (demascas::is_agent_command(normalized_command)) {
                path_taken = "agent";
                std::string context_json = "{}";
                try {
                    auto ctx = bridge.fetch_active_context();
                    if (!ctx.empty()) context_json = ctx;
                } catch (...) {}

                auto ar = bridge.execute_agent(normalized_command, context_json);

                if (ar.unavailable && ar.summary.empty()) {
                    // Agent system not loaded / unreachable — don't give up,
                    // the general tool loop can still handle the command.
                    std::cout << "[AGENT] Unavailable (" << ar.error
                              << ") — falling back to the tool path.\n";
                    run_tool_path = true;
                } else {
                    success = ar.success;
                    tools_used = ar.tool_calls;

                    if (!ar.confirm_token.empty()) {
                        // A destructive action is waiting for a yes/no.
                        pending_token = ar.confirm_token;
                        pending_tool = ar.pending_tool;
                        pending_description = ar.confirm_speech;
                        reply = ar.confirm_speech;
                        state = State::CONFIRMING;
                    } else if (!ar.summary.empty()) {
                        // TTS splits sentences itself (URL/decimal-safe); the
                        // old per-'.' split read "https://www." aloud.
                        on_sentence(ar.summary);
                        reply = ar.summary;
                    } else if (!ar.error.empty()) {
                        reply = "I ran into an issue: " + ar.error.substr(0, 80);
                        on_sentence(reply);
                        success = false;
                    }

                    if (state != State::CONFIRMING && ar.file_choices.size() > 1) {
                        // The agent's summary already listed the files.
                        pending_file_choices = ar.file_choices;
                        state = State::CLARIFYING;
                    }
                }
            }
            // PATH 2: Simple question (streaming fast LLM, no tool schemas)
            else if (shortcuts.is_simple_question(current_command)) {
                path_taken = "fast";
                std::cout << "[⚡ FAST PATH] Simple question — streaming.\n";
                reply = llm.fast_chat_streaming(
                    current_command, bridge, on_sentence, cancel_llm);
                success = llm.last_success();
                // The fast path has no tools.  Its prompt tells the model to
                // say "Let me handle that for you" when an action is needed —
                // actually re-route those to the tool path.
                if (success && contains(reply, "handle that for you")) {
                    std::cout << "[⚡ FAST PATH] Model asked for tools — re-routing.\n";
                    run_tool_path = true;
                }
            } else {
                run_tool_path = true;
            }

            // PATH 3: Tool ReAct (default fallback for everything else)
            if (run_tool_path) {
                path_taken = "tool";
                std::cout << "[🔧 TOOL PATH] Full processing — streaming.\n";

                std::string learned_ctx;
                try {
                    learned_ctx = bridge.fetch_learned_context(current_command);
                } catch (...) {}

                reply = llm.chat_with_tools_streaming(
                    current_command, bridge, on_sentence, cancel_llm,
                    learned_ctx);
                tools_used = llm.last_tools_used();
                success = llm.last_success();

                if (llm.has_pending_confirmation()) {
                    auto pc = llm.take_pending_confirmation();
                    pending_token = pc.token;
                    pending_tool = pc.tool;
                    pending_description = pc.speech;
                    state = State::CONFIRMING;
                } else {
                    auto choices = llm.take_file_choices();
                    if (choices.size() > 1) {
                        // "I found N files: … Which one?" was already spoken.
                        pending_file_choices = std::move(choices);
                        state = State::CLARIFYING;
                    }
                }
            }

            if (!reply.empty()) {
                std::cout << "[DEMASCAS]: " << reply << "\n";
            }

            // Log once, with this command's own tools and real outcome.
            try {
                bridge.log_interaction(
                    current_command, path_taken, tools_used,
                    reply.empty() ? "Done." : reply, success);
            } catch (...) {}

            // Persist session for crash recovery
            try { llm.save_session(); } catch (...) {}

            // Stay awake for a follow-up (CONFIRMING / CLARIFYING handle
            // themselves).
            if (state == State::PROCESSING) state = State::LISTENING;
            break;
        }

        // ────────────────────────────────────────────────────────
        // CONFIRMING — ask, then wait for the user's yes/no
        //   The tool server holds the pending action; we resolve it with
        //   its one-time token.  Anything but a clear "yes" cancels.
        // ────────────────────────────────────────────────────────
        case State::CONFIRMING: {
            std::cout << "[⚠️ CONFIRMING] Pending: " << pending_tool
                      << " — " << pending_description << "\n";

            if (pending_token.empty()) {
                // Should not happen — nothing to approve.
                pending_tool.clear();
                pending_description.clear();
                state = State::LISTENING;
                break;
            }

            // Ask the question (it has not been spoken yet).
            tts.enqueue(pending_description.empty() ? "Should I go ahead?"
                                                    : pending_description);
            while (tts.is_playing() && g_running)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            tts.play_sound("Tink");
            last_tts_done = clk::now();
            std::this_thread::sleep_for(post_tts_guard);
            audio.drain();

            // Listen for yes/no (shorter timeout than normal)
            auto utterance = audio.capture_utterance(8.0f, cfg::SILENCE_DURATION);
            std::string confirmation =
                utterance.empty() ? "" : whisper.transcribe_command(utterance);
            std::cout << "[⚠️ CONFIRMING] User said: \"" << confirmation << "\"\n";

            Answer answer = confirmation.empty() ? Answer::UNCLEAR
                                                 : classify_answer(confirmation);
            if (answer == Answer::YES) {
                std::cout << "[⚠️ CONFIRMING] APPROVED — executing " << pending_tool << "\n";
                auto result = bridge.confirm(pending_token, true);
                std::cout << "[TOOL RESULT] " << result << "\n";
                bool failed = result.rfind("[-]", 0) == 0 ||
                              result.rfind("Failed", 0) == 0 ||
                              result.find("\"success\": false") != std::string::npos;
                if (!failed) {
                    on_sentence("Done.");
                } else {
                    on_sentence("There was an issue. " + result.substr(0, 100));
                }
                bridge.log_interaction("confirm " + pending_tool, "confirm",
                                       {pending_tool}, result.substr(0, 200), !failed);
            } else {
                bridge.confirm(pending_token, false);
                std::cout << "[⚠️ CONFIRMING] Not approved — cancelling.\n";
                tts.enqueue(answer == Answer::NO
                                ? "Okay, cancelled."
                                : "I didn't hear a clear yes, so I cancelled it.");
            }

            tts.wait_done();
            last_tts_done = clk::now();

            pending_tool.clear();
            pending_token.clear();
            pending_description.clear();

            state = State::LISTENING;
            break;
        }

        // ────────────────────────────────────────────────────────
        // CLARIFYING — multi-match file disambiguation
        //   Entered when find_file (tool path or File agent) returns
        //   several matches.  The choices were already spoken.
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
                std::this_thread::sleep_for(post_tts_guard);
                audio.drain();
            }

            // Listen for user selection (shorter timeout)
            auto utterance = audio.capture_utterance(10.0f, cfg::SILENCE_DURATION);

            if (utterance.empty()) {
                // Keep the choices: a later "the second one" still works
                // via the continuation check in PROCESSING.
                std::cout << "[❓ CLARIFYING] No response — back to listening.\n";
                state = State::LISTENING;
                break;
            }

            std::string selection = whisper.transcribe_command(utterance);
            std::cout << "[❓ CLARIFYING] User said: \"" << selection << "\"\n";

            std::string chosen_name, chosen_path;
            if (resolve_file_choice(selection, pending_file_choices,
                                    chosen_name, chosen_path)) {
                std::cout << "[❓ CLARIFYING] Selected: " << chosen_name
                          << " → " << chosen_path << "\n";
                auto result = bridge.execute("open_file", {{"file_path", chosen_path}});
                std::cout << "[TOOL RESULT] " << result << "\n";
                bool ok = result.rfind("[-]", 0) != 0 &&
                          result.find("\"success\": false") == std::string::npos;
                on_sentence(ok ? "Opening " + chosen_name + "."
                               : "I couldn't open " + chosen_name + ".");
                pending_file_choices.clear();
                clarifying_goal.clear();
                tts.wait_done();
                last_tts_done = clk::now();
                state = State::LISTENING;
            } else if (is_dismiss(selection) || classify_answer(selection) == Answer::NO) {
                tts.enqueue("Okay.");
                pending_file_choices.clear();
                clarifying_goal.clear();
                state = State::LISTENING;
            } else {
                // Not a selection — treat it as a new command.
                std::cout << "[❓ CLARIFYING] Not a selection — processing as a command.\n";
                pending_file_choices.clear();
                clarifying_goal.clear();
                current_command = normalize_transcription(selection);
                state = (current_command.empty() || is_hallucination(current_command))
                            ? State::LISTENING : State::PROCESSING;
            }
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
