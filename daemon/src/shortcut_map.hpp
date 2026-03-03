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
        std::optional<std::string> best;
        size_t best_len = 0;

        for (const auto& [phrase, keys] : s_) {
            auto pwords = split_vec(phrase);
            if (pwords.size() == 1) {
                // Single-word shortcut: must be a standalone token
                if (words_set.count(pwords[0]) && 1 > best_len) {
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

    // ── Simple question detection ───────────────────────────────

    /**
     * Return true if the command is a conversational/knowledge question
     * that needs NO OS tools — the LLM can answer from its own weights.
     */
    bool is_simple_question(const std::string& text) const {
        std::string t = lower(trim(text));

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
        };
        for (const auto& tr : triggers)
            if (t.find(tr) != std::string::npos) return false;

        // Matches a question/conversational prefix
        static const std::vector<std::string> prefixes = {
            "what", "who", "when", "where", "why", "how",
            "is ", "are ", "do ", "does ", "can ", "could ",
            "would ", "will ",
            "tell me", "explain", "define", "describe", "calculate",
            "what's", "whats", "how's", "hows",
            "thank", "thanks", "hello", "hi ", "hey ", "good ",
            "name a", "list ", "give me",
            "say ", "sing ", "joke", "story", "poem", "quote",
            "meaning of", "convert ", "translate ",
        };
        for (const auto& p : prefixes)
            if (starts_with(t, p)) return true;

        // Ends with a question mark
        if (!t.empty() && t.back() == '?') return true;

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

}  // namespace demascas
