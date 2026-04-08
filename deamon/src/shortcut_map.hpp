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
        s_["screenshot window"]  = "command+shift+4+space";
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
        std::string t = lower(trim(text));

        // ── Phrases that MUST go to the LLM tool path ──────────
        static const std::vector<std::string> tool_only = {
            "summarize", "summarise", "read pdf", "list files", "what files",
            "what's in", "show files", "files in", "open ", "launch ",
            "start ", "click ", "type ", "activate ", "switch to ",
            "which app", "what app", "running app", "what is open",
            "downloads folder", "documents folder", "desktop folder",
            "search the web", "search online", "look up", "google",
            "browse", "read webpage", "read website", "fetch url",
            "remember", "save memory", "recall", "do you remember",
            "scroll ", "drag ",
            "list windows", "what windows", "show windows", "all windows",
            "[file]",
        };
        for (const auto& p : tool_only)
            if (t.find(p) != std::string::npos) return std::nullopt;

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
                        first != "this" && first != "the" && first != "all")
                        return std::nullopt;  // "close Safari" → tool
                }
            }
        }
        if (t.find("close it") != std::string::npos ||
            t.find("quit it")  != std::string::npos ||
            t.find("exit it")  != std::string::npos)
            return std::nullopt;

        // ── Direct exact lookup ─────────────────────────────────
        auto it = s_.find(t);
        if (it != s_.end()) return it->second;

        // ── Fuzzy match: prefer longer (more specific) phrases ──
        auto words_set = split_set(t);
        int input_word_count = static_cast<int>(words_set.size());
        std::optional<std::string> best;
        size_t best_len = 0;

        for (const auto& [phrase, keys] : s_) {
            auto pwords = split_vec(phrase);
            if (pwords.size() == 1) {
                // Single-word shortcut: only match in short inputs (≤ 3 words)
                // to prevent false positives like "back" matching in
                // "greet him back as if he was greeting you"
                if (input_word_count <= 3 &&
                    words_set.count(pwords[0]) && 1 > best_len) {
                    best     = keys;
                    best_len = 1;
                }
            } else {
                // Multi-word: all words must appear
                bool all = true;
                for (const auto& w : pwords)
                    if (t.find(w) == std::string::npos) { all = false; break; }
                if (all && pwords.size() > best_len) {
                    best     = keys;
                    best_len = pwords.size();
                }
            }
        }
        return best;
    }

    // ── URL detection (deterministic fast-path for opening websites) ──

    struct UrlMatch {
        std::string url;       // e.g. "https://instagram.com"
        std::string browser;   // e.g. "Safari", or "" for system default
    };

    /**
     * Detect a URL/domain in the user's speech and extract it.
     * Only triggers when the intent is clearly to OPEN/VISIT a site
     * (not "type", "search for", etc.).
     *
     * Returns the full URL + optional browser, or std::nullopt.
     */
    std::optional<UrlMatch> extract_url(const std::string& text) const {
        std::string t = lower(trim(text));

        // ── Must have an "open" intent — skip if user wants to TYPE ─
        static const std::vector<std::string> open_intents = {
            "open", "go to", "goto", "visit", "navigate",
            "browse", "load", "take me to", "show me",
            "pull up", "launch", "can you open",
        };
        bool has_open = false;
        for (const auto& intent : open_intents)
            if (t.find(intent) != std::string::npos) { has_open = true; break; }
        if (!has_open) return std::nullopt;

        // ── Normalise whisper "dot com" → ".com" transcriptions ──
        static const std::vector<std::pair<std::string, std::string>> dot_fixes = {
            {" dot com",  ".com"},  {" dot org",  ".org"},
            {" dot net",  ".net"},  {" dot io",   ".io"},
            {" dot edu",  ".edu"},  {" dot gov",  ".gov"},
            {" dot co ",  ".co "},  {" dot dev",  ".dev"},
            {" dot app",  ".app"},  {" dot in",   ".in"},
            {" dot me",   ".me"},
        };
        for (const auto& [from, to] : dot_fixes) {
            auto pos = t.find(from);
            if (pos != std::string::npos)
                t.replace(pos, from.size(), to);
        }

        // ── Resolve bare site names (e.g. "google" → "google.com") ──
        // Whisper often transcribes just the site name without the TLD.
        static const std::unordered_map<std::string, std::string> SITE_NAMES = {
            {"google",     "google.com"},
            {"youtube",    "youtube.com"},
            {"gmail",      "mail.google.com"},
            {"github",     "github.com"},
            {"reddit",     "reddit.com"},
            {"twitter",    "twitter.com"},
            {"facebook",   "facebook.com"},
            {"instagram",  "instagram.com"},
            {"linkedin",   "linkedin.com"},
            {"wikipedia",  "wikipedia.org"},
            {"amazon",     "amazon.com"},
            {"netflix",    "netflix.com"},
            {"spotify",    "open.spotify.com"},
            {"whatsapp",   "web.whatsapp.com"},
            {"stackoverflow", "stackoverflow.com"},
            {"stack overflow", "stackoverflow.com"},
            {"chatgpt",    "chat.openai.com"},
            {"chat gpt",   "chat.openai.com"},
        };
        for (const auto& [name, domain] : SITE_NAMES) {
            if (t.find(name) != std::string::npos) {
                std::string url = "https://" + domain;
                // Detect browser name
                std::string browser;
                static const std::vector<std::pair<std::string, std::string>> br = {
                    {"safari",  "Safari"},
                    {"chrome",  "Google Chrome"},
                    {"firefox", "Firefox"},
                    {"brave",   "Brave Browser"},
                    {"edge",    "Microsoft Edge"},
                    {"arc",     "Arc"},
                };
                for (const auto& [kw, bname] : br) {
                    if (t.find(kw) != std::string::npos) {
                        browser = bname;
                        break;
                    }
                }
                return UrlMatch{url, browser};
            }
        }

        // ── Extract domain (e.g. "instagram.com", "en.wikipedia.org") ─
        static const std::regex domain_re(
            R"(([a-z0-9][-a-z0-9]*(?:\.[a-z0-9][-a-z0-9]*)*\.(?:com|org|net|io|edu|gov|co|dev|app|me|tv|info|xyz|in))\b)");
        std::smatch m;
        if (!std::regex_search(t, m, domain_re)) return std::nullopt;

        std::string domain = m[1].str();
        std::string url = "https://" + domain;

        // ── Detect browser name ─────────────────────────────────
        std::string browser;
        static const std::vector<std::pair<std::string, std::string>> browsers = {
            {"safari",  "Safari"},
            {"chrome",  "Google Chrome"},
            {"firefox", "Firefox"},
            {"brave",   "Brave Browser"},
            {"edge",    "Microsoft Edge"},
            {"arc",     "Arc"},
        };
        for (const auto& [kw, name] : browsers) {
            if (t.find(kw) != std::string::npos) {
                browser = name;
                break;
            }
        }

        return UrlMatch{url, browser};
    }

    // ── App open detection (deterministic fast-path) ───────────

    struct AppMatch {
        std::string app_name;   // resolved + title-cased
    };

    /**
     * Detect "open [app]" / "launch [app]" commands and extract the app
     * name deterministically.  Runs AFTER extract_url() so domains are
     * already handled.  Returns std::nullopt for complex commands that
     * need LLM reasoning.
     */
    std::optional<AppMatch> extract_app_open(const std::string& text) const {
        std::string t = lower(trim(text));

        // Must start with an "open" intent
        static const std::vector<std::string> open_prefixes = {
            "can you open ", "could you open ", "please open ",
            "open up ", "fire up ", "launch ", "open ", "start ",
        };
        std::string after;
        bool found = false;
        for (const auto& p : open_prefixes) {
            if (starts_with(t, p)) {
                after = t.substr(p.size());
                found = true;
                break;
            }
        }
        if (!found) return std::nullopt;

        // Strip articles and filler words
        static const std::vector<std::string> filler = {
            "the ", "a ", "an ", "my ", "up ",
        };
        for (const auto& f : filler) {
            if (starts_with(after, f))
                after = after.substr(f.size());
        }

        // Trim trailing whitespace / punctuation
        while (!after.empty() &&
               (after.back() == ' ' || after.back() == '.' || after.back() == '!'))
            after.pop_back();
        if (after.empty()) return std::nullopt;

        // Skip if it contains a period → probably a domain (URL path)
        if (after.find('.') != std::string::npos) return std::nullopt;

        // Skip if too many words (complex command → LLM)
        int wc = 1;
        for (char c : after) if (c == ' ') ++wc;
        if (wc > 3) return std::nullopt;

        // Skip phrases that indicate non-app-open intent
        static const std::vector<std::string> skip = {
            "file", "folder", "directory", "document", "window",
            "tab", "page", "link", "pdf", "image", "photo",
        };
        for (const auto& s : skip)
            if (after.find(s) != std::string::npos) return std::nullopt;

        // ── Alias resolution (common names → macOS .app names) ──
        static const std::unordered_map<std::string, std::string> aliases = {
            {"safari", "Safari"}, {"chrome", "Google Chrome"},
            {"google chrome", "Google Chrome"}, {"firefox", "Firefox"},
            {"brave", "Brave Browser"}, {"brave browser", "Brave Browser"},
            {"arc", "Arc"}, {"edge", "Microsoft Edge"},
            {"terminal", "Terminal"}, {"iterm", "iTerm"},
            {"iterm2", "iTerm"}, {"finder", "Finder"},
            {"notes", "Notes"}, {"messages", "Messages"},
            {"facetime", "FaceTime"}, {"mail", "Mail"},
            {"maps", "Maps"}, {"music", "Music"},
            {"photos", "Photos"}, {"calendar", "Calendar"},
            {"reminders", "Reminders"}, {"calculator", "Calculator"},
            {"preview", "Preview"}, {"textedit", "TextEdit"},
            {"text edit", "TextEdit"}, {"books", "Books"},
            {"news", "News"}, {"stocks", "Stocks"},
            {"weather", "Weather"}, {"clock", "Clock"},
            {"activity monitor", "Activity Monitor"},
            {"disk utility", "Disk Utility"},
            {"system settings", "System Settings"},
            {"system preferences", "System Preferences"},
            {"vscode", "Visual Studio Code"},
            {"vs code", "Visual Studio Code"},
            {"visual studio code", "Visual Studio Code"},
            {"xcode", "Xcode"}, {"slack", "Slack"},
            {"discord", "Discord"}, {"spotify", "Spotify"},
            {"telegram", "Telegram"}, {"whatsapp", "WhatsApp"},
            {"zoom", "zoom.us"}, {"teams", "Microsoft Teams"},
            {"microsoft teams", "Microsoft Teams"},
            {"word", "Microsoft Word"}, {"excel", "Microsoft Excel"},
            {"powerpoint", "Microsoft PowerPoint"},
            {"notion", "Notion"}, {"obsidian", "Obsidian"},
            {"figma", "Figma"}, {"keynote", "Keynote"},
            {"pages", "Pages"}, {"numbers", "Numbers"},
            {"garageband", "GarageBand"}, {"imovie", "iMovie"},
        };

        auto it = aliases.find(after);
        if (it != aliases.end()) return AppMatch{it->second};

        // Title-case unknown app names
        std::string titled;
        bool cap_next = true;
        for (char c : after) {
            if (c == ' ') { titled += ' '; cap_next = true; }
            else { titled += cap_next ? static_cast<char>(toupper(c)) : c; cap_next = false; }
        }
        return AppMatch{titled};
    }

    // ── App close detection (deterministic fast-path) ───────────

    /**
     * Detect "close [app]" / "quit [app]" and extract the app name.
     * Returns std::nullopt for "close window", "close tab", "close this"
     * (those are handled by the shortcut map).
     */
    std::optional<AppMatch> extract_app_close(const std::string& text) const {
        std::string t = lower(trim(text));

        static const std::vector<std::string> close_prefixes = {
            "can you close ", "please close ", "could you close ",
            "can you quit ", "please quit ",
            "close ", "quit ", "exit ",
        };
        std::string after;
        bool found = false;
        for (const auto& p : close_prefixes) {
            if (starts_with(t, p)) {
                after = t.substr(p.size());
                found = true;
                break;
            }
        }
        if (!found) return std::nullopt;

        // Strip articles
        if (starts_with(after, "the ")) after = after.substr(4);
        if (starts_with(after, "a "))   after = after.substr(2);

        while (!after.empty() && after.back() == ' ') after.pop_back();
        if (after.empty()) return std::nullopt;

        // These go to the shortcut map, not here
        static const std::vector<std::string> not_app = {
            "window", "tab", "this", "it", "that", "all",
            "everything", "app", "application",
        };
        for (const auto& na : not_app)
            if (after == na || starts_with(after, na + " ")) return std::nullopt;

        // Too many words → complex command → LLM
        int wc = 1;
        for (char c : after) if (c == ' ') ++wc;
        if (wc > 3) return std::nullopt;

        // Alias resolution
        static const std::unordered_map<std::string, std::string> close_aliases = {
            {"safari", "Safari"}, {"chrome", "Google Chrome"},
            {"google chrome", "Google Chrome"}, {"firefox", "Firefox"},
            {"brave", "Brave Browser"}, {"terminal", "Terminal"},
            {"finder", "Finder"}, {"notes", "Notes"},
            {"messages", "Messages"}, {"mail", "Mail"},
            {"music", "Music"}, {"photos", "Photos"},
            {"calendar", "Calendar"}, {"vscode", "Visual Studio Code"},
            {"vs code", "Visual Studio Code"},
            {"slack", "Slack"}, {"discord", "Discord"},
            {"spotify", "Spotify"}, {"xcode", "Xcode"},
            {"activity monitor", "Activity Monitor"},
            {"system settings", "System Settings"},
        };

        auto it = close_aliases.find(after);
        if (it != close_aliases.end()) return AppMatch{it->second};

        // Title-case
        std::string titled;
        bool cap_next = true;
        for (char c : after) {
            if (c == ' ') { titled += ' '; cap_next = true; }
            else { titled += cap_next ? static_cast<char>(toupper(c)) : c; cap_next = false; }
        }
        return AppMatch{titled};
    }

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
            if (t.find(kw) != std::string::npos) return true;
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
            "compose", "draft an email", "draft a mail", "draft email",
            "write an email", "write a mail", "write email",
            "send an email", "send a mail", "send email",
            "reply to", "respond to",
            "click the", "click on", "tap the", "tap on",
            "fill in", "fill out", "fill the",
            "type into", "type in the",
            "navigate to", "go to ",
            "search for", "search on",
            "play on youtube", "play video",
            "add to cart", "buy ",
            "open gmail", "open youtube", "open amazon",
        };
        for (const auto& kw : force_tool)
            if (t.find(kw) != std::string::npos) return false;

        // Explicit tool triggers → NOT simple
        static const std::vector<std::string> triggers = {
            "open ", "launch ", "start ", "close ", "quit ", "exit",
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
            "search the web", "search online", "look up", "google ",
            "browse ", "read webpage", "read website", "fetch ",
            "remember ", "save memory", "recall ", "do you remember",
            "what did i tell", "what did i say",
            "scroll ", "drag ",
            "list windows", "what windows", "show windows", "all windows",
            "[file]",  // transcription normalizer tags file-related queries
        };
        for (const auto& tr : triggers)
            if (t.find(tr) != std::string::npos) return false;

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
            if (starts_with(t, p)) return true;

        // Ends with a question mark
        if (!t.empty() && t.back() == '?') return true;

        // Heuristic: if there are no OS-action verbs and it's
        // a conversational-length sentence, treat it as simple Q&A.
        // This catches things like "greet my friend", "tell him hi",
        // "my friend says hello" without hard-coding each phrase.
        {
            static const std::vector<std::string> action_verbs = {
                "open", "close", "click", "type", "press", "scroll",
                "search", "download", "upload", "save", "delete",
                "create", "move", "copy", "paste", "drag",
                "launch", "quit", "minimize", "maximize", "switch",
                "screenshot", "activate", "navigate", "browse",
            };
            bool has_action = false;
            for (const auto& v : action_verbs) {
                if (t.find(v) != std::string::npos) {
                    has_action = true;
                    break;
                }
            }
            if (!has_action) return true;
        }

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

// ── Macro system (PATH 1.8) — pre-built multi-step sequences ───────
//    These bypass the LLM entirely for common multi-step flows.
//    Each macro is a sequence of tool calls executed deterministically.

struct MacroStep {
    std::string tool;       // tool name
    std::string args_json;  // JSON args as string (will be parsed at runtime)
    int delay_ms;           // milliseconds to wait after this step
};

struct Macro {
    std::string description;        // spoken confirmation
    std::vector<MacroStep> steps;
};

/// Try to match a user command to a pre-built macro.
/// Returns the Macro if found, or std::nullopt.
inline std::optional<Macro> try_macro(const std::string& text) {
    std::string t = text;
    std::transform(t.begin(), t.end(), t.begin(), ::tolower);

    // ── Planning language guard ─────────────────────────────────
    // If the user is giving a multi-step instruction (e.g. "first open
    // downloads, then find the file named SIH"), suppress macro firing
    // and let the full input fall through to the Planner (PATH 2.5).
    {
        static const char* PLANNING_PHRASES[] = {
            "first ", "then ", " then ", "after that", " next ",
            "finally ", "and then", "followed by", "step ",
            "steps", "afterwards", "once you", "when you",
            "before ", " before ",
        };
        for (auto& p : PLANNING_PHRASES) {
            if (t.find(p) != std::string::npos)
                return std::nullopt;
        }
    }

    // ── File intent guard ───────────────────────────────────────
    // If the user mentions a folder AND a file action/type, they want
    // to do something with a file IN that folder → send to planner,
    // not macro.  "in the downloads folder, open the PDF named SIH"
    {
        static const char* FOLDER_REFS[] = {
            "downloads", "documents", "desktop", "folder",
        };
        static const char* FILE_ACTIONS[] = {
            "open", "find", "search", "look for", "show", "read", "locate",
        };
        static const char* FILE_TYPES[] = {
            "pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx",
            "file", "certificate", "txt", "zip", "mp4", "mp3",
            "named", "called",
        };

        bool has_folder = false;
        for (auto& fr : FOLDER_REFS) {
            if (t.find(fr) != std::string::npos) { has_folder = true; break; }
        }
        if (has_folder) {
            bool has_file_signal = false;
            for (auto& fa : FILE_ACTIONS) {
                if (t.find(fa) != std::string::npos) { has_file_signal = true; break; }
            }
            if (!has_file_signal) {
                for (auto& ft : FILE_TYPES) {
                    if (t.find(ft) != std::string::npos) { has_file_signal = true; break; }
                }
            }
            if (has_file_signal) {
                // Exception: simple "open downloads folder" / "open the downloads" should still macro
                // Only suppress if there's ALSO a file type or "named"/"called"
                bool has_file_type_or_name = false;
                static const char* SPECIFIC_FILE_SIGNALS[] = {
                    "pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx",
                    "file", "certificate", "txt", "zip", "named", "called",
                    "mp4", "mp3",
                };
                for (auto& s : SPECIFIC_FILE_SIGNALS) {
                    if (t.find(s) != std::string::npos) { has_file_type_or_name = true; break; }
                }
                if (has_file_type_or_name) {
                    return std::nullopt;  // suppress macro → goes to planner
                }
            }
        }
    }

    // Strip common prefixes
    static const char* prefixes[] = {
        "can you ", "could you ", "please ", "hey ", "demascas ",
    };
    for (auto& p : prefixes) {
        std::string ps(p);
        if (t.size() >= ps.size() && t.compare(0, ps.size(), ps) == 0)
            t = t.substr(ps.size());
    }

    // Trim whitespace
    while (!t.empty() && t.front() == ' ') t.erase(t.begin());
    while (!t.empty() && t.back() == ' ')  t.pop_back();

    // ── Gmail macro ─────────────────────────────────────────────
    if (t == "open gmail" || t == "check my email" || t == "check email"
        || t == "open my email" || t == "check my gmail") {
        return Macro{
            "Opening Gmail.",
            {
                {"open_url", R"({"url":"https://mail.google.com"})", 0},
            }
        };
    }

    // ── YouTube macro ───────────────────────────────────────────
    if (t == "open youtube" || t == "go to youtube") {
        return Macro{
            "Opening YouTube.",
            {
                {"open_url", R"({"url":"https://www.youtube.com"})", 0},
            }
        };
    }

    // ── New tab macro ───────────────────────────────────────────
    if (t == "new tab" || t == "open a new tab" || t == "open new tab") {
        return Macro{
            "New tab.",
            {
                {"press_keyboard_shortcut", R"({"keys":"command+t"})", 0},
            }
        };
    }

    // ── Close tab macro ─────────────────────────────────────────
    if (t == "close tab" || t == "close this tab" || t == "close the tab") {
        return Macro{
            "Tab closed.",
            {
                {"press_keyboard_shortcut", R"({"keys":"command+w"})", 0},
            }
        };
    }

    // ── Go back macro ───────────────────────────────────────────
    if (t == "go back" || t == "back" || t == "go back a page"
        || t == "previous page") {
        return Macro{
            "Going back.",
            {
                {"press_keyboard_shortcut", R"({"keys":"command+["})", 0},
            }
        };
    }

    // ── Open Downloads folder in Finder ─────────────────────────
    if (t.find("downloads") != std::string::npos &&
        (t.find("folder") != std::string::npos || t.find("open") != std::string::npos
         || t.find("finder") != std::string::npos || t == "downloads")) {
        return Macro{
            "Opening Downloads folder.",
            {
                {"open_application", R"({"app_name":"Finder"})", 500},
                {"press_keyboard_shortcut", R"({"keys":"command+option+l"})", 0},
            }
        };
    }

    // ── Open Documents folder in Finder ─────────────────────────
    if (t.find("documents") != std::string::npos &&
        (t.find("folder") != std::string::npos || t.find("open") != std::string::npos
         || t.find("finder") != std::string::npos)) {
        return Macro{
            "Opening Documents folder.",
            {
                {"open_application", R"({"app_name":"Finder"})", 500},
                {"press_keyboard_shortcut", R"({"keys":"command+shift+o"})", 0},
            }
        };
    }

    // ── Open Desktop folder in Finder ───────────────────────────
    if (t.find("desktop") != std::string::npos &&
        (t.find("folder") != std::string::npos || t.find("open") != std::string::npos
         || t.find("finder") != std::string::npos)) {
        return Macro{
            "Opening Desktop folder.",
            {
                {"open_application", R"({"app_name":"Finder"})", 500},
                {"press_keyboard_shortcut", R"({"keys":"command+shift+d"})", 0},
            }
        };
    }

    // ── Google search macro ─────────────────────────────────────
    // "google X", "search google for X", "search for X on google"
    {
        std::string query;
        if (t.rfind("google ", 0) == 0) {
            query = t.substr(7);
        } else if (t.find("search google for ") == 0) {
            query = t.substr(18);
        }
        if (!query.empty()) {
            // URL-encode spaces
            std::string encoded;
            for (char c : query) {
                if (c == ' ') encoded += '+';
                else encoded += c;
            }
            return Macro{
                "Searching Google for " + query + ".",
                {
                    {"open_url", R"({"url":"https://www.google.com/search?q=)" + encoded + R"("})", 0},
                }
            };
        }
    }

    return std::nullopt;
}

}  // namespace demascas
