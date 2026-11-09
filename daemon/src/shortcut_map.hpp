/**
 * DEMASCAS — daemon/src/shortcut_map.hpp
 * Header-only: 72+ macOS shortcut lookup + simple-question detection.
 *
 * This runs entirely in C++ — no Python, no LLM.  Matching a
 * shortcut takes < 1 µs, giving the daemon a zero-latency fast-path
 * for common OS commands like "close window", "take a screenshot",
 * "minimize", etc.
 *
 * Also contains is_simple_question() which detects conversational
 * queries that can be answered by the LLM without any tools.
 */

#pragma once

#include <algorithm>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace demascas {

/// Whole-word / whole-phrase containment: "rain" does NOT match "brain",
/// "use" does NOT match "because".  Leading/trailing spaces in `phrase`
/// are ignored.  Both strings are expected to be lowercase.
inline bool contains_phrase(const std::string& text, const std::string& phrase_in) {
    auto a = phrase_in.find_first_not_of(' ');
    auto b = phrase_in.find_last_not_of(' ');
    if (a == std::string::npos) return false;
    const std::string phrase = phrase_in.substr(a, b - a + 1);
    auto is_word = [](char c) {
        return std::isalnum(static_cast<unsigned char>(c)) || c == '\'';
    };
    size_t pos = 0;
    while ((pos = text.find(phrase, pos)) != std::string::npos) {
        bool left_ok  = pos == 0 || !is_word(text[pos - 1]) || !is_word(phrase.front());
        size_t end = pos + phrase.size();
        bool right_ok = end >= text.size() || !is_word(text[end]) || !is_word(phrase.back());
        if (left_ok && right_ok) return true;
        ++pos;
    }
    return false;
}

class ShortcutMap {
public:
    ShortcutMap() {
        // ── Window / App management ─────────────────────────────
        s_["close window"]       = "command+w";
        s_["close tab"]          = "command+w";
        s_["quit app"]           = "command+q";
        s_["close app"]          = "command+q";
        s_["exit app"]           = "command+q";
        s_["quit application"]   = "command+q";
        s_["close application"]  = "command+q";
        s_["minimize"]           = "command+m";
        s_["minimize all"]       = "command+option+m";
        s_["hide app"]           = "command+h";
        s_["hide others"]        = "command+option+h";
        s_["new window"]         = "command+n";
        s_["new tab"]            = "command+t";
        s_["close all windows"]  = "command+option+w";
        s_["close powerpoint"]   = "command+q";
        s_["close powerpoint app"] = "command+q";
        s_["close power point app"] = "command+q";
        s_["close powerpoint presentation"] = "command+q";
        s_["close the powerpoint presentation"] = "command+q";
        s_["full screen"]        = "command+control+f";
        s_["toggle full screen"] = "command+control+f";
        s_["switch app"]         = "command+tab";
        s_["switch window"]      = "command+`";
        s_["force quit"]         = "command+option+escape";
        s_["next tab"]           = "control+tab";
        s_["previous tab"]       = "control+shift+tab";

        // ── Editing ─────────────────────────────────────────────
        s_["undo"]               = "command+z";
        s_["redo"]               = "command+shift+z";
        s_["cut"]                = "command+x";
        s_["copy"]               = "command+c";
        s_["paste"]              = "command+v";
        s_["paste plain"]        = "command+shift+v";
        s_["select all"]         = "command+a";
        s_["find"]               = "command+f";
        s_["find replace"]       = "command+option+f";
        s_["find next"]          = "command+g";
        s_["find previous"]      = "command+shift+g";
        s_["bold"]               = "command+b";
        s_["italic"]             = "command+i";
        s_["underline"]          = "command+u";

        // ── File ────────────────────────────────────────────────
        s_["save"]               = "command+s";
        s_["save as"]            = "command+shift+s";
        s_["open file"]          = "command+o";
        s_["print"]              = "command+p";
        s_["reopen tab"]         = "command+shift+t";

        // ── System ──────────────────────────────────────────────
        s_["spotlight"]          = "command+space";
        s_["search"]             = "command+space";
        s_["screenshot"]         = "command+shift+3";
        s_["screenshot region"]  = "command+shift+4";
        s_["screenshot window"]  = "command+shift+4, space";  // two-step sequence
        s_["screen recording"]   = "command+shift+5";
        s_["lock screen"]        = "command+control+q";
        s_["log out"]            = "command+shift+q";
        s_["show desktop"]       = "command+f3";
        s_["mission control"]    = "control+up";
        s_["app expose"]         = "control+down";
        s_["left space"]         = "control+left";
        s_["right space"]        = "control+right";
        s_["emoji"]              = "command+control+space";
        s_["zoom in"]            = "command+=";
        s_["zoom out"]           = "command+-";
        s_["reset zoom"]         = "command+0";
        s_["refresh"]            = "command+r";
        s_["reload"]             = "command+r";
        s_["preferences"]        = "command+,";
        s_["settings"]           = "command+,";
        s_["trash"]              = "command+delete";
        s_["empty trash"]        = "command+shift+delete";

        // ── Browser ─────────────────────────────────────────────
        s_["back"]               = "command+[";
        s_["forward"]            = "command+]";
        s_["address bar"]        = "command+l";
        s_["url bar"]            = "command+l";
        s_["bookmark"]           = "command+d";
        s_["bookmarks"]          = "command+shift+b";
        s_["downloads"]          = "command+option+l";
        s_["private window"]     = "command+shift+n";
        s_["incognito"]          = "command+shift+n";
    }

    // ── Nano-router: heavy-compute classification ───────────────

    /**
     * Return true if the query requires the Heavyweight 7B model.
     *
     * Triggers on deep reasoning: debugging, architecture analysis,
     * algorithm explanations, code review, detailed comparisons,
     * multi-step logical problems, etc.
     *
     * Everything else (quick Q&A, OS control, tool calls) stays on
     * the Vanguard for near-instant response.
     */
    bool is_heavy_query(const std::string& text) const {
        std::string t = lower(trim(text));

        // ── Explicit tool / OS triggers → NOT heavy ─────────────
        // If it's clearly an OS action, the vanguard handles it.
        static const std::vector<std::string> light_only = {
            "open ", "launch ", "close ", "quit ", "click ",
            "type ", "press ", "minimize", "maximize", "screenshot",
            "scroll ", "drag ", "switch to", "activate ",
            "what time", "what date", "what day",
            "list files", "list windows", "list app",
            "search for", "search the web",
            "remember ", "save memory", "recall ",
        };
        for (const auto& lo : light_only)
            if (t.find(lo) != std::string::npos) return false;

        // ── Deep-reasoning triggers → HEAVY ─────────────────────
        static const std::vector<std::string> heavy_triggers = {
            // Programming / debugging
            "debug", "refactor", "code review", "explain the code",
            "explain this code", "what does this code",
            "fix this", "fix the bug", "segfault", "seg fault",
            "memory leak", "stack trace", "backtrace",
            "data structure", "algorithm", "time complexity",
            "space complexity", "big o",
            // Architecture / design
            "architecture", "design pattern", "system design",
            "trade-off", "tradeoff", "pros and cons",
            "compare", "versus", " vs ", "difference between",
            // ML / AI
            "hyperparameter", "tuning", "xgboost", "gradient",
            "neural network", "machine learning", "deep learning",
            "train", "training", "epoch", "loss function",
            "backpropagation", "transformer", "attention mechanism",
            // Math / logic
            "prove ", "proof", "theorem", "derivation",
            "mathematical", "equation", "integral", "derivative",
            // Lengthy explanations
            "in detail", "in depth", "step by step",
            "walk me through", "break down", "deep dive",
            "thorough", "comprehensive", "elaborate",
            "explain how", "explain why", "how does",
            "write a program", "write code", "implement",
            "build a", "create a script", "write a script",
            // General indicators of complex reasoning
            "analyze", "analyse", "evaluate", "assessment",
            "strategy", "optimiz", "benchmark",
        };
        for (const auto& ht : heavy_triggers)
            if (t.find(ht) != std::string::npos) return true;

        // ── Long queries (> 20 words) are likely complex ────────
        int word_count = 0;
        bool in_word = false;
        for (char c : t) {
            if (std::isalpha(static_cast<unsigned char>(c))) {
                if (!in_word) { ++word_count; in_word = true; }
            } else {
                in_word = false;
            }
        }
        if (word_count > 20) return true;

        return false;
    }

    // ── Shortcut matching ───────────────────────────────────────

    /**
     * Try to match user text against the shortcut map.
     * Returns the key combo (e.g. "command+shift+3") or std::nullopt.
     *
     * Skips matching when the command clearly needs LLM tool reasoning
     * (e.g. "summarize PDF", "open Safari", "scroll down").
     */
    std::optional<std::string> match(const std::string& text) const {
        std::string raw = lower(trim(text));
        if (raw.empty()) return std::nullopt;

        // ── Questions are answered, never executed ──────────────
        // "how do I lock my screen?" must not lock the screen.
        if (raw.back() == '?') return std::nullopt;
        std::string t = normalize_words(raw);
        auto tokens = split_vec(t);
        if (tokens.empty()) return std::nullopt;
        static const std::set<std::string> QUESTION_STARTS = {
            "how", "what", "what's", "whats", "why", "when", "where", "which",
            "who", "is", "are", "does", "should", "could", "would", "will",
            "explain", "tell", "describe",
        };
        if (QUESTION_STARTS.count(tokens.front())) return std::nullopt;

        // ── Phrases that MUST go to the LLM tool path ──────────
        static const std::vector<std::string> tool_only = {
            "summarize", "summarise", "read pdf", "list files", "what files",
            "what's in", "show files", "files in", "open", "launch",
            "start", "click", "type", "activate", "switch to",
            "which app", "what app", "running app", "what is open",
            "downloads folder", "documents folder", "desktop folder",
            "search the web", "search online", "look up", "google",
            "browse", "read webpage", "read website", "fetch url",
            "remember", "save memory", "recall", "do you remember",
            "scroll", "drag",
            "list windows", "what windows", "show windows", "all windows",
            "[file]",
        };
        for (const auto& p : tool_only) {
            if (p == "open" && starts_with(t, "reopen")) continue;  // "reopen tab" is a shortcut
            if (contains_phrase(raw, p)) return std::nullopt;
        }

        // ── "close X" / "quit X" where X is an app name ────────
        static const std::vector<std::string> quit_w = {
            "close ", "quit ", "exit "
        };
        for (const auto& qw : quit_w) {
            if (starts_with(t, qw)) {
                auto after = t.substr(qw.size());
                if (!after.empty()) {
                    auto first = after.substr(0, after.find(' '));
                    if (first != "window" && first != "tab" &&
                        first != "this" && first != "the" && first != "all" &&
                        first != "app" && first != "application")
                        return std::nullopt;  // "close Safari" → tool
                }
            }
        }
        if (contains_phrase(t, "close it") || contains_phrase(t, "quit it") ||
            contains_phrase(t, "exit it"))
            return std::nullopt;

        // ── Direct exact lookup ─────────────────────────────────
        auto it = s_.find(t);
        if (it != s_.end()) return it->second;

        // ── Phrase match: the utterance must consist ONLY of the
        //    shortcut's words plus filler words.  Substring matching used
        //    to turn "check the logs about the outage" into ⌘⇧Q (log out)
        //    and "how much disk space is left" into ⌃← (switch space).
        static const std::set<std::string> FILLER = {
            "please", "can", "could", "would", "will", "you", "the", "a", "an",
            "this", "that", "it", "my", "me", "now", "current", "just",
            "quickly", "go", "ahead", "and", "hey", "demascas", "damascus",
            "take", "do", "window", "app", "for", "right", "okay", "ok",
            "alright", "then", "thanks", "thank", "kindly", "hit", "press",
            "file", "page", "document", "make", "turn", "to", "again",
        };
        std::set<std::string> token_set(tokens.begin(), tokens.end());
        std::optional<std::string> best;
        size_t best_len = 0;
        for (const auto& [phrase, keys] : s_) {
            auto pwords = split_vec(phrase);
            std::set<std::string> pset(pwords.begin(), pwords.end());
            bool all_present = true;
            for (const auto& w : pwords)
                if (!token_set.count(w)) { all_present = false; break; }
            if (!all_present) continue;
            bool only_filler_left = true;
            for (const auto& tok : tokens)
                if (!pset.count(tok) && !FILLER.count(tok)) { only_filler_left = false; break; }
            if (!only_filler_left) continue;
            if (pwords.size() > best_len) {
                best = keys;
                best_len = pwords.size();
            }
        }
        return best;
    }

    // ── [LOBOTOMY] extract_url, extract_app_open, extract_app_close,
    //    extract_type_text REMOVED — all intent routing now handled
    //    by the LLM semantic path (PATH 3).  Only pure keyboard
    //    shortcuts (PATH 1) and simple Q&A (PATH 2) remain in C++.

    // ── Realtime query detection ────────────────────────────────

    /**
     * Return true if the query needs LIVE data (weather, news, scores, etc).
     * These MUST go to PATH 3 (Tool LLM with search_web), never PATH 2.
     */
    bool is_realtime_query(const std::string& text) const {
        std::string t = lower(trim(text));
        static const std::vector<std::string> realtime_triggers = {
            "weather", "temperature", "forecast", "rain", "sunny",
            "humid", "degrees", "climate",
            "news", "latest", "current events",
            "stock", "price of", "market",
            "score", "result of", "who won", "what happened",
            "search for", "look up", "find out", "check the",
            "what time is it in",
        };
        for (const auto& kw : realtime_triggers)
            if (contains_phrase(t, kw)) return true;
        return false;
    }

    // ── Simple question detection ───────────────────────────────

    /**
     * Return true if the command is a conversational/knowledge question
     * that needs NO OS tools — the LLM can answer from its own weights.
     */
    bool is_simple_question(const std::string& text) const {
        std::string t = lower(trim(text));

        // Realtime queries MUST go to tool path, never fast Q&A
        if (is_realtime_query(text)) return false;

        // ── FORCE_TOOL_KEYWORDS: commands that MUST reach PATH 3/4 ──
        // These are action/compose commands the fast path cannot handle.
        static const std::vector<std::string> force_tool = {
            "create ", "make ",
            "composing", "composition",
            "compose", "draft an email", "draft a mail", "draft email",
            "write an email", "write a mail", "write email",
            "send an email", "send a mail", "send email",
            "reply to", "respond to",
            "read email", "check inbox", "first 10 emails",
            "click the", "click on", "tap the", "tap on",
            "fill in", "fill out", "fill the",
            "type into", "type in the",
            "navigate to", "go to ",
            "search for", "search on",
            "add to cart", "buy ",
            "open gmail", "open youtube", "open amazon",
            "open my study routine", "study routine",
            "summarize selected pdf", "summarize selected file", "highlighted file",
            "play lofi", "lofi", "low fi", "low five",
            "process", "processes", "running on my", "system info",
            "battery", "disk space", "memory usage", "cpu", 
            "storage", "uptime", "wifi", "network", "ip address",
            "what apps", "which apps", "check my", "show me my",
            "list my", "tell me the current", "currently running",
            "check your", "logs", "error", "delegate", "iot",
            "remember that", "always use",
            "do it", "set it", "change it", "go ahead", "execute", "remember", "save",
            // Questions about what's on screen / local data need tools
            // (the fast path has none and would just make something up).
            "screen", "on my screen", "this page", "this window", "this tab",
            "clipboard", "selected", "folder", "my files", "inbox",
            "my email", "my emails", "my mail", "read my", "unread",
        };
        for (const auto& kw : force_tool)
            if (contains_phrase(t, kw)) return false;

        // Explicit tool triggers → NOT simple
        static const std::vector<std::string> triggers = {
            "open ", "launch ", "start ", "close ", "quit ", "exit",
            "compose", "composing", "draft", "send mail", "send email",
            "create ", "make ",
            "minimize", "maximize", "click ", "type ", "press ",
            "search for", "go to", "switch to", "screenshot", "save",
            "copy", "paste", "undo", "redo", "find ", "select all",
            "summarize", "summarise", "read pdf", "full screen", "hide ",
            "new tab", "new window", "refresh", "reload", "zoom",
            "lock", "log out", "show desktop", "mission control",
            "force quit", "bookmark", "download", "private", "incognito",
            "trash", "emoji", "activate",
            "list files", "what files", "files in", "show files",
            "which app", "what app", "running app", "what is open",
            "what's open", "whats open",
            "browser", "read webpage", "read website", "fetch ",
            "remember", "save memory", "recall", "do you remember",
            "what did i tell", "what did i say", "rule", "preference",
            "scroll ", "drag ",
            "list windows", "what windows", "show windows", "all windows",
            "study routine", "summarize selected", "selected pdf", "highlighted file",
            "youtube", "play music", "lofi", "low fi", "low five",
            "[file]",  // transcription normalizer tags file-related queries
        };
        for (const auto& tr : triggers)
            if (contains_phrase(t, tr)) return false;

        // Action phrasing can still be prefixed with conversational lead-ins
        // like "so" or "alright". Do not fast-path those.
        static const std::vector<std::string> action_verbs = {
            "open", "close", "click", "type", "press", "scroll",
            "search", "download", "upload", "save", "delete",
            "create", "make", "move", "copy", "paste", "drag",
            "launch", "quit", "minimize", "maximize", "switch",
            "screenshot", "activate", "navigate", "browse", "use",
        };
        bool has_action = false;
        for (const auto& v : action_verbs) {
            if (contains_phrase(t, v)) {
                has_action = true;
                break;
            }
        }

        // Matches a question/conversational prefix
        static const std::vector<std::string> prefixes = {
            "what", "who", "when", "where", "why", "how",
            "is ", "are ", "do ", "does ", "can ", "could ",
            "would ", "will ", "should ", "shall ",
            "tell me", "explain", "define", "describe", "calculate",
            "what's", "whats", "how's", "hows",
            "thank", "thanks", "hello", "hi ", "hey ", "good ",
            "name a", "list ", "give me",
            "say ", "sing ", "joke", "story", "poem", "quote",
            "meaning of", "convert ", "translate ",
            "introduce", "my name", "your name", "you are",
            "nice to", "pleased to", "glad to",
            "i am ", "i'm ", "call me ",
            "alright", "okay", "ok ", "so ",
            "you know", "actually", "well ",
        };
        for (const auto& p : prefixes)
            if (starts_with(t, p)) return !has_action;

        // Ends with a question mark
        if (!t.empty() && t.back() == '?') return true;

        // Heuristic: if there are no OS-action verbs and it's
        // a conversational-length sentence, treat it as simple Q&A.
        // This catches things like "greet my friend", "tell him hi",
        // "my friend says hello" without hard-coding each phrase.
        if (!has_action) return true;

        return false;
    }

private:
    std::unordered_map<std::string, std::string> s_;

    // ── String utilities ────────────────────────────────────────

    static std::string lower(std::string s) {
        std::transform(s.begin(), s.end(), s.begin(), ::tolower);
        return s;
    }

    static std::string trim(const std::string& s) {
        auto a = s.find_first_not_of(" \t\n\r");
        auto b = s.find_last_not_of(" \t\n\r");
        if (a == std::string::npos) return "";
        return s.substr(a, b - a + 1);
    }

    /// Lowercase text → words separated by single spaces; sentence
    /// punctuation removed (apostrophes and [file] tags kept).
    static std::string normalize_words(const std::string& s) {
        std::string out;
        out.reserve(s.size());
        for (char c : s) {
            if (c == '.' || c == ',' || c == '!' || c == '?' || c == ';' ||
                c == ':' || c == '"' || c == '\t' || c == '\n')
                c = ' ';
            if (c == ' ' && (out.empty() || out.back() == ' ')) continue;
            out += c;
        }
        while (!out.empty() && out.back() == ' ') out.pop_back();
        return out;
    }

    static bool starts_with(const std::string& s, const std::string& prefix) {
        return s.size() >= prefix.size() &&
               s.compare(0, prefix.size(), prefix) == 0;
    }

    static std::set<std::string> split_set(const std::string& s) {
        std::set<std::string> out;
        std::istringstream iss(s);
        std::string word;
        while (iss >> word) out.insert(word);
        return out;
    }

    static std::vector<std::string> split_vec(const std::string& s) {
        std::vector<std::string> out;
        std::istringstream iss(s);
        std::string word;
        while (iss >> word) out.push_back(word);
        return out;
    }
};

inline bool is_agent_command(const std::string& cmd) {
    static const std::vector<std::string> AGENT_TRIGGERS = {
        "compose email", "compose mail", "composing email", "composing mail",
        "write email", "write mail", "draft email", "draft mail",
        "new email", "new mail", "email to", "mail to",
        "reply to", "smart reply", "respond to that",
        "open gmail", "check email", "read email",
        "powerpoint", "power point", "blank presentation", "new presentation",
        "open downloads folder", "open download folder",
        "open documents folder", "open desktop folder",
        "browse to", "navigate to", "go to website",
        "open youtube", "youtube in brave", "open in brave",
        "open the", "open a", "website", "webpage", "web page",
        "wikipedia", "google", "reddit", "github", "youtube",
        "youtube", "watch video", "play video",
        "search the web", "look up online",
        "summarize the pdf", "read the pdf",
        "find all", "list all files",
        "student productivity pack", "student workflow pack", "install student pack",
        "start class notes", "start notes session", "lecture notes",
        "start revision timer", "study timer", "focus timer", "pomodoro",
        "assignment planner", "plan my assignments", "homework planner",
        "capture citation", "research citation", "save source link",
        "daily student startup", "start my study day", "student startup",
        "list student workflows", "show student workflows", "show built in workflows",
        "open my study routine", "start study routine", "study routine",
        "research", "look up", "tell me about",
        "weather in", "news about", "latest on",
        "find information", "find out",
        "extract contact", "who emailed",
        "summarize selected pdf", "summarize selected file", "highlighted file",
        "play lofi", "lofi", "low fi", "low five",
        "open my", "find my", "find the",
    };

    std::string lower = cmd;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);

    static const std::regex web_open_1(
        R"(\bopen\b.{0,40}\b(page|website|site|url|link)\b)",
        std::regex::icase
    );
    static const std::regex web_open_2(
        R"(\bopen\b.{0,40}(wikipedia|google|reddit|github|twitter|youtube|instagram|amazon))",
        std::regex::icase
    );
    static const std::regex web_open_3(
        R"(\bopen\b.{0,40}(\.com|\.org|\.io|\.net|\.edu))",
        std::regex::icase
    );

    if (std::regex_search(lower, web_open_1) ||
        std::regex_search(lower, web_open_2) ||
        std::regex_search(lower, web_open_3)) {
        return true;
    }

    // Robust speech/noise handling for communication/workflow intents.
    static const std::regex compose_mail_re(
        R"(\b(compose|draft|write|create|new)\b.{0,40}\b(mail|email)\b)",
        std::regex::icase
    );
    static const std::regex mail_to_re(
        R"(\b(mail|email)\b.{0,25}\bto\b)",
        std::regex::icase
    );
    static const std::regex study_routine_re(
        R"(\b(start|open|run)\b.{0,35}\b(study routine|student startup|daily student startup)\b)",
        std::regex::icase
    );
    if (std::regex_search(lower, compose_mail_re) ||
        std::regex_search(lower, mail_to_re) ||
        std::regex_search(lower, study_routine_re)) {
        return true;
    }

    for (const auto& trigger : AGENT_TRIGGERS) {
        if (contains_phrase(lower, trigger)) return true;
    }
    return false;
}

}  // namespace demascas
