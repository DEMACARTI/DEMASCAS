/**
 * DEMASCAS — daemon/src/ollama_client.cpp
 * Ollama HTTP integration with IST time, ReAct loop, memory injection,
 * and streaming with sentence-by-sentence TTS callbacks.
 */

#include "ollama_client.hpp"
#include "tool_bridge.hpp"
#include "config.hpp"

#include <algorithm>
#include <chrono>
#include <ctime>
#include <iostream>
#include <regex>
#include <sstream>

#include <curl/curl.h>

namespace demascas {

namespace cfg = config;
using json = nlohmann::json;

// ── IST time helpers (UTC+5:30) ─────────────────────────────────────

static std::string ist_time_str() {
    auto now = std::time(nullptr);
    std::tm utc{};
    gmtime_r(&now, &utc);
    // Add 5 h 30 m for IST
    utc.tm_hour += 5;
    utc.tm_min  += 30;
    std::mktime(&utc);   // normalise overflow
    char buf[32];
    std::strftime(buf, sizeof(buf), "%I:%M %p", &utc);
    return buf;
}

static std::string ist_date_str() {
    auto now = std::time(nullptr);
    std::tm utc{};
    gmtime_r(&now, &utc);
    utc.tm_hour += 5;
    utc.tm_min  += 30;
    std::mktime(&utc);
    char buf[64];
    std::strftime(buf, sizeof(buf), "%A, %d %B %Y", &utc);
    return buf;
}

// ── System prompts ──────────────────────────────────────────────────

json OllamaClient::build_system_prompt(const std::string& learned_context) const {
    // Build learned context block for injection
    std::string profile_block;
    if (!learned_context.empty()) {
        profile_block = "\n\n[LEARNED CONTEXT]\n" + learned_context + "\n";
    }

    return {
        {"role", "system"},
        {"content",
            "You are DEMASCAS, an intelligent local macOS AI assistant.\n"
            "Current date: " + ist_date_str() + "\n"
            "Current time (IST): " + ist_time_str() + "\n\n"
            "RULES:\n"
            "1. ANSWER QUESTIONS DIRECTLY — no tools for knowledge/time/math/jokes.\n"
            "2. OPEN APPS only when user says 'open'/'launch'/'start'.\n"
            "3. CLOSING APPS vs WINDOWS:\n"
            "   'close [AppName]' → quit_application(app_name)\n"
            "   'close window/tab/this' → press_keyboard_shortcut('command+w')\n"
            "   'close app' (no name) → press_keyboard_shortcut('command+q') on frontmost\n"
            "4. MULTI-WINDOW: Use list_running_applications or list_open_windows.\n"
            "   Use activate_application to switch focus. get_active_window_tree(app_name) to scan.\n"
            "5. USE SHORTCUTS — prefer press_keyboard_shortcut for generic actions:\n"
            "   minimize=command+m | new tab=command+t | save=command+s\n"
            "6. READ SCREEN BEFORE CLICKING — call get_active_window_tree first.\n"
            "7. FILES — summarize_latest_pdf for PDFs, list_files_in_folder for listing.\n"
            "8. WEB — search_web for online queries, read_webpage for specific URLs.\n"
            "9. MEMORY — save_memory when user says 'remember'. retrieve_memories for recall.\n"
            "10. SCROLL & DRAG — scroll_direction for scrolling, drag_element for drag-and-drop.\n"
            "11. EXACTNESS — use element names EXACTLY from the UI tree.\n"
            "12. SPOKEN OUTPUT — 1-3 sentences, no markdown, no URLs, no code blocks.\n"
            "    Be concise, natural, conversational — like JARVIS from Iron Man.\n"
            "13. LEARNING — you have a persistent memory across sessions. Use the [LEARNED CONTEXT] "
            "to personalize responses. Address the user by name if known. "
            "When you see similar past commands, use what worked before. "
            "If corrections appear, avoid repeating the same mistake."
            + profile_block
        }
    };
}

json OllamaClient::build_simple_prompt() const {
    return {
        {"role", "system"},
        {"content",
            "You are DEMASCAS, an AI assistant. "
            "Current date: " + ist_date_str() + ". "
            "Current time (IST): " + ist_time_str() + ". "
            "Answer concisely in 1-3 spoken sentences. No markdown, no code blocks. "
            "Be natural and conversational — like JARVIS."
        }
    };
}

// ── Ollama HTTP call ────────────────────────────────────────────────

json OllamaClient::call_ollama(const json& messages,
                                bool use_tools, bool fast) {
    auto t0 = std::chrono::steady_clock::now();

    int ctx = fast ? cfg::NUM_CTX_FAST  : cfg::NUM_CTX_TOOLS;
    int pred = fast ? cfg::NUM_PREDICT_FAST : cfg::NUM_PREDICT_TOOLS;

    json body = {
        {"model",      cfg::OLLAMA_MODEL},
        {"messages",   messages},
        {"stream",     false},
        {"keep_alive", cfg::KEEP_ALIVE},
        {"options", {
            {"num_ctx",     ctx},
            {"num_predict", pred},
            {"num_batch",   cfg::NUM_BATCH},
            {"temperature", 0}
        }}
    };

    if (use_tools && !tool_schemas_.empty())
        body["tools"] = tool_schemas_;

    auto resp_str = ToolBridge::http_post(cfg::OLLAMA_URL, body.dump());

    float elapsed = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[⏱️  Ollama responded in " << elapsed << " s]\n";

    if (resp_str.empty())
        return {{"role", "assistant"}, {"content", ""}};

    try {
        auto j = json::parse(resp_str);
        return j.at("message");
    } catch (const std::exception& e) {
        std::cerr << "[!] Ollama parse error: " << e.what() << "\n";
        return {{"role", "assistant"}, {"content", ""}};
    }
}

// ── History management ──────────────────────────────────────────────

void OllamaClient::trim_history() {
    if (static_cast<int>(history_.size()) > cfg::MAX_HISTORY)
        history_.erase(history_.begin(),
                       history_.end() - cfg::MAX_HISTORY);
}

void OllamaClient::reset() { history_.clear(); }

void OllamaClient::set_tools(const json& schemas) {
    tool_schemas_ = schemas;
}

// ── Prewarm ─────────────────────────────────────────────────────────

void OllamaClient::prewarm() {
    std::cout << "[*] Prewarming " << cfg::OLLAMA_MODEL
              << " (~2 GB into GPU RAM)...\n";
    auto t0 = std::chrono::steady_clock::now();

    json body = {
        {"model",      cfg::OLLAMA_MODEL},
        {"messages",   json::array({{{"role","user"},{"content","hi"}}})},
        {"stream",     false},
        {"keep_alive", cfg::KEEP_ALIVE},
        {"options", {
            {"num_ctx", cfg::NUM_CTX_TOOLS},
            {"num_predict", 1},
            {"num_batch", 1}
        }}
    };
    ToolBridge::http_post(cfg::OLLAMA_URL, body.dump());

    float s = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[✓] Model prewarmed in " << s << " s — ready.\n";
}

// ── Fast chat (no tools) ────────────────────────────────────────────

std::string OllamaClient::fast_chat(const std::string& user_msg) {
    trim_history();
    history_.push_back({{"role", "user"}, {"content", user_msg}});

    json messages;
    messages.push_back(build_simple_prompt());

    int start = std::max(0, static_cast<int>(history_.size()) - cfg::MAX_HISTORY);
    for (int i = start; i < static_cast<int>(history_.size()); ++i)
        messages.push_back(history_[i]);

    auto msg = call_ollama(messages, /*use_tools=*/false, /*fast=*/true);
    history_.push_back(msg);
    return msg.value("content", "");
}

// ── Full tool-calling chat (ReAct loop) ─────────────────────────────

std::string OllamaClient::chat_with_tools(const std::string& user_msg,
                                            ToolBridge& bridge,
                                            const std::string& learned_context) {
    trim_history();
    last_tools_.clear();
    last_path_ = "tool";

    // ── Memory auto-retrieval (best-effort, never blocks main flow) ─
    std::string enriched = user_msg;
    try {
        auto mem = bridge.execute("retrieve_memories",
                                   {{"query", user_msg}, {"top_k", 3}});
        if (!mem.empty() &&
            mem.find("No ") == std::string::npos &&
            mem.find("[-]") == std::string::npos) {
            enriched += "\n\n[MEMORY CONTEXT]\n" + mem;
            std::cout << "[🧠 MEMORY] Injected context from long-term memory.\n";
        }
    } catch (...) {}

    // ── Inject learned context from past interactions ────────────
    if (!learned_context.empty()) {
        enriched += "\n\n[LEARNED CONTEXT]\n" + learned_context;
        std::cout << "[🧠 LEARN] Injected learned context from past interactions.\n";
    }

    history_.push_back({{"role", "user"}, {"content", enriched}});

    // Build full message list with system prompt (includes user profile)
    auto sys = build_system_prompt(learned_context);
    json messages;
    messages.push_back(sys);
    for (auto& m : history_) messages.push_back(m);

    auto msg = call_ollama(messages, /*use_tools=*/true, /*fast=*/false);
    history_.push_back(msg);

    // ── ReAct loop — execute tool calls, feed results back ──────
    int depth = 0;
    while (msg.contains("tool_calls") && !msg["tool_calls"].empty()
           && depth < cfg::MAX_TOOL_DEPTH) {
        ++depth;

        for (auto& tc : msg["tool_calls"]) {
            std::string fn   = tc["function"]["name"].get<std::string>();
            json        args = tc["function"].value("arguments", json::object());

            std::cout << "[TOOL CALL " << depth << "] "
                      << fn << "(" << args.dump() << ")\n";

            // Track tool usage for learning
            last_tools_.push_back(fn);

            auto result = bridge.execute(fn, args);
            std::cout << "[TOOL RESULT] " << result << "\n";

            history_.push_back({
                {"role", "tool"},
                {"content", result},
                {"name", fn}
            });
        }

        // Re-send full history for next round
        messages.clear();
        messages.push_back(sys);
        for (auto& m : history_) messages.push_back(m);

        msg = call_ollama(messages, /*use_tools=*/true, /*fast=*/false);
        history_.push_back(msg);
    }

    if (depth >= cfg::MAX_TOOL_DEPTH)
        std::cout << "[!] Hit max tool depth (" << cfg::MAX_TOOL_DEPTH << ").\n";

    return msg.value("content", "Done.");
}

// ── Sentence boundary detection ─────────────────────────────────────

static const int MIN_SENTENCE = 15;  // chars before allowing a split

/// Split buffer at the last sentence boundary.
/// Returns {sentence, remainder}.  If no boundary → {"", buffer}.
static std::pair<std::string, std::string> split_sentence(const std::string& buf) {
    // Look for sentence-ending punctuation followed by space or end
    static const std::regex re(R"([.!?]\s|[.!?]$|\n)");
    std::pair<std::string, std::string> result{"", buf};

    auto begin = std::sregex_iterator(buf.begin(), buf.end(), re);
    auto end   = std::sregex_iterator();
    std::sregex_iterator last_match;
    bool found = false;

    for (auto it = begin; it != end; ++it) {
        if (static_cast<int>(it->position() + it->length()) >= MIN_SENTENCE) {
            last_match = it;
            found = true;
        }
    }

    if (found) {
        int pos = static_cast<int>(last_match->position() + last_match->length());
        std::string sentence = buf.substr(0, pos);
        // Trim
        while (!sentence.empty() && (sentence.back() == ' ' || sentence.back() == '\n'))
            sentence.pop_back();
        result = {sentence, buf.substr(pos)};
    }
    return result;
}

// ── Streaming HTTP with libcurl ─────────────────────────────────────

struct StreamCtx {
    SentenceCallback on_sentence;
    std::atomic<bool>* cancel;
    std::string buffer;          // token accumulator for sentence detection
    std::string full_text;       // all generated text
    json tool_calls = json::array();
    std::string line_buffer;     // for parsing NDJSON lines
};

static size_t stream_write_cb(void* ptr, size_t size, size_t nmemb,
                               void* userdata) {
    auto* ctx = static_cast<StreamCtx*>(userdata);
    if (ctx->cancel && ctx->cancel->load()) return 0;  // abort transfer

    size_t bytes = size * nmemb;
    ctx->line_buffer.append(static_cast<char*>(ptr), bytes);

    // Ollama streams NDJSON — one JSON object per line
    std::string::size_type pos;
    while ((pos = ctx->line_buffer.find('\n')) != std::string::npos) {
        std::string line = ctx->line_buffer.substr(0, pos);
        ctx->line_buffer.erase(0, pos + 1);

        if (line.empty()) continue;
        try {
            auto j = json::parse(line);
            auto msg = j.value("message", json::object());

            // Tool calls
            if (msg.contains("tool_calls") && !msg["tool_calls"].empty()) {
                for (auto& tc : msg["tool_calls"])
                    ctx->tool_calls.push_back(tc);
                continue;
            }

            // Text token
            std::string token = msg.value("content", "");
            if (token.empty()) continue;

            ctx->buffer += token;
            ctx->full_text += token;

            // Check for sentence boundary
            auto [sentence, remainder] = split_sentence(ctx->buffer);
            if (!sentence.empty()) {
                ctx->on_sentence(sentence);
                ctx->buffer = remainder;
            }
        } catch (...) {
            // Skip unparseable lines (progress, etc.)
        }
    }

    return bytes;
}

std::pair<std::string, json>
OllamaClient::call_ollama_streaming(const json& messages,
                                     SentenceCallback on_sentence,
                                     std::atomic<bool>& cancel,
                                     bool use_tools, bool fast) {
    auto t0 = std::chrono::steady_clock::now();

    int ctx = fast ? cfg::NUM_CTX_FAST  : cfg::NUM_CTX_TOOLS;
    int pred = fast ? cfg::NUM_PREDICT_FAST : cfg::NUM_PREDICT_TOOLS;

    json body = {
        {"model",      cfg::OLLAMA_MODEL},
        {"messages",   messages},
        {"stream",     true},
        {"keep_alive", cfg::KEEP_ALIVE},
        {"options", {
            {"num_ctx",     ctx},
            {"num_predict", pred},
            {"num_batch",   cfg::NUM_BATCH},
            {"temperature", 0}
        }}
    };

    if (use_tools && !tool_schemas_.empty())
        body["tools"] = tool_schemas_;

    StreamCtx sctx;
    sctx.on_sentence = on_sentence;
    sctx.cancel = &cancel;

    CURL* curl = curl_easy_init();
    if (curl) {
        struct curl_slist* hdrs = nullptr;
        hdrs = curl_slist_append(hdrs, "Content-Type: application/json");
        std::string body_str = body.dump();

        curl_easy_setopt(curl, CURLOPT_URL,           cfg::OLLAMA_URL.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER,     hdrs);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS,     body_str.c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION,  stream_write_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA,      &sctx);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT,        60L);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL,       1L);

        curl_easy_perform(curl);
        curl_slist_free_all(hdrs);
        curl_easy_cleanup(curl);
    }

    // Flush remaining buffer
    if (!sctx.buffer.empty() && !cancel.load()) {
        // Trim
        auto& b = sctx.buffer;
        while (!b.empty() && (b.back() == ' ' || b.back() == '\n'))
            b.pop_back();
        if (!b.empty()) on_sentence(b);
    }

    float elapsed = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[⏱️  Ollama streamed in " << elapsed << " s]\n";

    return {sctx.full_text, sctx.tool_calls};
}

// ── Streaming fast chat ─────────────────────────────────────────────

std::string OllamaClient::fast_chat_streaming(const std::string& user_msg,
                                               SentenceCallback on_sentence,
                                               std::atomic<bool>& cancel) {
    trim_history();
    history_.push_back({{"role", "user"}, {"content", user_msg}});

    json messages;
    messages.push_back(build_simple_prompt());

    int start = std::max(0, static_cast<int>(history_.size()) - cfg::MAX_HISTORY);
    for (int i = start; i < static_cast<int>(history_.size()); ++i)
        messages.push_back(history_[i]);

    auto [text, _tools] = call_ollama_streaming(
        messages, on_sentence, cancel, false, true);

    history_.push_back({{"role", "assistant"}, {"content", text}});
    return text;
}

// ── Streaming tool-calling chat ─────────────────────────────────────

std::string OllamaClient::chat_with_tools_streaming(
    const std::string& user_msg,
    ToolBridge& bridge,
    SentenceCallback on_sentence,
    std::atomic<bool>& cancel,
    const std::string& learned_context)
{
    trim_history();
    last_tools_.clear();
    last_path_ = "tool";

    // Memory auto-retrieval
    std::string enriched = user_msg;
    try {
        auto mem = bridge.execute("retrieve_memories",
                                   {{"query", user_msg}, {"top_k", 3}});
        if (!mem.empty() &&
            mem.find("No ") == std::string::npos &&
            mem.find("[-]") == std::string::npos) {
            enriched += "\n\n[MEMORY CONTEXT]\n" + mem;
            std::cout << "[🧠 MEMORY] Injected context.\n";
        }
    } catch (...) {}

    // Learned context
    if (!learned_context.empty()) {
        enriched += "\n\n[LEARNED CONTEXT]\n" + learned_context;
        std::cout << "[🧠 LEARN] Injected learned context.\n";
    }

    history_.push_back({{"role", "user"}, {"content", enriched}});

    auto sys = build_system_prompt(learned_context);
    json messages;
    messages.push_back(sys);
    for (auto& m : history_) messages.push_back(m);

    // First streaming call — may produce tool_calls or text
    auto [text, tool_calls] = call_ollama_streaming(
        messages, on_sentence, cancel, true, false);

    history_.push_back({{"role", "assistant"}, {"content", text}});
    if (!tool_calls.empty())
        history_.back()["tool_calls"] = tool_calls;

    // ReAct loop
    int depth = 0;
    while (!tool_calls.empty() && depth < cfg::MAX_TOOL_DEPTH && !cancel.load()) {
        ++depth;
        for (auto& tc : tool_calls) {
            std::string fn   = tc["function"]["name"].get<std::string>();
            json        args = tc["function"].value("arguments", json::object());
            std::cout << "[TOOL CALL " << depth << "] " << fn << "(" << args.dump() << ")\n";
            last_tools_.push_back(fn);

            auto result = bridge.execute(fn, args);
            std::cout << "[TOOL RESULT] " << result << "\n";
            history_.push_back({{"role", "tool"}, {"content", result}, {"name", fn}});
        }

        messages.clear();
        messages.push_back(sys);
        for (auto& m : history_) messages.push_back(m);

        auto [next_text, next_tools] = call_ollama_streaming(
            messages, on_sentence, cancel, true, false);

        text = next_text;
        tool_calls = next_tools;
        history_.push_back({{"role", "assistant"}, {"content", text}});
        if (!tool_calls.empty())
            history_.back()["tool_calls"] = tool_calls;
    }

    return text.empty() ? "Done." : text;
}

}  // namespace demascas
