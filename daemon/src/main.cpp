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

#include <deque>
#include <mutex>
#include <thread>
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
#include "vad.hpp"
#include "aec.hpp"
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

/// Whisper sometimes loops: "open discord, open, discord, open, discord, …".
/// A 1–4 word phrase repeated 3+ times in a row is cut to one copy (and a
/// dangling partial copy after it is dropped).
static std::string collapse_repeats(const std::string& text) {
    std::vector<std::string> words, keys;
    {
        std::istringstream iss(text);
        std::string w;
        while (iss >> w) {
            words.push_back(w);
            std::string k;
            for (char c : w)
                if (std::isalnum(static_cast<unsigned char>(c)))
                    k += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
            keys.push_back(k);
        }
    }
    bool changed = false;
    for (size_t n = 1; n <= 4; ++n) {
        std::vector<std::string> ow, ok;
        size_t i = 0;
        while (i < keys.size()) {
            size_t reps = 1;
            if (i + n <= keys.size()) {
                while (i + (reps + 1) * n <= keys.size() &&
                       std::equal(keys.begin() + i, keys.begin() + i + n,
                                  keys.begin() + i + reps * n))
                    ++reps;
            }
            if (reps >= 3) {
                for (size_t j = 0; j < n; ++j) { ow.push_back(words[i + j]); ok.push_back(keys[i + j]); }
                size_t next = i + reps * n;
                // drop a trailing partial copy ("…, open discord, open,")
                size_t p = 0;
                while (next + p < keys.size() && p < n && keys[next + p] == keys[i + p]) ++p;
                if (next + p == keys.size()) next = keys.size();
                i = next;
                changed = true;
            } else {
                ow.push_back(words[i]); ok.push_back(keys[i]);
                ++i;
            }
        }
        words.swap(ow);
        keys.swap(ok);
    }
    if (!changed) return text;
    std::string out;
    for (auto& w : words) { if (!out.empty()) out += ' '; out += w; }
    while (!out.empty() && (out.back() == ',' || out.back() == ' ')) out.pop_back();
    return out;
}

/// "You did nothing" / "it didn't work" — a complaint about the last task.
static bool is_complaint(const std::string& text) {
    std::string s = simplify(text);
    static const char* P[] = {
        "you did nothing", "you didnt do", "you did not do", "nothing happened",
        "it didnt work", "that didnt work", "it did not work", "not what i asked",
        "you didnt send", "you did not send", "you didnt open", "you didnt even",
        "that is not what i", "thats not what i", "you only", "still not done",
        "you havent done", "you have not done",
    };
    for (auto& p : P)
        if (has_word(s, p)) return true;
    return false;
}

/// A chat reply that CLAIMS an action it cannot perform ("Sending a hi…").
static bool claims_action(const std::string& reply) {
    std::string s = simplify(reply);
    static const char* P[] = {
        "sending a", "sending your", "sending the", "sending it", "sending that",
        "ive sent", "i have sent", "message sent", "ill send", "i will send",
        "im sending", "i am sending", "opening it", "ive opened", "i have opened",
        "im opening", "i am opening", "i just sent", "i just opened",
    };
    for (auto& p : P)
        if (has_word(s, p)) return true;
    return false;
}

static std::string normalize_transcription(const std::string& raw) {
    std::string s = collapse_repeats(raw);
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
        "stay shut", "stay quiet", "keep quiet", "be silent", "stay silent",
        "silence", "shush", "hush", "zip it", "quiet please",
    };
    if (DISMISS.count(s) > 0) return true;
    // Clear "I'm done" sentences ("that's enough for the day, I want to stop now").
    static const char* DONE[] = {
        "enough for the day", "enough for today", "done for the day", "done for today",
        "i want to stop", "stop for now", "stop for today", "thats all for now",
        "thats all for today", "good night", "goodnight", "talk to you tomorrow",
    };
    const std::string full = simplify(text);
    for (auto& p : DONE)
        if (has_word(full, p)) return true;
    return false;
}

/// Is the WHOLE utterance feedback on the previous result?
/// +1 "good job" / "perfect", -1 "that's wrong" / "not what I asked", 0 otherwise.
/// Whole-utterance only, so "open the wrong folder fix" or "good morning
/// routine" are still treated as commands.
static int feedback_polarity(const std::string& text) {
    static const std::set<std::string> FILLERS = {
        "demascas", "damascus", "hey", "oh", "um", "uh", "well", "so", "that",
        "thats", "was", "is", "it", "you", "really", "very", "a", "the", "thank",
        "thanks", "ok", "okay", "yeah", "wow", "nice", "work",
    };
    std::string s = strip_fillers(simplify(text), FILLERS);
    if (s.empty()) return 0;
    static const std::set<std::string> POS = {
        "good job", "great job", "well done", "perfect", "exactly", "correct",
        "good", "great", "awesome", "excellent", "brilliant", "spot on",
        "exactly what i wanted", "just what i wanted", "worked", "it worked",
        "good one", "nailed it", "right",
    };
    static const std::set<std::string> NEG = {
        "wrong", "not right", "not what i asked", "not what i wanted",
        "not what i meant", "you got wrong", "got wrong", "bad", "incorrect",
        "didnt", "didnt work", "not working", "you messed up", "messed up",
        "thats not it", "not it", "wrong one", "terrible", "useless",
    };
    if (POS.count(s)) return 1;
    if (NEG.count(s)) return -1;
    return 0;
}

/// "What are you doing?" — asked while a task runs.
static bool is_status_question(const std::string& text) {
    std::string s = simplify(text);
    static const char* P[] = {
        "what are you doing", "whatre you doing", "what are you working on",
        "status", "how is it going", "hows it going", "are you done",
        "are you finished", "how long", "still working", "progress",
        "whats taking so long", "what is taking so long", "is it done",
        "are you still", "where are you at",
    };
    for (auto& p : P)
        if (has_word(s, p)) return true;
    return false;
}

/// "Stop" / "cancel" — said while a task runs.
static bool is_cancel_request(const std::string& text) {
    std::string s = simplify(text);
    static const char* P[] = {
        "cancel", "cancel that", "stop that", "stop it", "stop the task",
        "abort", "never mind", "nevermind", "forget it", "dont do that",
        "do not do that", "stop working", "stop doing that", "stop",
    };
    for (auto& p : P)
        if (has_word(s, p)) return true;
    return false;
}

// ── Background task: DEMASCAS keeps listening while it works ────────
//    Long work (skills, agents, the tool loop) runs here; the main loop
//    stays in LISTENING so the user can ask questions, check progress,
//    queue more work or say "stop" — JARVIS-style.

struct TaskOutcome {
    std::string command, path, skill_id, reply;
    std::vector<std::string> tools;
    bool success = true;
    std::string confirm_token, confirm_tool, confirm_speech;
    std::vector<std::pair<std::string, std::string>> file_choices;
};

class BackgroundTask {
public:
    using clk = std::chrono::steady_clock;
    ~BackgroundTask() { abandon(); }

    template <class Fn>
    void start(const std::string& command, Fn fn) {
        if (th_.joinable()) th_.join();
        cancel_ = false;
        busy_ = true;
        ++id_;
        command_ = command;
        started_ = clk::now();
        th_ = std::thread([this, fn, command]() mutable {
            TaskOutcome o;
            try {
                o = fn();
            } catch (...) {
                o.success = false;
                o.reply = "Something went wrong while I was working on that.";
            }
            o.command = command;
            std::lock_guard<std::mutex> lk(mu_);
            result_ = std::move(o);
            has_result_ = true;
            busy_ = false;
        });
    }
    bool busy() const { return busy_; }
    bool has_result() {
        std::lock_guard<std::mutex> lk(mu_);
        return has_result_;
    }
    bool take(TaskOutcome& out) {
        std::lock_guard<std::mutex> lk(mu_);
        if (!has_result_) return false;
        out = std::move(result_);
        has_result_ = false;
        return true;
    }
    std::atomic<bool>& cancel_flag() { return cancel_; }
    uint64_t id() const { return id_; }
    const std::string& command() const { return command_; }
    int seconds() const {
        return static_cast<int>(std::chrono::duration_cast<std::chrono::seconds>(
            clk::now() - started_).count());
    }
    /// On shutdown: don't wait up to 100 s for an agent to finish.
    void abandon() {
        cancel_ = true;
        if (th_.joinable()) {
            if (busy_) th_.detach(); else th_.join();
        }
    }
    std::deque<std::string> queue;          // main thread only

private:
    std::thread       th_;
    uint64_t          id_ = 0;
    std::atomic<bool> busy_{false};
    std::atomic<bool> cancel_{false};
    std::mutex        mu_;
    bool              has_result_ = false;
    TaskOutcome       result_;
    std::string       command_;
    clk::time_point   started_{};
};

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
    // Second client for background tasks, so the main one stays free to
    // answer questions while a task runs.  (Only conversation state — no
    // extra model memory; both talk to the same local runtime.)
    demascas::LLMClient worker_llm;
    if (llm_cfg.configured)
        worker_llm.configure(llm_cfg.chat_url, llm_cfg.model, llm_cfg.context_tokens);
    {   // core tools live in the cached part of the tool prompt
        auto core = bridge.fetch_core_tools();
        llm.set_core_tools(core);
        worker_llm.set_core_tools(core);
        std::cout << "[✓] Core tools in the cached prompt: " << core.size() << "\n";
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

    // ── 6b. Speech detector + echo canceller ────────────────────
    //    Silero VAD tells a human voice from fans/typing/music; speexdsp
    //    removes DEMASCAS's own voice from the mic so the user can
    //    interrupt at a normal speaking volume.
    demascas::SpeechDetector vad, barge_vad;
    if (vad.init(cfg::SILERO_VAD_MODEL, cfg::SAMPLE_RATE, cfg::VAD_SPEECH_THRESHOLD,
                 cfg::VAD_MIN_SPEECH_SEC, cfg::VAD_MIN_SILENCE_SEC)) {
        barge_vad.init(cfg::SILERO_VAD_MODEL, cfg::SAMPLE_RATE, cfg::VAD_SPEECH_THRESHOLD,
                       cfg::VAD_MIN_SPEECH_SEC, cfg::VAD_MIN_SILENCE_SEC);
        audio.set_speech_detector(&vad);
        std::cout << "[✓] Speech detector: Silero VAD (ignores fans, typing, music)\n";
    }
    demascas::EchoCanceller aec(cfg::SAMPLE_RATE, cfg::AEC_TAIL_MS);
    if (aec.available()) {
        tts.set_echo_canceller(&aec);
        std::cout << "[✓] Echo cancellation on — interrupt me at a normal voice\n";
    }
    demascas::BargeInDetector barge;
    BackgroundTask task;
    std::string last_action_command;   // for "you did nothing" → retry
    std::string held_command;          // queued work held back after a failure
    std::chrono::steady_clock::time_point held_at{};
    bool first_after_wake = false;     // first request after the wake word
    float last_heard_conf = 1.0f;      // recogniser confidence of the last request
    uint64_t announced_task = 0;       // "still working on it" bookkeeping
    int announce_stage = 0;
    bool last_task_failed = false;     // don't run the queue behind a failure

    // ── 7. Init whisper.cpp (tiny + base models) ────────────────
    demascas::WhisperEngine whisper;
    if (!whisper.init(cfg::WHISPER_TINY_MODEL, cfg::WHISPER_BASE_MODEL)) {
        std::cerr << "[FATAL] Cannot load Whisper models.  Run:\n"
                  << "  ./scripts/build.sh   (downloads models automatically)\n";
        return 1;
    }
    // Recogniser hints = base words + what the user taught / apps DEMASCAS
    // learned ("Discord" stops being heard as "this cord").  Kept short:
    // whisper's prompt budget is ~220 tokens.
    auto refresh_vocabulary = [&]() {
        std::string prompt = cfg::WHISPER_PROMPT;
        size_t added = 0;
        for (auto& t : bridge.fetch_vocabulary()) {
            if (prompt.find(t) != std::string::npos) continue;
            if (prompt.size() + t.size() > 700 || added >= 40) break;
            prompt += ", " + t;
            ++added;
        }
        whisper.set_prompt(prompt);
        return added;
    };
    std::cout << "[✓] Recogniser vocabulary: +" << refresh_vocabulary() << " learned words\n";
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
    // What DEMASCAS said recently — if the mic hands it back (echo canceller
    // still learning), it must not be treated as the user talking.
    std::deque<std::pair<clk::time_point, std::string>> recently_said;
    std::mutex said_mu;
    auto on_sentence = [&tts, &recently_said, &said_mu](const std::string& sentence) {
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
        {
            std::lock_guard<std::mutex> lk(said_mu);
            recently_said.emplace_back(std::chrono::steady_clock::now(), sentence);
            while (recently_said.size() > 8) recently_said.pop_front();
        }
        tts.enqueue(sentence);
    };
    // "rest of your day" right after DEMASCAS said "How's the rest of your day going?"
    auto is_own_echo = [&](const std::string& heard) {
        std::istringstream iss(simplify(heard));
        std::vector<std::string> words;
        for (std::string w; iss >> w;) words.push_back(w);
        if (words.size() < 2) return false;
        std::lock_guard<std::mutex> lk(said_mu);
        auto now = std::chrono::steady_clock::now();
        for (auto& [t, said] : recently_said) {
            if (now - t > std::chrono::seconds(20)) continue;
            std::istringstream sis(simplify(said));
            std::vector<std::string> said_words;
            for (std::string w; sis >> w;) said_words.push_back(w);
            size_t hit = 0;
            for (auto& w : words) {
                // same word, or the same stem ("success" ~ "successfully")
                bool found = false;
                for (auto& sw : said_words) {
                    const size_t n = std::min(w.size(), sw.size());
                    if (w == sw || (n >= 4 && w.compare(0, n, sw, 0, n) == 0)) { found = true; break; }
                }
                hit += found;
            }
            if (hit * 10 >= words.size() * 7) return true;      // ≥ 70 % of the words
        }
        return false;
    };

    // Speech from a background task — silent once the user cancelled it.
    auto task_say = [&](const std::string& sentence) {
        if (!task.cancel_flag()) on_sentence(sentence);
    };

    // The three kinds of background work.  Each returns what happened;
    // the main loop logs it and handles confirmations / file choices.
    auto tool_work = [&](const std::string& cmd) {
        TaskOutcome o;
        o.path = "tool";
        std::string learned_ctx;
        try { learned_ctx = bridge.fetch_learned_context(cmd); } catch (...) {}
        o.reply = worker_llm.chat_with_tools_streaming(cmd, bridge, task_say,
                                                       task.cancel_flag(), learned_ctx);
        o.tools = worker_llm.last_tools_used();
        o.success = worker_llm.last_success();
        if (worker_llm.has_pending_confirmation()) {
            auto pc = worker_llm.take_pending_confirmation();
            o.confirm_token = pc.token;
            o.confirm_tool = pc.tool;
            o.confirm_speech = pc.speech;
        } else {
            o.file_choices = worker_llm.take_file_choices();
        }
        return o;
    };
    auto agent_work = [&](const std::string& cmd, const std::string& context_json) {
        auto ar = bridge.execute_agent(cmd, context_json);
        if (ar.unavailable && ar.summary.empty()) {
            std::cout << "[AGENT] Unavailable (" << ar.error << ") — using the tool path.\n";
            return tool_work(cmd);
        }
        TaskOutcome o;
        o.path = "agent";
        o.success = ar.success;
        o.tools = ar.tool_calls;
        if (!ar.confirm_token.empty()) {
            o.confirm_token = ar.confirm_token;
            o.confirm_tool = ar.pending_tool;
            o.confirm_speech = ar.confirm_speech;
            o.reply = ar.confirm_speech;
        } else if (!ar.summary.empty()) {
            o.reply = ar.summary;
            task_say(ar.summary);
        } else if (!ar.error.empty()) {
            o.reply = "I ran into an issue: " + demascas::utf8_cut(ar.error, 80);
            o.success = false;
            task_say(o.reply);
        }
        if (o.confirm_token.empty() && ar.file_choices.size() > 1)
            o.file_choices = ar.file_choices;
        return o;
    };
    auto skill_work = [&](const std::string& sid, const std::string& params) {
        TaskOutcome o;
        o.path = "skill";
        o.skill_id = sid;
        auto t0 = clk::now();
        auto sr = bridge.run_skill(sid, params);
        std::cout << "[⏱ SKILL RUN] " << std::chrono::duration_cast<std::chrono::milliseconds>(
                         clk::now() - t0).count() << " ms\n";
        o.success = sr.success;
        o.tools = sr.tool_calls;
        if (!sr.confirm_token.empty()) {
            // e.g. a chat message is typed and waiting: CONFIRMING asks.
            o.confirm_token = sr.confirm_token;
            o.confirm_tool = sr.pending_tool;
            o.confirm_speech = sr.confirm_speech;
            o.reply = sr.confirm_speech;
            return o;
        }
        o.reply = sr.summary.empty() ? (sr.success ? "Done." : "That didn't work.") : sr.summary;
        task_say(o.reply);
        return o;
    };

    auto short_cmd = [](const std::string& c) {
        // "I want you to open the friends section" → "open the friends section"
        static const std::regex FILLER(
            R"(^\s*(?:(?:hey|so|okay|ok|alright|well|please|now)[, ]+)*(?:(?:i want you to|i need you to|i'd like you to|can you|could you|would you|please|you should)\s+)+)",
            std::regex::icase);
        std::string s = std::regex_replace(c, FILLER, "");
        return s.size() <= 60 ? s : demascas::utf8_cut(s, 57) + "...";
    };
    auto task_status_line = [&]() {
        std::string line = "I'm still working on " + short_cmd(task.command()) + ".";
        try {
            auto st = bridge.agent_status();
            if (st.value("running", false)) {
                int step = st.value("step", 0);
                std::string tool = st.value("last_tool", std::string());
                for (auto& ch : tool) if (ch == '_') ch = ' ';
                if (step > 0) line += " I'm on step " + std::to_string(step);
                if (!tool.empty()) line += ", I just used " + tool;
                line += ".";
            }
        } catch (...) {}
        line += " " + std::to_string(task.seconds()) + " seconds so far.";
        if (!task.queue.empty())
            line += " After that I'll do " + short_cmd(task.queue.front()) + ".";
        return line;
    };

    // Barge-in while DEMASCAS speaks: echo-cancel the mic, run the VAD on
    // what's left, interrupt on a real human voice.  `spoken` receives the
    // (cleaned) audio of what the user already said, so no words are lost.
    auto watch_for_barge_in = [&](std::vector<float>& spoken) -> bool {
        barge.reset();
        barge_vad.reset();
        std::vector<float> recent;
        const size_t keep = static_cast<size_t>(cfg::SAMPLE_RATE * 0.8);
        while (tts.is_playing() && g_running) {
            std::this_thread::sleep_for(std::chrono::milliseconds(cfg::CAPTURE_POLL_MS));
            clk::time_point t_end;
            auto raw = audio.drain_timed(t_end);
            if (raw.empty()) continue;
            auto clean = aec.process(raw, t_end);
            if (clean.empty()) continue;
            barge_vad.accept(clean.data(), clean.size());
            recent.insert(recent.end(), clean.begin(), clean.end());
            if (recent.size() > keep)
                recent.erase(recent.begin(), recent.begin() + static_cast<long>(recent.size() - keep));
            float mic_rms = demascas::AudioEngine::rms(raw.data(), raw.size());
            float clean_rms = demascas::AudioEngine::rms(clean.data(), clean.size());
            bool speech = barge_vad.speaking();
            if (barge.update(mic_rms, clean_rms, speech, aec.converged(), speech)) {
                std::cout << "[!] BARGE-IN (voice " << clean_rms << " after echo removal, "
                          << (aec.converged() ? "AEC " + std::to_string(int(aec.echo_reduction_db())) + " dB"
                                              : std::string("AEC still learning"))
                          << ")\n";
                tts.interrupt();
                spoken = recent;
                return true;
            }
        }
        return false;
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
                first_after_wake = true;
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
            // ── A background task finished → report, then maybe run the next
            {
                TaskOutcome done;
                if (task.take(done)) {
                    std::cout << "[✅ TASK DONE] " << done.command
                              << (task.cancel_flag() ? " (cancelled)" : "") << "\n";
                    last_task_failed = !done.success && !task.cancel_flag();
                    if (done.path == "skill" || done.path == "agent") refresh_vocabulary();  // new apps learned
                    if (!done.reply.empty())
                        std::cout << "[DEMASCAS]: " << done.reply << "\n";
                    try {
                        bridge.log_interaction(done.command, done.path, done.tools,
                                               done.reply.empty() ? "Done." : done.reply,
                                               done.success && !task.cancel_flag(), done.skill_id);
                    } catch (...) {}
                    if (!task.cancel_flag()) {
                        if (!done.confirm_token.empty()) {
                            pending_token = done.confirm_token;
                            pending_tool = done.confirm_tool;
                            pending_description = done.confirm_speech;
                            state = State::CONFIRMING;
                            break;
                        }
                        if (done.file_choices.size() > 1) {
                            pending_file_choices = done.file_choices;   // choices already spoken
                            state = State::CLARIFYING;
                            break;
                        }
                    }
                }
                if (last_task_failed && !task.queue.empty()) {
                    // Don't blindly run what was queued behind a failed task.
                    std::string skipped = short_cmd(task.queue.front());
                    held_command = task.queue.front();
                    held_at = clk::now();
                    task.queue.clear();
                    last_task_failed = false;
                    on_sentence("Since that didn't work, I've held off on " + skipped +
                                ". Say it again if you still want it.");
                }
                if (!task.busy() && !task.has_result() && !task.queue.empty()) {
                    current_command = task.queue.front();
                    task.queue.pop_front();
                    std::cout << "[▶ NEXT] " << current_command << "\n";
                    state = State::PROCESSING;
                    break;
                }
            }

            // ── A long task: say so, instead of going quiet
            if (task.busy()) {
                if (task.id() != announced_task) { announced_task = task.id(); announce_stage = 0; }
                int secs = task.seconds();
                if (secs >= 15 && announce_stage == 0) {
                    on_sentence("Still working on " + short_cmd(task.command()) + ".");
                    announce_stage = 1;
                } else if (secs >= 45 && announce_stage == 1) {
                    on_sentence("This is taking a while. Say stop if you'd like me to give up.");
                    announce_stage = 2;
                }
            }

            // ── Barge-in monitor while DEMASCAS speaks (echo-cancelled VAD)
            std::vector<float> spoken_so_far;
            if (tts.is_playing()) {
                bool barged_in = watch_for_barge_in(spoken_so_far);
                if (!barged_in) {
                    // TTS finished naturally — short cooldown to let
                    // speaker residual ring out, then flush mic buffer.
                    if (!task.busy()) tts.play_sound("Tink");
                    last_tts_done = clk::now();
                    std::this_thread::sleep_for(post_tts_guard);
                    audio.drain();
                }
                // If barged in, skip cooldown — the user is already
                // speaking and we don't want to lose their words.
            }

            // While a task runs, listen in short slices so finished results
            // and new speech from DEMASCAS are handled promptly.
            const bool working = task.busy();
            static bool announced_working = false;
            if (working && !announced_working)
                std::cout << "[🟢 LISTENING] (task running — still listening)\n";
            else if (!working)
                std::cout << "[🟢 LISTENING]  (timeout " << cfg::FOLLOWUP_TIMEOUT << " s)\n";
            announced_working = working;

            auto utterance = audio.capture_utterance(
                working ? 1.0f : cfg::FOLLOWUP_TIMEOUT, cfg::SILENCE_DURATION, spoken_so_far,
                [&] { return tts.is_playing() || task.has_result(); },
                [&] { tts.hold(true); });          // don't talk over the user
            tts.hold(false);

            if (utterance.empty()) {
                if (working || task.busy() || task.has_result() || tts.is_playing())
                    break;                          // keep listening while working
                std::cout << "[*] No speech detected — going back to sleep.\n";
                tts.enqueue("Standing by.");
                tts.wait_done();
                last_tts_done = clk::now();
                audio.drain();  // flush any residual TTS echo
                llm.reset();
                worker_llm.reset();
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
                if (!task.busy()) ++consecutive_blank;
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
            // ── Its own voice coming back through the mic → not a command
            if (is_own_echo(current_command)) {
                std::cout << "[*] Ignored own echo: \"" << current_command << "\"\n";
                break;                                       // stay LISTENING
            }
            // ── How sure was the recogniser?  Garbled or background speech
            //    must never trigger an action ("run to, delete, cloud").
            {
                const float conf = whisper.last_confidence();
                const float unsure = whisper.last_unsure_share();
                const bool control = is_cancel_request(current_command) ||
                                     is_status_question(current_command) ||
                                     is_dismiss(current_command);
                std::cout << "[🎯 HEARD] confidence " << conf << ", unsure " << unsure << "\n";
                last_heard_conf = conf;
                if (task.busy() && !control && (conf < 0.65f || unsure > 0.2f)) {
                    // While working, DEMASCAS listens to the room: only clear
                    // speech (or stop / status) counts — not TV or other people.
                    std::cout << "[*] Ignored unclear background speech while working\n";
                    break;
                }
                if (!control && (conf < 0.5f || unsure >= 0.35f)) {
                    on_sentence("Sorry, I didn't catch that. Could you say it again?");
                    break;
                }
            }

            if (is_wake_only(current_command)) {
                std::cout << "[*] Wake word in LISTENING state — acknowledging.\n";
                tts.enqueue("I'm here. What can I do for you?");
                break;  // stay in LISTENING
            }

            // ── Dismiss detection (ChatGPT-style) ─────────
            //    While a task runs, "stop" means stop the TASK (PROCESSING).
            if (is_dismiss(current_command) && !task.busy()) {
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
            std::string skill_id;
            std::vector<std::string> tools_used;
            bool success = true;
            cancel_llm = false;
            std::string normalized_command = current_command;
            bool force_agent = false;     // retry after a complaint

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
                                           demascas::utf8_cut(result, 200), ok);
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

            // ── Feedback on the previous result ("good job" / "that's wrong")
            //    → reward signal for skills and future fine-tuning.  Not a task.
            {
                int fb = feedback_polarity(current_command);
                if (fb != 0) {
                    std::cout << "[👍 FEEDBACK] " << (fb > 0 ? "positive" : "negative") << "\n";
                    try { bridge.send_feedback(fb > 0); } catch (...) {}
                    on_sentence(fb > 0 ? "Thanks, I'll remember that worked."
                                       : "Sorry about that. I'll avoid doing it that way again.");
                    state = State::LISTENING;
                    break;
                }
            }

            // ── While a background task runs, the user can steer it by voice
            if (task.busy()) {
                if (is_cancel_request(current_command) || is_dismiss(current_command)) {
                    std::cout << "[⏹ CANCEL] " << task.command() << "\n";
                    task.cancel_flag() = true;                 // tool loop stops streaming
                    try { bridge.cancel_agent(); } catch (...) {}   // agent stops before its next step
                    task.queue.clear();
                    tts.interrupt();
                    const std::string s = simplify(current_command);
                    const bool done_for_now = has_word(s, "for the day") || has_word(s, "for today") ||
                        has_word(s, "goodbye") || has_word(s, "good night") || has_word(s, "go to sleep") ||
                        has_word(s, "stop now") || has_word(s, "thats enough") || is_dismiss(current_command);
                    if (done_for_now) {
                        // "that's enough for the day" = stop the task AND stand by
                        tts.enqueue("Okay, stopping. Standing by.");
                        tts.wait_done();
                        last_tts_done = clk::now();
                        audio.drain();
                        llm.reset();
                        worker_llm.reset();
                        state = State::SLEEPING;
                        break;
                    }
                    on_sentence("Okay, I've stopped.");
                    state = State::LISTENING;
                    break;
                }
                if (is_status_question(current_command)) {
                    on_sentence(task_status_line());
                    state = State::LISTENING;
                    break;
                }
            }
            // A new task while one is running waits its turn.
            auto defer_if_busy = [&]() -> bool {
                if (!task.busy()) return false;
                task.queue.push_back(normalized_command);
                std::cout << "[⏸ QUEUED] " << normalized_command << "\n";
                on_sentence("Sure, I'll do that right after I finish " +
                            short_cmd(task.command()) + ".");
                return true;
            };
            bool launched = false;   // work handed to the background task

            // PATH 1.5 (checked right after the shortcut): skills — learned or
            // built-in procedures run without the step-by-step agent loop.
            demascas::ToolBridge::SkillMatch skill;
            auto shortcut = shortcuts.match(current_command);
            if (!shortcut) {
                auto t_skill = clk::now();
                try { skill = bridge.match_skill(normalized_command); } catch (...) {}
                std::cout << "[⏱ SKILL MATCH] "
                          << std::chrono::duration_cast<std::chrono::milliseconds>(
                                 clk::now() - t_skill).count()
                          << " ms" << (skill.matched ? " → " + skill.name : "")
                          << (!skill.matched && !skill.route.empty() ? " (route: " + skill.route + ")" : "")
                          << "\n";
                if (!skill.note.empty()) {             // "Got it — Discord."
                    on_sentence(skill.note);
                    refresh_vocabulary();
                }
                // The server fixes names it knows ("cord" → "Discord").
                if (!skill.command.empty() && skill.command != normalized_command) {
                    std::cout << "[📝 CORRECTED] \"" << normalized_command << "\" → \""
                              << skill.command << "\"\n";
                    normalized_command = skill.command;
                    current_command = skill.command;
                }
            }
            // "Yes, I still want it" → run what was held back after a failure.
            if (!held_command.empty() && clk::now() - held_at < std::chrono::seconds(60)) {
                const std::string s = simplify(current_command);
                if (has_word(s, "still want") || has_word(s, "still wanted") || has_word(s, "yes") ||
                    has_word(s, "do it") || has_word(s, "go ahead") || has_word(s, "please do")) {
                    std::cout << "[↩ HELD] running: " << held_command << "\n";
                    current_command = held_command;
                    held_command.clear();
                    state = State::PROCESSING;
                    break;
                }
                held_command.clear();
            }
            // Follow-up side talk: after the first exchange, chat-like speech
            // counts only if it's addressed to DEMASCAS (it asked a question,
            // or you said its name).  Commands still work without the name.
            if (!shortcut && !skill.matched && !first_after_wake &&
                (skill.route == "chat" ||
                 (skill.route.empty() && shortcuts.is_simple_question(current_command)))) {
                std::string last_said;
                {
                    std::lock_guard<std::mutex> lk(said_mu);
                    if (!recently_said.empty()) last_said = recently_said.back().second;
                }
                const bool it_asked = !last_said.empty() &&
                    last_said.find_last_not_of(" ") != std::string::npos &&
                    last_said[last_said.find_last_not_of(" ")] == '?';
                const std::string s = simplify(current_command);
                const bool named = has_word(s, "demascas") || has_word(s, "damascus");
                if (!it_asked && !named) {
                    std::cout << "[*] Side talk ignored (not addressed to DEMASCAS)\n";
                    state = State::LISTENING;
                    break;
                }
            }
            first_after_wake = false;

            // The server judged it not a sensible request (fragments of words).
            if (!shortcut && !skill.matched && skill.route == "unclear") {
                std::cout << "[*] Unclear request — asking instead of acting\n";
                on_sentence(skill.ask.empty() ? "Sorry, I didn't catch that. Could you say it again?"
                                              : skill.ask);       // "Open what?"
                state = State::LISTENING;
                break;
            }
            // A complaint about the last task → reward signal + one honest retry
            // through the multi-step agent (instead of treating it as a command).
            if (!shortcut && !skill.matched && is_complaint(current_command)) {
                try { bridge.send_feedback(false); } catch (...) {}
                if (!last_action_command.empty()) {
                    std::cout << "[↩ RETRY] " << last_action_command << "\n";
                    on_sentence("Sorry about that. Let me try it properly.");
                    normalized_command = current_command = last_action_command;
                    force_agent = true;
                } else {
                    on_sentence("Sorry about that. Tell me what you'd like me to do and I'll try again.");
                    state = State::LISTENING;
                    break;
                }
            } else if (!shortcut && skill.route != "chat") {
                last_action_command = normalized_command;
            }

            // PATH 1: Shortcut fast-path (< 1 µs, bypasses LLM entirely)
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
            // PATH 1.5: Skill
            else if (skill.matched) {
                path_taken = "skill";
                skill_id = skill.skill_id;
                std::cout << "[🧠 SKILL] " << skill.name << "\n";
                if (skill.done) {
                    reply = skill.summary;               // e.g. taught a routine / a word
                    on_sentence(reply);
                    if (skill.skill_id == "vocabulary") refresh_vocabulary();
                } else if (!skill.ask.empty()) {
                    reply = skill.ask;                   // missing parameter
                    on_sentence(reply);
                } else {
                    launched = true;
                    if (!defer_if_busy()) {
                        if (!skill.ack.empty()) on_sentence(skill.ack);   // slow skills
                        std::string sid = skill.skill_id, params = skill.params_json;
                        task.start(normalized_command,
                                   [&skill_work, sid, params] { return skill_work(sid, params); });
                    }
                }
            }
            // PATH 2.5: Domain Agent (specialized, multi-step, memory-aware)
            //   The server's route decides; the local keyword rules only when
            //   it's unsure (or unreachable).
            else if (force_agent || skill.route == "agent" ||
                     (skill.route.empty() && demascas::is_agent_command(normalized_command))) {
                launched = true;
                if (!defer_if_busy()) {
                    std::string context_json = "{}";
                    try {
                        auto ctx = bridge.fetch_active_context();
                        if (!ctx.empty()) context_json = ctx;
                    } catch (...) {}
                    std::string cmd = normalized_command;
                    task.start(cmd, [&agent_work, cmd, context_json] {
                        return agent_work(cmd, context_json);
                    });
                }
            }
            // PATH 2: Simple question (streaming fast LLM, no tool schemas)
            //   Answered right away — even while a task runs.
            else if (skill.route == "chat" ||
                     (skill.route.empty() && shortcuts.is_simple_question(current_command))) {
                path_taken = "fast";
                std::cout << "[⚡ FAST PATH] Simple question — streaming.\n";
                if (!skill.knowledge.empty())
                    std::cout << "[🌐 WEB NOTES] " << skill.knowledge.size() << " chars from saved pages\n";
                reply = llm.fast_chat_streaming(
                    current_command, bridge, on_sentence, cancel_llm, skill.knowledge);
                if (last_heard_conf < 0.7f) llm.drop_last_exchange();   // don't let a mishearing steer later replies
                success = llm.last_success();
                // The fast path has no tools.  Its prompt tells the model to
                // say "Let me handle that for you" when an action is needed —
                // actually re-route those to the tool path.
                if (success && (contains(reply, "handle that for you") || claims_action(reply))) {
                    std::cout << "[⚡ FAST PATH] Model asked for tools — re-routing.\n";
                    run_tool_path = true;
                }
            } else {
                run_tool_path = true;
            }

            // PATH 3: Tool ReAct (default fallback) — in the background
            if (run_tool_path) {
                launched = true;
                if (!defer_if_busy()) {
                    std::cout << "[🔧 TOOL PATH] Full processing — streaming.\n";
                    std::string cmd = current_command;
                    task.start(cmd, [&tool_work, cmd] { return tool_work(cmd); });
                }
            }

            if (launched) {
                // Logged when the task finishes (LISTENING picks up the result).
                state = State::LISTENING;
                break;
            }

            if (!reply.empty()) {
                std::cout << "[DEMASCAS]: " << reply << "\n";
            }

            // Log once, with this command's own tools and real outcome.
            try {
                bridge.log_interaction(
                    current_command, path_taken, tools_used,
                    reply.empty() ? "Done." : reply, success, skill_id);
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
                    on_sentence("There was an issue. " + demascas::utf8_cut(result, 100));
                }
                bridge.log_interaction("confirm " + pending_tool, "confirm",
                                       {pending_tool}, demascas::utf8_cut(result, 200), !failed);
            } else {
                bridge.confirm(pending_token, false);
                std::cout << "[⚠️ CONFIRMING] Not approved — cancelling.\n";
                // Not a yes/no but a new request ("open the finder app") →
                // drop the pending action AND do what was just asked.
                std::string redirect;
                if (answer == Answer::UNCLEAR && !confirmation.empty() &&
                    !is_hallucination(confirmation)) {
                    std::istringstream iss(simplify(confirmation));
                    int words = 0;
                    for (std::string w; iss >> w;) ++words;
                    if (words >= 2) redirect = normalize_transcription(confirmation);
                }
                tts.enqueue(answer == Answer::NO ? "Okay, cancelled."
                            : !redirect.empty() ? "Okay, I won't do that."
                                                : "I didn't hear a clear yes, so I cancelled it.");
                if (!redirect.empty()) {
                    tts.wait_done();
                    last_tts_done = clk::now();
                    pending_tool.clear();
                    pending_token.clear();
                    pending_description.clear();
                    current_command = redirect;
                    std::cout << "[⚠️ CONFIRMING] New request instead: \"" << redirect << "\"\n";
                    state = State::PROCESSING;
                    break;
                }
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
    task.abandon();                 // don't wait for a long agent run
    tts.set_echo_canceller(nullptr);  // the canceller is destroyed before TTS
    audio.stop();
    whisper.shutdown();
    return 0;
}
