/**
 * DEMASCAS — daemon/src/llm_client.cpp
 * MLX LLM integration with IST time, pure-JSON tool-calling,
 * ReAct loop, memory injection, and SSE streaming with
 * sentence-by-sentence TTS callbacks.
 *
 * API protocol (OpenAI-compatible):
 *   POST  http://127.0.0.1:8081/v1/chat/completions
 *   Body: {"model":"...","messages":[...],"stream":true,"max_tokens":N}
 *   SSE:  data: {"choices":[{"delta":{"content":"token"}}]}
 *         data: [DONE]
 *
 * Tool-calling is pure-JSON — the model outputs one of:
 *   {"tool":"tool_name","args":{"key":"value"}}   ← call a tool
 *   {"done":true,"say":"spoken text here"}         ← final answer
 */

#include "llm_client.hpp"
#include "tool_bridge.hpp"
#include "config.hpp"

#include <algorithm>
#include <chrono>
#include <ctime>
#include <fstream>
#include <iostream>
#include <regex>
#include <set>
#include <sstream>
#include <thread>

#include <curl/curl.h>
#include <sys/stat.h>

namespace demascas {

namespace cfg = config;
using json = nlohmann::json;

static constexpr size_t LEARNED_CONTEXT_MAX_CHARS = 1200;

// ── IST time helpers (UTC+5:30) ─────────────────────────────────────

static std::string ist_time_str() {
    auto now = std::time(nullptr);
    std::tm utc{};
    gmtime_r(&now, &utc);
    utc.tm_hour += 5;
    utc.tm_min  += 30;
    std::mktime(&utc);
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

// ── Format tool schemas as compact text ─────────────────────────────

std::string LLMClient::format_tools_as_text(const json& schemas) const {
    // Produces a compact tool list for the system prompt:
    //   - tool_name(param1, param2) — description
    std::string out;
    for (auto& s : schemas) {
        auto fn = s.value("function", s);  // handle both wrapped and unwrapped
        std::string name = fn.value("name", "?");
        std::string desc = fn.value("description", "");

        // Extract parameter names
        std::vector<std::string> params;
        if (fn.contains("parameters") && fn["parameters"].contains("properties")) {
            for (auto& [key, _val] : fn["parameters"]["properties"].items()) {
                params.push_back(key);
            }
        }

        // The tool list changes per command, so it is NOT reused from the
        // runtime's prompt cache — every character here is prefill time on
        // every command.  Keep just the first sentence of each description
        // (the full text was ~2.6k chars / ~750 tokens for 8 tools).
        constexpr size_t MAX_DESC = 160;
        std::string short_desc = desc;
        for (size_t k = 0; k + 1 < short_desc.size(); ++k) {
            if ((short_desc[k] == '.' || short_desc[k] == '!' || short_desc[k] == '?') &&
                short_desc[k + 1] == ' ' && k >= 20) {
                short_desc = short_desc.substr(0, k + 1);
                break;
            }
        }
        if (short_desc.size() > MAX_DESC) short_desc = short_desc.substr(0, MAX_DESC - 1) + "…";

        out += "- " + name + "(";
        for (size_t i = 0; i < params.size(); ++i) {
            if (i > 0) out += ", ";
            out += params[i];
        }
        out += ")";
        if (!short_desc.empty()) out += " — " + short_desc;
        out += "\n";
    }
    return out;
}

// ── System prompts ──────────────────────────────────────────────────

json LLMClient::build_tool_prompt(const json& tool_schemas,
                                    ToolBridge& bridge,
                                    const std::string& learned_context) const {
    std::string tools_text = format_tools_as_text(tool_schemas);

    // ── STATIC CONTENT (stable across turns — maximises KV cache reuse) ──
    std::string static_content =
        "You are DEMASCAS, a macOS voice assistant.\n\n"
        "RULES:\n"
        "1. To ACT on macOS (click, type, scroll, open, read screen, manage files) → CALL a tool. "
        "Text alone does nothing. NEVER say \"I will click\" — just CALL the tool.\n"
        "2. For multi-step tasks: call tools ONE AT A TIME. After each tool result, "
        "decide the next step. NEVER repeat the same tool call with the same arguments.\n"
        "3. Use ONLY tool names from AVAILABLE TOOLS exactly as written. "
        "Never invent names like click, click_element, switch_application, etc.\n"
        "4. If no listed tool can perform the action, return done JSON explaining the limitation.\n"
        "5. Questions, math, jokes, greetings, conversation → answer directly with {\"done\":true,\"say\":\"...\"}. "
        "You are a character — speak DIRECTLY, never say 'you can say X'. If asked to greet someone, YOU greet them.\n"
        "6. Reply in 1-2 spoken sentences. No markdown, no URLs, no code.\n"
        "7. To open a WEBSITE, use open_url — NOT open_application + type_text. "
        "For example: open Google → {\"tool\":\"open_url\",\"args\":{\"url\":\"https://google.com\"}}\n"
        "8. ELEMENT NAMING: Use EXACT visible labels from the accessibility tree, NOT snake_case. "
        "If a click fails, call get_active_window_tree to read actual element names. "
        "Example: use 'Compose' not 'compose_button', 'Send' not 'send_button'.\n"
        "9. WEB AGENT: When Brave Browser is the active app, prefer browser_* tools over "
        "click_ui_element/type_text for web interaction. Use browser_click_text or web_smart_click "
        "for clicking web elements, browser_type for filling fields. For CAPTCHAs, paywalls, or "
        "login walls, REPORT them with web_handle_obstacle — never try to bypass. "
        "NEVER auto-send emails — gmail_compose_draft fills the draft but does NOT send.\n"
        "10. BRAVE BROWSER LIMITATION: get_active_window_tree CANNOT see inside Brave Browser "
        "web page content. It only sees the browser chrome (toolbar, tabs). To inspect web "
        "page elements, use browser_get_dom_summary instead. If a click fails on a web page, "
        "call browser_get_dom_summary to see the actual elements.\n"
        "11. GMAIL: When the user says 'compose an email' or 'draft an email', call "
        "gmail_compose_draft DIRECTLY — do NOT try to click 'Compose' button manually. "
        "gmail_compose_draft handles opening compose + filling To/Subject/Body in one call.\n"
        "12. EMAIL COLLECTION: If the user says 'email X' but doesn't provide the email address, "
        "ask them for it with {\"done\":true,\"say\":\"What's their email address?\"}. "
        "Do NOT guess email addresses.\n"
        "13. EXPLICIT LEARNING: If the user explicitly asks you to remember a rule, preference, "
        "or fact (e.g. 'always use Brave', 'remember I like X'), use set_preference or save_memory. "
        "DO NOT execute the action they mention unless they also tell you to do it right now.\n\n"
        "OUTPUT FORMAT — reply with EXACTLY ONE JSON object per turn:\n"
        "  To call a tool:    {\"tool\":\"tool_name\",\"args\":{\"key\":\"value\"}}\n"
        "  When finished:     {\"done\":true,\"say\":\"spoken answer here\"}\n\n"
        "AVAILABLE TOOLS:\n" + tools_text;

    // ── DYNAMIC CONTENT (changes every turn — placed at the bottom) ──
    std::string dynamic_content = "\n--- CURRENT CONTEXT ---\n";
    dynamic_content += "Date: " + ist_date_str() + "\n";
    dynamic_content += "Time: " + ist_time_str() + " IST\n";

    if (!learned_context.empty()) {
        std::string trimmed = learned_context;
        if (trimmed.size() > LEARNED_CONTEXT_MAX_CHARS) {
            trimmed = trimmed.substr(0, LEARNED_CONTEXT_MAX_CHARS);
            trimmed += "\n... [context truncated]";
        }
        dynamic_content += "\n[CONTEXT]\n" + trimmed;
    }

    // Preferences block contains user rules and humor personality
    try {
        std::string prefs_block = bridge.fetch_preferences();
        if (!prefs_block.empty()) {
            dynamic_content += "\n" + prefs_block;
        }
    } catch (...) {}

    return {{"role", "system"}, {"content", static_content + dynamic_content}};
}

json LLMClient::build_simple_prompt(ToolBridge& bridge) const {
    // ── STATIC CONTENT (stable across turns — maximises KV cache reuse) ──
    std::string static_content =
        "You are DEMASCAS, a personal AI voice assistant — like JARVIS from Iron Man. "
        "Answer concisely in 1-3 spoken sentences. No markdown, no code blocks.\n\n"
        "GENERAL GUIDELINES:\n"
        "- You are a CHARACTER with your own voice. You speak DIRECTLY to the user and anyone present.\n"
        "- Never say 'you can say X' or 'try saying X'. YOU are the one talking.\n"
        "- If the user says 'greet my friend', YOU greet them. If they say 'tell a joke', YOU tell it.\n"
        "- Be warm, confident, and slightly witty. Show personality.\n"
        "- Understand context: if someone is introduced to you, acknowledge THEM by name.\n"
        "- The user is Daksh, your creator. Treat anyone they introduce as a friend.\n\n"
        "IMPORTANT: You are on the FAST Q&A path — you have NO tools. "
        "If the user asks you to compose an email, click something, fill a form, "
        "open a website, or take any OS action, say 'Let me handle that for you' "
        "and the system will re-route to the tool path. NEVER pretend to perform actions.\n";

    // ── DYNAMIC CONTENT (changes every turn — placed at the bottom) ──
    std::string dynamic_content = "\n--- CURRENT CONTEXT ---\n";
    dynamic_content += "Current date: " + ist_date_str() + "\n";
    dynamic_content += "Current time (IST): " + ist_time_str() + "\n";

    try {
        std::string prefs_block = bridge.fetch_preferences();
        if (!prefs_block.empty()) {
            dynamic_content += "\n" + prefs_block + "\n";
        }
    } catch (...) {}

    return {
        {"role", "system"},
        {"content", static_content + dynamic_content}
    };
}

// ── Sentence boundary detection ─────────────────────────────────────

static std::pair<std::string, std::string> split_sentence(const std::string& buf) {
    constexpr int MIN_SENTENCE = 10;
    constexpr int MIN_CLAUSE   = 30;
    int n = static_cast<int>(buf.size());

    int last_sentence_end = -1;
    int last_clause_end   = -1;

    for (int i = 0; i < n; ++i) {
        char c = buf[i];
        if (c == '\n') {
            int pos = i + 1;
            if (pos >= MIN_SENTENCE) last_sentence_end = pos;
        }
        else if (c == '.' || c == '!' || c == '?') {
            int pos = i + 1;
            if (pos >= n || buf[pos] == ' ' || buf[pos] == '\n') {
                if (pos < n) ++pos;
                if (pos >= MIN_SENTENCE) last_sentence_end = pos;
            }
        }
        else if (c == ',' || c == ';' || c == ':') {
            int pos = i + 1;
            if (pos < n && buf[pos] == ' ') {
                pos++;
                if (pos >= MIN_CLAUSE) last_clause_end = pos;
            }
        }
    }

    int split_at = last_sentence_end >= 0 ? last_sentence_end : last_clause_end;
    if (split_at < 0) return {"", buf};

    std::string sentence = buf.substr(0, split_at);
    while (!sentence.empty() && (sentence.back() == ' ' || sentence.back() == '\n'))
        sentence.pop_back();
    return {sentence, buf.substr(split_at)};
}

// ── SSE streaming HTTP with libcurl ─────────────────────────────────

struct SSECtx {
    SentenceCallback on_sentence;
    std::atomic<bool>* cancel;
    std::string buffer;        // token accumulator for sentence detection
    std::string full_text;     // all generated text
    std::string line_buffer;   // for parsing SSE lines
};

/// libcurl write callback for SSE (Server-Sent Events).
/// MLX format: data: {"choices":[{"delta":{"content":"token"}}]}
/// Final:      data: [DONE]
static size_t sse_write_cb(void* ptr, size_t size, size_t nmemb,
                            void* userdata) {
    auto* ctx = static_cast<SSECtx*>(userdata);
    if (ctx->cancel && ctx->cancel->load()) return 0;

    size_t bytes = size * nmemb;
    ctx->line_buffer.append(static_cast<char*>(ptr), bytes);

    // Process complete lines
    std::string::size_type pos;
    while ((pos = ctx->line_buffer.find('\n')) != std::string::npos) {
        std::string line = ctx->line_buffer.substr(0, pos);
        ctx->line_buffer.erase(0, pos + 1);

        // Trim \r
        if (!line.empty() && line.back() == '\r') line.pop_back();

        // Skip empty lines (SSE separators)
        if (line.empty()) continue;

        // Must start with "data: "
        if (line.rfind("data: ", 0) != 0) continue;

        std::string payload = line.substr(6);

        // End-of-stream sentinel
        if (payload == "[DONE]") continue;

        try {
            auto j = json::parse(payload);

            // Extract content token from choices[0].delta.content
            if (j.contains("choices") && !j["choices"].empty()) {
                auto delta = j["choices"][0].value("delta", json::object());
                std::string token = delta.value("content", "");
                if (token.empty()) continue;

                ctx->buffer += token;
                ctx->full_text += token;

                // Check for sentence boundary
                auto [sentence, remainder] = split_sentence(ctx->buffer);
                if (!sentence.empty()) {
                    ctx->on_sentence(sentence);
                    ctx->buffer = remainder;
                }
            }
        } catch (...) {
            // Skip unparseable payloads
        }
    }

    return bytes;
}

// ── MLX HTTP calls ──────────────────────────────────────────────────

std::string LLMClient::call_llm_streaming(const json& messages,
                                           SentenceCallback on_sentence,
                                           std::atomic<bool>& cancel,
                                           int max_tokens,
                                           float temperature) {
    auto t0 = std::chrono::steady_clock::now();

    json body = {
        {"model",       model_},
        {"messages",    messages},
        {"stream",      true},
        {"max_tokens",  max_tokens},
        {"temperature", temperature},
        {"stop",        json::array({"<|endoftext|>", "<|im_end|>"})}
    };

    SSECtx sctx;
    sctx.on_sentence = on_sentence;
    sctx.cancel = &cancel;

    CURL* curl = curl_easy_init();
    if (curl) {
        struct curl_slist* hdrs = nullptr;
        hdrs = curl_slist_append(hdrs, "Content-Type: application/json");
        hdrs = curl_slist_append(hdrs, "Accept: text/event-stream");
        std::string body_str = body.dump();

        curl_easy_setopt(curl, CURLOPT_URL,           chat_url_.c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER,     hdrs);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS,     body_str.c_str());
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION,  sse_write_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA,      &sctx);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT,        60L);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL,       1L);

        CURLcode res = curl_easy_perform(curl);
        if (res != CURLE_OK && res != CURLE_WRITE_ERROR) {
            std::cerr << "[!] MLX curl error: " << curl_easy_strerror(res) << "\n";
        }

        long http_code = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
        if (http_code != 200 && http_code != 0) {
            std::cerr << "[!] MLX HTTP " << http_code << "\n";
        }

        curl_slist_free_all(hdrs);
        curl_easy_cleanup(curl);
    } else {
        std::cerr << "[!] curl_easy_init() failed\n";
    }

    // Flush remaining buffer
    if (!sctx.buffer.empty() && !cancel.load()) {
        auto& b = sctx.buffer;
        while (!b.empty() && (b.back() == ' ' || b.back() == '\n'))
            b.pop_back();
        if (!b.empty()) on_sentence(b);
    }

    float elapsed = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[MLX streamed in " << elapsed << " s]\n";

    if (sctx.full_text.empty()) {
        std::cerr << "[!] MLX returned EMPTY response — check server on port 8081.\n";
    }

    return sctx.full_text;
}

std::string LLMClient::call_llm_blocking(const json& messages,
                                          int max_tokens) {
    auto t0 = std::chrono::steady_clock::now();

    json body = {
        {"model",       model_},
        {"messages",    messages},
        {"stream",      false},
        {"max_tokens",  max_tokens},
        {"temperature", static_cast<double>(cfg::LLM_TEMPERATURE)},
        {"stop",        json::array({"<|endoftext|>", "<|im_end|>"})}
    };

    std::string resp = ToolBridge::http_post(chat_url_, body.dump());

    float elapsed = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[MLX blocked in " << elapsed << " s]\n";

    if (resp.empty()) return "";

    try {
        auto j = json::parse(resp);
        return j["choices"][0]["message"]["content"].get<std::string>();
    } catch (const std::exception& e) {
        std::cerr << "[!] MLX parse error: " << e.what() << "\n";
        return "";
    }
}

// ── Parse LLM action from raw text ──────────────────────────────────

/// Detect tool error prefixes in a tool result string.
/// Used by the ReAct loop to inject recovery guidance.
static bool is_tool_error(const std::string& result) {
    // Only look at how the result STARTS — content-bearing results (web
    // pages, file listings, search hits) often contain words like
    // "not found" or "Could not" without being failures.
    auto p = result.find_first_not_of(" \t\r\n");
    if (p == std::string::npos) return true;
    std::string head = result.substr(p, 24);
    std::transform(head.begin(), head.end(), head.begin(), ::tolower);
    static const char* ERROR_PREFIXES[] = {
        "[-]", "failed", "error", "timeout", "could not", "refused",
    };
    for (auto& prefix : ERROR_PREFIXES)
        if (head.rfind(prefix, 0) == 0) return true;

    // JSON results: {"success": false, ...}
    if (result[p] == '{') {
        try {
            auto j = json::parse(result);
            if (j.is_object()) {
                if (j.contains("success") && j["success"].is_boolean() && !j["success"].get<bool>())
                    return true;
                std::string msg = ToolBridge::json_str(j, "message");
                if (msg.rfind("[-]", 0) == 0) return true;
            }
        } catch (...) {}
    }
    return false;
}

/// Extract top-level JSON object slices from free-form model text.
/// This recovers from outputs like:
///   {..tool..},{..tool..}
/// or prose + fenced JSON.
static std::vector<std::string> extract_json_objects(const std::string& text) {
    std::vector<std::string> out;
    int depth = 0;
    int start = -1;
    bool in_string = false;
    bool escaping = false;

    for (int i = 0; i < static_cast<int>(text.size()); ++i) {
        char c = text[i];

        if (in_string) {
            if (escaping) {
                escaping = false;
                continue;
            }
            if (c == '\\') {
                escaping = true;
                continue;
            }
            if (c == '"') {
                in_string = false;
            }
            continue;
        }

        if (c == '"') {
            in_string = true;
            continue;
        }

        if (c == '{') {
            if (depth == 0) start = i;
            ++depth;
            continue;
        }

        if (c == '}' && depth > 0) {
            --depth;
            if (depth == 0 && start >= 0) {
                out.push_back(text.substr(start, i - start + 1));
                start = -1;
            }
        }
    }

    return out;
}

LLMAction LLMClient::parse_llm_action(const std::string& raw_text) const {
    LLMAction action;
    action.raw = raw_text;

    auto objs = extract_json_objects(raw_text);
    for (const auto& obj : objs) {
        try {
            auto j = json::parse(obj);

            if (j.contains("tool") && j["tool"].is_string()) {
                action.is_done = false;
                action.tool = j["tool"].get<std::string>();
                action.args = j.value("args", json::object());
                return action;
            }

            if (j.contains("done") && j.value("done", false)) {
                action.is_done = true;
                action.say = j.value("say", "");
                return action;
            }
        } catch (...) {
            // Try next extracted object
        }
    }

    // No parseable action JSON found — treat as spoken text.
    action.is_done = true;
    action.say = raw_text;

    return action;
}

// ── History management ──────────────────────────────────────────────

void LLMClient::trim_history() {
    if (static_cast<int>(history_.size()) > cfg::MAX_HISTORY)
        history_.erase(history_.begin(),
                       history_.end() - cfg::MAX_HISTORY);
}

void LLMClient::reset() { history_.clear(); }

void LLMClient::fit_history_to_context(const json& sys) {
    // ~3 characters per token is conservative for English + JSON, so the
    // estimate errs on the side of trimming rather than overflowing.
    const long reserve_tokens = cfg::TOOL_PREDICT + 96;
    const size_t budget_chars = static_cast<size_t>(
        std::max<long>(512, ctx_tokens_ - reserve_tokens) * 3);
    auto content = [](const json& m) -> std::string {
        return (m.contains("content") && m["content"].is_string())
                   ? m["content"].get<std::string>() : std::string();
    };
    auto total = [&]() {
        size_t n = content(sys).size();
        for (auto& m : history_) n += content(m).size();
        return n;
    };
    if (total() <= budget_chars) return;

    // 1) Drop screen-state dumps from all but the newest tool result.
    static const std::string SCREEN_TAG = "\n\n[SCREEN STATE AFTER ACTION]";
    for (size_t i = 0; i + 1 < history_.size() && total() > budget_chars; ++i) {
        std::string c = content(history_[i]);
        auto pos = c.find(SCREEN_TAG);
        if (pos != std::string::npos) history_[i]["content"] = c.substr(0, pos);
    }

    // 2) Drop the oldest assistant/tool-result pairs, but keep a one-line
    //    record of each inside the FIRST user message, so the model still
    //    knows what it already did (otherwise it repeats steps).  Merged into
    //    that message — not a new one — because some chat templates reject
    //    two consecutive user messages.
    static const std::string EARLIER = "\n\n[EARLIER STEPS — details trimmed to fit context]";
    while (total() > budget_chars && history_.size() > 3) {
        std::string call = content(history_[1]);
        std::string res = content(history_[2]);
        auto nl = res.find('\n');
        std::string body = nl == std::string::npos ? res : res.substr(nl + 1);
        bool failed = is_tool_error(body);
        std::string line = "\n- " + call.substr(0, 200) + " → " +
                           (failed ? "FAILED: " : "ok: ") +
                           body.substr(0, 80);
        std::replace(line.begin() + 1, line.end(), '\n', ' ');
        std::string first = content(history_[0]);
        if (first.find(EARLIER) == std::string::npos) first += EARLIER;
        history_[0]["content"] = first + line;
        history_.erase(history_.begin() + 1, history_.begin() + 3);
    }

    // 3) Still too big: shorten the newest tool result, then the first
    //    user message's injected context (never below the request itself).
    for (size_t idx : {history_.size() - 1, size_t{0}}) {
        if (total() <= budget_chars || idx >= history_.size()) continue;
        std::string c = content(history_[idx]);
        size_t over = total() - budget_chars;
        if (c.size() > over + 200)
            history_[idx]["content"] = c.substr(0, c.size() - over) + "\n…[trimmed to fit context]";
    }
}

// ── Session persistence ─────────────────────────────────────────

static std::string session_path(bool create_dir) {
    const char* home = std::getenv("HOME");
    std::string dir = std::string(home && *home ? home : "/tmp") + "/.demascas";
    if (create_dir) ::mkdir(dir.c_str(), 0700);
    return dir + "/" + cfg::SESSION_FILE_NAME;
}

void LLMClient::save_session() const {
    try {
        json arr = json::array();
        // Only persist the last SESSION_MAX_TURNS entries
        int start = std::max(0, static_cast<int>(history_.size()) - cfg::SESSION_MAX_TURNS);
        for (int i = start; i < static_cast<int>(history_.size()); ++i)
            arr.push_back(history_[i]);

        const std::string path = session_path(/*create_dir=*/true);
        std::ofstream f(path, std::ios::trunc);
        if (f.is_open()) {
            f << arr.dump(2);
            f.close();
            ::chmod(path.c_str(), 0600);  // conversation text is private
        }
    } catch (const std::exception& e) {
        std::cerr << "[!] Session save failed: " << e.what() << "\n";
    }
}

void LLMClient::load_session() {
    try {
        std::ifstream f(session_path(/*create_dir=*/false));
        if (!f.is_open()) return;

        std::string content((std::istreambuf_iterator<char>(f)),
                             std::istreambuf_iterator<char>());
        f.close();

        if (content.empty()) return;

        auto arr = json::parse(content);
        if (!arr.is_array()) return;

        history_.clear();
        for (auto& msg : arr) {
            if (msg.contains("role") && msg.contains("content"))
                history_.push_back(msg);
        }

        if (!history_.empty()) {
            std::cout << "[SESSION] Restored " << history_.size()
                      << " turns from previous session.\n";
        }
    } catch (const std::exception& e) {
        // Session file corrupt or missing — start fresh
        std::cerr << "[!] Session load failed (starting fresh): " << e.what() << "\n";
        history_.clear();
    }
}

// ── Prewarm ─────────────────────────────────────────────────────────

void LLMClient::configure(const std::string& chat_url, const std::string& model,
                          int context_tokens) {
    chat_url_ = chat_url;
    model_ = model;
    ctx_tokens_ = std::max(1024, context_tokens);
}

void LLMClient::prewarm(ToolBridge& bridge) {
    if (!configured()) {
        std::cerr << "[!] No language model is set up yet. Plug one in with:\n"
                  << "    python main.py setup\n";
        return;
    }
    std::cout << "[*] Checking LLM (" << model_ << ") at " << chat_url_ << "...\n";
    auto t0 = std::chrono::steady_clock::now();

    // Extract base URL for health check (strip /v1/chat/completions)
    std::string base = chat_url_;
    auto v1pos = base.find("/v1/");
    if (v1pos != std::string::npos) base = base.substr(0, v1pos);

    std::string health = ToolBridge::http_get(base + "/v1/models");
    if (health.empty()) {
        std::cerr << "[!] LLM runtime not responding at " << base
                  << " — start it (see: python main.py doctor).\n";
        return;
    }

    float s = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[OK] LLM runtime ready (" << s << " s).\n";

    // Build the warm-up prompts here (uses the bridge), then send them from
    // a background thread so startup isn't blocked.  The tool prompt's
    // static rules are identical for every command, so once they're in the
    // server's prompt cache only the per-command tail is processed.
    json tool_msgs = json::array({
        build_tool_prompt(json::array(), bridge, ""),
        {{"role", "user"}, {"content", "warm up"}},
    });
    json fast_msgs = json::array({
        build_simple_prompt(bridge),
        {{"role", "user"}, {"content", "hello"}},
    });
    std::string url = chat_url_, model = model_;
    std::thread([tool_msgs, fast_msgs, url, model]() {
        auto t1 = std::chrono::steady_clock::now();
        for (const auto& msgs : {tool_msgs, fast_msgs}) {
            json body = {
                {"model", model}, {"messages", msgs},
                {"stream", false}, {"max_tokens", 1}, {"temperature", 0.0},
            };
            ToolBridge::http_post(url, body.dump());
        }
        float w = std::chrono::duration<float>(std::chrono::steady_clock::now() - t1).count();
        std::cout << "[✓] LLM warm-up done (" << w << " s) — prompt cache primed.\n";
    }).detach();
}

// ── Streaming fast chat (no tools) ──────────────────────────────────

std::string LLMClient::fast_chat_streaming(const std::string& user_msg,
                                            ToolBridge& bridge,
                                            SentenceCallback on_sentence,
                                            std::atomic<bool>& cancel) {
    trim_history();
    last_tools_.clear();
    last_path_ = "fast";
    last_success_ = true;
    if (!configured()) {
        last_success_ = false;
        std::string msg = "I don't have a language model set up yet. "
                          "Run python main.py setup to plug one in.";
        on_sentence(msg);
        return msg;
    }
    history_.push_back({{"role", "user"}, {"content", user_msg}});

    json messages;
    messages.push_back(build_simple_prompt(bridge));

    int start = std::max(0, static_cast<int>(history_.size()) - cfg::MAX_HISTORY);
    for (int i = start; i < static_cast<int>(history_.size()); ++i)
        messages.push_back(history_[i]);

    auto text = call_llm_streaming(
        messages, on_sentence, cancel,
        cfg::FAST_PREDICT, cfg::LLM_TEMPERATURE);

    if (text.empty()) {
        // Don't leave the user in silence, and don't store an empty turn.
        history_.pop_back();
        last_success_ = false;
        if (!cancel.load()) {
            text = "Sorry, I couldn't reach my language model. "
                   "Please check that the MLX server is running.";
            on_sentence(text);
        }
        return text;
    }

    history_.push_back({{"role", "assistant"}, {"content", text}});
    return text;
}

// ── Streaming tool-calling chat (ReAct loop) ────────────────────────

std::string LLMClient::chat_with_tools_streaming(
    const std::string& user_msg,
    ToolBridge& bridge,
    SentenceCallback on_sentence,
    std::atomic<bool>& cancel,
    const std::string& learned_context)
{
    // Hard-reset history for each new tool command.
    // The tool path gets full context from the system prompt, memory,
    // and learned context — stale chat turns from previous commands
    // (e.g. greeting conversations) pollute the LLM and cause hallucinations.
    history_.clear();
    last_tools_.clear();
    last_path_ = "tool";
    last_success_ = true;
    pending_confirmation_ = {};
    file_choices_.clear();
    if (!configured()) {
        last_success_ = false;
        std::string msg = "I don't have a language model set up yet. "
                          "Run python main.py setup to plug one in.";
        on_sentence(msg);
        return msg;
    }

    // ── Tool filtering: fetch only relevant tools for this query ─
    json filtered_schemas = bridge.fetch_filtered_tools(user_msg, 8);

    if (filtered_schemas.empty() || !filtered_schemas.is_array()
        || filtered_schemas.size() == 0) {
        // Only fetch the full 43 KB schema list when filtering failed.
        filtered_schemas = bridge.fetch_tool_schemas();
        std::cout << "[ROUTER] Filtering failed — using all "
                  << filtered_schemas.size() << " tools.\n";
    } else {
        std::cout << "[ROUTER] Using " << filtered_schemas.size()
                  << " filtered tools.\n";
    }

    // Memory auto-retrieval
    std::string enriched = user_msg;
    try {
        // Strict threshold: only clearly related memories are injected.
        auto mem = bridge.execute("retrieve_memories",
                                   {{"query", user_msg}, {"top_k", 3}, {"max_distance", 0.55}});
        // retrieve_memories only returns "Relevant memories ..." when
        // something actually matches the query (distance threshold).
        if (mem.rfind("Relevant memories", 0) == 0) {
            enriched += "\n\n[MEMORY CONTEXT]\n" + mem;
            std::cout << "[MEMORY] Injected context.\n";
        }
    } catch (...) {}

    // Learned context goes into the system prompt only (build_tool_prompt);
    // it used to be injected twice, wasting the small model's context.
    if (!learned_context.empty())
        std::cout << "[LEARN] Learned context added to system prompt.\n";

    history_.push_back({{"role", "user"}, {"content", enriched}});

    // Build system prompt with tool list embedded as text
    auto sys = build_tool_prompt(filtered_schemas, bridge, learned_context);

    // ── ReAct loop ──────────────────────────────────────────────
    int depth = 0;
    std::string final_text;
    error_count_ = 0;  // reset per-command error counter

    // Deterministic high-confidence path: selected/highlighted PDF summary.
    {
        std::string lower_msg = user_msg;
        std::transform(lower_msg.begin(), lower_msg.end(), lower_msg.begin(), ::tolower);

        bool gmail_open_req =
            (lower_msg.find("open gmail") != std::string::npos) ||
            (lower_msg.find("gmail in browser") != std::string::npos);
        if (gmail_open_req) {
            auto direct = bridge.execute("open_url_and_wait", {
                {"url", "https://mail.google.com"},
                {"browser", "Brave Browser"},
                {"wait_seconds", 4}
            });
            if (!is_tool_error(direct)) {
                std::string say = "Opened Gmail in Brave Browser.";
                on_sentence(say);
                return say;
            }
        }

        // (A substring-based "compose email" short-circuit used to live here.
        //  It fired on phrases like "any news from my gmail" and opened an
        //  EMPTY draft, discarding recipient/subject/body.  Email composing
        //  is handled by the comms agent / gmail_compose_draft instead.)

        bool selected_pdf_req =
            (lower_msg.find("summarize") != std::string::npos || lower_msg.find("summarise") != std::string::npos) &&
            (lower_msg.find("selected pdf") != std::string::npos ||
             lower_msg.find("selected file") != std::string::npos ||
             lower_msg.find("highlighted file") != std::string::npos);

        if (selected_pdf_req) {
            auto direct = bridge.execute("summarize_selected_pdf", json::object());
            if (!direct.empty() && direct.rfind("[-]", 0) != 0 && direct.rfind("Failed:", 0) != 0) {
                on_sentence(direct);
                return direct;
            }
            // If this direct path fails, continue with normal tool-planning below.
        }
    }
    std::string prev_tool;   // for dedup detection
    json        prev_args;   // for dedup detection
    bool        prev_failed = false;  // did the most recent tool call fail?

    while (depth < cfg::MAX_TOOL_DEPTH && !cancel.load()) {
        ++depth;

        // Build full message list (trimmed to the runtime's context window)
        fit_history_to_context(sys);
        json messages;
        messages.push_back(sys);
        for (auto& m : history_) messages.push_back(m);

        // For tool-calling, don't stream to TTS — we need the full
        // JSON output to parse the action. Use a silent accumulator.
        std::string raw_output;
        auto silent_cb = [](const std::string&) {};

        raw_output = call_llm_streaming(
            messages, silent_cb, cancel,
            cfg::TOOL_PREDICT, cfg::LLM_TEMPERATURE);

        if (cancel.load()) break;
        if (raw_output.empty()) {
            final_text = "Sorry, I couldn't reach my language model. "
                         "Please check that the MLX server is running.";
            last_success_ = false;
            on_sentence(final_text);
            break;
        }

        // Parse the LLM's output
        auto action = parse_llm_action(raw_output);

        if (action.is_done) {
            // Final answer — stream it to TTS
            final_text = action.say.empty() ? raw_output : action.say;
            history_.push_back({{"role", "assistant"}, {"content", final_text}});

            // Send to TTS via sentence splitting
            if (!final_text.empty()) {
                on_sentence(final_text);
            }
            break;
        }

        // Tool call
        std::string fn = action.tool;
        json args = action.args;

        // ── Dedup: break if the LLM repeats the exact same call ──
        if (fn == prev_tool && args == prev_args) {
            std::cout << "[!] Duplicate tool call detected (" << fn
                      << ") — breaking ReAct loop.\n";
            if (prev_failed) {
                final_text = "I couldn't get that to work.";
                last_success_ = false;
            } else {
                final_text = "Done.";
            }
            on_sentence(final_text);
            break;
        }
        prev_tool = fn;
        prev_args = args;

        std::cout << "[TOOL CALL " << depth << "] "
                  << fn << "(" << args.dump() << ")\n";
        last_tools_.push_back(fn);

        // Record the assistant's tool-call output in history
        history_.push_back({{"role", "assistant"}, {"content", raw_output}});

        // Execute the tool.  A screen read costs ~1-2 s, so only ask for it
        // after actions whose next step needs on-screen element names.
        // (Browser tools: the tree can't see page content anyway — rule 10.)
        static const std::set<std::string> NEEDS_SCREEN = {
            "click_ui_element", "find_and_click", "right_click_element",
            "open_application", "activate_application", "type_text_into_element",
            "drag_element", "scroll_to_element",
        };
        auto tr = bridge.execute_with_screen(fn, args, NEEDS_SCREEN.count(fn) > 0);
        std::cout << "[TOOL RESULT] " << tr.result << "\n";

        // ── Error detection & recovery guidance ─────────────────
        // Prefer the server's explicit success flag; fall back to parsing.
        bool tool_failed = tr.has_success ? !tr.success : is_tool_error(tr.result);
        prev_failed = tool_failed;

        // ── Destructive action needs a spoken yes/no ────────────
        if (!tool_failed && tr.result.find("pending_confirmation") != std::string::npos) {
            try {
                auto pj = json::parse(tr.result);
                if (ToolBridge::json_str(pj, "status") == "pending_confirmation") {
                    pending_confirmation_.token  = ToolBridge::json_str(pj, "confirm_token");
                    pending_confirmation_.speech = ToolBridge::json_str(pj, "confirm_speech", "Should I go ahead?");
                    pending_confirmation_.tool   = ToolBridge::json_str(pj, "pending_tool");
                    if (!pending_confirmation_.token.empty()) {
                        final_text = pending_confirmation_.speech;
                        history_.push_back({{"role", "assistant"}, {"content", final_text}});
                        break;  // the daemon asks and handles yes/no
                    }
                }
            } catch (...) {}
        }

        // ── find_file with several matches → ask which one ──────
        if (!tool_failed && fn == "find_file") {
            try {
                auto fj = json::parse(tr.result);
                if (fj.contains("matches") && fj["matches"].is_array() && fj["matches"].size() > 1) {
                    std::string names;
                    for (auto& m : fj["matches"]) {
                        std::string path = ToolBridge::json_str(m, "path");
                        if (path.empty()) continue;
                        std::string name = ToolBridge::json_str(m, "name", path);
                        file_choices_.push_back({name, path});
                        if (!names.empty()) names += ", ";
                        names += name;
                        if (file_choices_.size() >= 5) break;
                    }
                    if (file_choices_.size() > 1) {
                        final_text = "I found " + std::to_string(fj["matches"].size()) +
                                     " files: " + names + ". Which one should I open?";
                        on_sentence(final_text);
                        history_.push_back({{"role", "assistant"}, {"content", final_text}});
                        break;
                    }
                    file_choices_.clear();
                }
            } catch (...) {}
        }
        if (tool_failed) {
            ++error_count_;
            std::cout << "[!] Tool error #" << error_count_
                      << ": " << tr.result << "\n";

            // Hard stop after REACT_MAX_ERRORS consecutive errors — prevent infinite loops
            if (error_count_ >= cfg::REACT_MAX_ERRORS) {
                std::cout << "[!] " << cfg::REACT_MAX_ERRORS
                          << " consecutive tool errors — aborting ReAct loop.\n";
                last_success_ = false;
                final_text = "I tried several approaches but couldn't complete that action. "
                             "You might need to do it manually.";
                on_sentence(final_text);
                history_.push_back({{"role", "assistant"}, {"content", final_text}});
                break;
            }
        } else {
            error_count_ = 0;  // reset on success
        }

        // Build tool result feedback — inject screen state if available.
        // MLX has no "tool" role, so we feed results back as "user" messages.
        // Cap large tool results (file lists, page text): every extra token
        // is prefill time on the next step and eats the context window.
        constexpr size_t MAX_RESULT_CHARS = 2000;
        std::string result_text = tr.result;
        if (result_text.size() > MAX_RESULT_CHARS)
            result_text = result_text.substr(0, MAX_RESULT_CHARS) + "\n…[" +
                          std::to_string(tr.result.size() - MAX_RESULT_CHARS) + " more chars]";
        std::string feedback = "[TOOL RESULT: " + fn + "]\n" + result_text;

        // Inject recovery guidance on error so the LLM tries a different approach
        if (tool_failed) {
            // Specific recovery hints based on the tool that failed
            if (fn == "find_file") {
                // Check if it was a folder-not-found or no-matches error
                bool folder_not_found = tr.result.find("Folder not found") != std::string::npos
                                     || tr.result.find("does not exist") != std::string::npos
                                     || tr.result.find("folder_not_found") != std::string::npos;
                if (folder_not_found) {
                    feedback += "\n\n[RECOVERY HINT] find_file failed: folder not found. "
                                "Retry with folder=\"~/Downloads\" (use tilde, not full path with username). "
                                "NEVER use /Users/<name>/... — always use ~/Downloads, ~/Documents, ~/Desktop. "
                                "Maximum 1 retry, then ask the user for the correct folder.";
                } else {
                    feedback += "\n\n[RECOVERY HINT] find_file failed: no matching files. "
                                "The file does not exist in that folder. Tell the user what you searched for "
                                "and ask them to confirm the filename. "
                                "NEVER open a browser, Terminal, or any app to search for local files. "
                                "NEVER create a file when asked to find/open one. "
                                "Use {\"done\":true,\"say\":\"I couldn't find that file. Could you tell me the exact name?\"}.";
                }
            } else {
                feedback += "\n\n[RECOVERY HINT] The tool failed. Try a different approach: "
                            "read the screen with get_active_window_tree to find correct "
                            "element names, use a keyboard shortcut instead, or report "
                            "the issue to the user with {\"done\":true,\"say\":\"...\"}. "
                            "NEVER open a browser to search for local files. "
                            "NEVER create a file when asked to find/open one.";
            }
        }

        if (!tr.screen_state.empty()) {
            std::string screen = tr.screen_state;
            // Every extra prompt token costs prefill time on the next step.
            if (screen.size() > 1500)
                screen = screen.substr(0, 1500) + "... (truncated)";
            feedback += "\n\n[SCREEN STATE AFTER ACTION]\n" + screen;
            std::cout << "[SCREEN] Injected " << screen.size()
                      << " chars of screen state.\n";
        }

        history_.push_back({{"role", "user"}, {"content", feedback}});
    }

    if (depth >= cfg::MAX_TOOL_DEPTH)
        std::cout << "[!] Hit max tool depth (" << cfg::MAX_TOOL_DEPTH << ").\n";

    // If the loop ended without a final answer, report honestly.
    if (final_text.empty() && !cancel.load()) {
        if (prev_failed || last_tools_.empty()) {
            final_text = "I wasn't able to finish that.";
            last_success_ = false;
        } else {
            final_text = "Done.";
        }
        on_sentence(final_text);
    }

    return final_text;
}

// ── Planner: detect multi-step commands ─────────────────────────────

bool LLMClient::is_multi_step_command(const std::string& command) const {
    std::string cmd = command;
    std::transform(cmd.begin(), cmd.end(), cmd.begin(), ::tolower);

    static const char* MULTI_STEP_KEYWORDS[] = {
        " and then ", " after that ", " then ",
        " next ", " also ", " followed by ",
        "step 1", "step 2", "first ", "second ",
        "search for", "look up", "find and ",
        "go to", "navigate to",
    };

    for (auto& kw : MULTI_STEP_KEYWORDS) {
        if (cmd.find(kw) != std::string::npos)
            return true;
    }

    static const char* ACTION_VERBS[] = {
        "open", "click", "type", "search", "scroll",
        "close", "save", "download", "copy", "paste",
    };
    int verb_count = 0;
    for (auto& v : ACTION_VERBS) {
        if (cmd.find(v) != std::string::npos)
            ++verb_count;
    }
    return verb_count >= 2;
}

bool LLMClient::is_structured_task(const std::string& command) const {
    std::string cmd = command;
    std::transform(cmd.begin(), cmd.end(), cmd.begin(), ::tolower);

    // ── Task-oriented phrases that always need structured execution ──
    static const char* TASK_PHRASES[] = {
        "write an email", "write a mail", "write email", "write mail",
        "send an email", "send a mail", "send email", "send mail",
        "compose email", "compose mail", "compose a", "draft email",
        "reply to", "respond to", "forward email", "forward mail",
        "search on youtube", "search youtube", "play on youtube",
        "search on google", "google for", "search google",
        "log into", "log in to", "sign into", "sign in to",
        "open gmail and", "open youtube and", "open google and",
        "go to gmail", "go to youtube",
        "download from", "upload to",
        "fill out", "fill in the", "submit the form",
        "book a", "order a", "purchase",
    };

    for (auto& phrase : TASK_PHRASES) {
        if (cmd.find(phrase) != std::string::npos)
            return true;
    }

    // ── Also catch everything is_multi_step_command catches ──
    return is_multi_step_command(command);
}

std::vector<std::string> LLMClient::plan_steps(
    const std::string& command,
    ToolBridge& bridge,
    std::atomic<bool>& cancel) {

    std::cout << "[PLANNER] Decomposing: \"" << command << "\"\n";

    json plan_messages;
    plan_messages.push_back({
        {"role", "system"},
        {"content",
            "You are a macOS automation planner. Break the user's request into 2-4 sequential steps.\n\n"
            "CRITICAL RULES:\n"
            "1. You may ONLY use steps that map to these real tools:\n"
            "   - open_application(app_name) — launch a macOS app by its EXACT name\n"
            "   - open_url(url) — open a website URL in Safari\n"
            "   - find_file(folder, partial_name, extension) — search for a file by partial name\n"
            "   - open_file(file_path) — open a file at an absolute path\n"
            "   - click_ui_element(element_name) — click a named UI element\n"
            "   - type_text(text) — type text into the focused field\n"
            "   - press_keyboard_shortcut(keys) — only use shortcuts from the approved list below\n"
            "   - search_web(query) — search the web via DuckDuckGo\n"
            "   - read_webpage(url) — fetch and extract text from a URL\n"
            "   - get_active_window_tree() — read current UI elements on screen\n"
            "   - activate_application(app_name) — bring an app to the foreground\n\n"
            "2. NEVER invent keyboard shortcuts. The ONLY approved shortcuts are:\n"
            "   cmd+w, cmd+q, cmd+t, cmd+n, cmd+c, cmd+v, cmd+z, cmd+a, cmd+space,\n"
            "   cmd+option+l (Downloads), cmd+shift+o (Documents), cmd+shift+d (Desktop),\n"
            "   cmd+shift+3 (screenshot), cmd+shift+4 (screenshot selection)\n\n"
            "3. To find and open a file: ALWAYS use find_file first, then open_file with {result_of_step_1}.\n"
            "   NEVER use open_application to open a file. NEVER type a filename into an app.\n\n"
            "4. To open a website: ALWAYS use open_url. NEVER use open_application + type_text.\n\n"
            "5. Output ONLY a JSON array. No explanation. No markdown. Example:\n"
            "   [{\"tool\": \"find_file\", \"args\": {\"folder\": \"~/Downloads\", \"partial_name\": \"SIH\", \"extension\": \"pdf\"}},\n"
            "    {\"tool\": \"open_file\", \"args\": {\"file_path\": \"{result_of_step_1}\"}}]\n\n"
            "6. If the request needs only 1 step, output an array with 1 item.\n"
            "7. Maximum 4 steps. If it needs more, output the first 4 steps only.\n"
            "8. If the user says 'open a/the/my [extension]' with NO specific filename,\n"
            "   use find_file with partial_name=\"\" to list all files of that type.\n"
            "   Then speak the list to the user and wait for them to specify which one.\n"
            "   NEVER use \"FILE\", \"file\", or the extension word itself as the partial_name.\n"
            "9. ALWAYS use folder='~/Downloads' or '~/Documents' or '~/Desktop' — NEVER construct\n"
            "   paths with a username. Use tilde (~) only."
        }
    });
    plan_messages.push_back({
        {"role", "user"},
        {"content", command}
    });

    auto plan_text = call_llm_blocking(plan_messages, cfg::TOOL_PREDICT + 100);

    std::cout << "[PLANNER] Raw output: " << plan_text << "\n";

    // ── Try to parse JSON array format first ────────────────────
    std::vector<std::string> steps;
    {
        // Find the JSON array in the output
        auto arr_start = plan_text.find('[');
        auto arr_end = plan_text.rfind(']');
        if (arr_start != std::string::npos && arr_end != std::string::npos && arr_end > arr_start) {
            std::string json_str = plan_text.substr(arr_start, arr_end - arr_start + 1);
            try {
                auto arr = json::parse(json_str);
                if (arr.is_array()) {
                    // Known valid tool names
                    static const std::set<std::string> VALID_TOOLS = {
                        "open_application", "open_url", "find_file", "open_file",
                        "click_ui_element", "type_text", "press_keyboard_shortcut",
                        "search_web", "read_webpage", "get_active_window_tree",
                        "activate_application", "find_and_click", "type_and_submit",
                        "type_text_into_element", "quit_application",
                        "list_files_in_folder", "wait_for_element",
                        "scroll_direction", "vision_execute",
                        "browser_click_text", "browser_type",
                        "gmail_compose_draft",
                    };

                    // Store parsed step data for result substitution
                    std::vector<json> parsed_steps;
                    for (auto& item : arr) {
                        if (!item.is_object()) continue;
                        std::string tool = item.value("tool", "");
                        if (tool.empty() || VALID_TOOLS.find(tool) == VALID_TOOLS.end()) {
                            std::cout << "[PLANNER] Skipping invalid tool: " << tool << "\n";
                            continue;
                        }
                        parsed_steps.push_back(item);
                    }

                    // Convert to step strings that the tool path understands
                    for (size_t i = 0; i < parsed_steps.size() && i < 4; ++i) {
                        auto& item = parsed_steps[i];
                        std::string tool = item.value("tool", "");
                        auto args = item.value("args", json::object());

                        // Substitute {result_of_step_N} references
                        // For now, these are placeholders — the actual result
                        // substitution happens at execution time.  Convert to
                        // a natural language instruction the tool path can handle.
                        std::string step;
                        if (tool == "find_file") {
                            step = "Search for files named '" +
                                   args.value("partial_name", "") + "' in " +
                                   args.value("folder", "~/Downloads");
                            if (args.contains("extension"))
                                step += " with extension " + args.value("extension", "");
                        } else if (tool == "open_file") {
                            step = "Open the file that was found in the previous step";
                        } else if (tool == "open_url") {
                            step = "Open " + args.value("url", "") + " in the browser";
                        } else if (tool == "open_application") {
                            step = "Open " + args.value("app_name", "");
                        } else if (tool == "type_text") {
                            step = "Type '" + args.value("text", "") + "'";
                        } else if (tool == "press_keyboard_shortcut") {
                            step = "Press keyboard shortcut " + args.value("keys", "");
                        } else if (tool == "click_ui_element") {
                            step = "Click on '" + args.value("element_name", "") + "'";
                        } else if (tool == "search_web") {
                            step = "Search the web for '" + args.value("query", "") + "'";
                        } else {
                            step = "Use " + tool;
                            if (!args.empty()) step += " with " + args.dump();
                        }
                        if (!step.empty())
                            steps.push_back(step);
                    }
                }
            } catch (const json::parse_error&) {
                std::cout << "[PLANNER] JSON parse failed — falling back to numbered list.\n";
            }
        }
    }

    // ── Fallback: parse numbered steps from plain text ──────────
    if (steps.empty()) {
        std::istringstream iss(plan_text);
        std::string line;
        while (std::getline(iss, line)) {
            while (!line.empty() && std::isspace(static_cast<unsigned char>(line.front())))
                line.erase(line.begin());
            while (!line.empty() && std::isspace(static_cast<unsigned char>(line.back())))
                line.pop_back();

            if (line.size() > 2 && std::isdigit(static_cast<unsigned char>(line[0]))) {
                auto dot_pos = line.find('.');
                if (dot_pos != std::string::npos && dot_pos < 4) {
                    std::string step = line.substr(dot_pos + 1);
                    while (!step.empty() && step.front() == ' ')
                        step.erase(step.begin());
                    if (!step.empty())
                        steps.push_back(step);
                }
            }
        }
    }

    // Cap at 4 steps
    if (steps.size() > 4) steps.resize(4);

    std::cout << "[PLANNER] Decomposed into " << steps.size() << " steps:\n";
    for (size_t i = 0; i < steps.size(); ++i)
        std::cout << "  " << (i + 1) << ". " << steps[i] << "\n";

    return steps;
}

// ── Planner structured: returns JSON steps for direct execution ─────

std::vector<json> LLMClient::plan_steps_structured(
    const std::string& command,
    ToolBridge& bridge,
    std::atomic<bool>& cancel) {

    std::cout << "[PLANNER-S] Decomposing: \"" << command << "\"\n";

    // Reuse the same planner prompt
    json plan_messages;
    plan_messages.push_back({
        {"role", "system"},
        {"content",
            "You are a macOS automation planner. Break the user's request into 2-4 sequential steps.\n\n"
            "CRITICAL RULES:\n"
            "1. You may ONLY use steps that map to these real tools:\n"
            "   - open_application(app_name) — launch a macOS app by its EXACT name\n"
            "   - open_url(url) — open a website URL in Safari\n"
            "   - find_file(folder, partial_name, extension) — search for a file by partial name\n"
            "   - open_file(file_path) — open a file at an absolute path\n"
            "   - click_ui_element(element_name) — click a named UI element\n"
            "   - type_text(text) — type text into the focused field\n"
            "   - press_keyboard_shortcut(keys) — only use shortcuts from the approved list below\n"
            "   - search_web(query) — search the web via DuckDuckGo\n"
            "   - read_webpage(url) — fetch and extract text from a URL\n"
            "   - get_active_window_tree() — read current UI elements on screen\n"
            "   - activate_application(app_name) — bring an app to the foreground\n\n"
            "2. NEVER invent keyboard shortcuts. The ONLY approved shortcuts are:\n"
            "   cmd+w, cmd+q, cmd+t, cmd+n, cmd+c, cmd+v, cmd+z, cmd+a, cmd+space,\n"
            "   cmd+option+l (Downloads), cmd+shift+o (Documents), cmd+shift+d (Desktop),\n"
            "   cmd+shift+3 (screenshot), cmd+shift+4 (screenshot selection)\n\n"
            "3. To find and open a file: ALWAYS use find_file first, then open_file.\n"
            "   For open_file, use {result_of_step_1} as the file_path — it will be replaced\n"
            "   with the actual path from find_file's result.\n"
            "   NEVER use open_application to open a file.\n\n"
            "4. To open a website: ALWAYS use open_url. NEVER use open_application + type_text.\n\n"
            "5. Output ONLY a JSON array. No explanation. No markdown.\n"
            "   [{\"tool\": \"find_file\", \"args\": {\"folder\": \"~/Downloads\", \"partial_name\": \"SIH\", \"extension\": \"pdf\"}},\n"
            "    {\"tool\": \"open_file\", \"args\": {\"file_path\": \"{result_of_step_1}\"}}]\n\n"
            "6. Maximum 4 steps.\n"
            "7. If the user says 'open a/the/my [extension]' with NO specific filename,\n"
            "   use find_file with partial_name=\"\" to list all files of that type.\n"
            "   NEVER use \"FILE\" or the extension word as the partial_name.\n"
            "8. ALWAYS use folder='~/Downloads' or '~/Documents' or '~/Desktop'.\n"
            "   NEVER use a path with a username like /Users/name/. Use ~ instead."
        }
    });
    plan_messages.push_back({
        {"role", "user"},
        {"content", command}
    });

    auto plan_text = call_llm_blocking(plan_messages, cfg::TOOL_PREDICT + 100);
    std::cout << "[PLANNER-S] Raw output: " << plan_text << "\n";

    std::vector<json> structured_steps;

    // Try to parse JSON array
    auto arr_start = plan_text.find('[');
    auto arr_end = plan_text.rfind(']');
    if (arr_start != std::string::npos && arr_end != std::string::npos && arr_end > arr_start) {
        std::string json_str = plan_text.substr(arr_start, arr_end - arr_start + 1);
        try {
            auto arr = json::parse(json_str);
            if (arr.is_array()) {
                static const std::set<std::string> VALID_TOOLS = {
                    "open_application", "open_url", "find_file", "open_file",
                    "click_ui_element", "type_text", "press_keyboard_shortcut",
                    "search_web", "read_webpage", "get_active_window_tree",
                    "activate_application", "find_and_click", "type_and_submit",
                    "type_text_into_element", "quit_application",
                    "list_files_in_folder", "wait_for_element",
                    "scroll_direction", "vision_execute",
                    "browser_click_text", "browser_type",
                    "gmail_compose_draft",
                };

                for (auto& item : arr) {
                    if (!item.is_object()) continue;
                    std::string tool = item.value("tool", "");
                    if (tool.empty() || VALID_TOOLS.find(tool) == VALID_TOOLS.end()) {
                        std::cout << "[PLANNER-S] Skipping invalid tool: " << tool << "\n";
                        continue;
                    }

                    // Sanitize find_file args — block generic partial_name values
                    if (tool == "find_file" && item.contains("args")) {
                        auto& args = item["args"];
                        std::string pn = args.value("partial_name", "");
                        std::string pn_lower = pn;
                        std::transform(pn_lower.begin(), pn_lower.end(), pn_lower.begin(), ::tolower);
                        static const std::set<std::string> BLOCKLIST = {
                            "file", "a", "the", "my", "this", "that", "it",
                            "pdf", "ppt", "doc", "docx", "pptx", "xls", "xlsx",
                            "mp4", "mp3", "zip", "txt", "jpg", "png", "csv",
                        };
                        if (pn.empty() || BLOCKLIST.count(pn_lower)) {
                            args["partial_name"] = "";
                            std::cout << "[PLANNER-S] Cleared generic partial_name: '" << pn << "'\n";
                        }

                        // Ensure folder uses ~ not hardcoded username
                        std::string folder = args.value("folder", "~/Downloads");
                        if (folder.find("/Users/") == 0) {
                            // Replace /Users/<anything>/<rest> with ~/<rest>
                            auto parts_start = folder.find('/', 7); // skip "/Users/"
                            if (parts_start != std::string::npos) {
                                folder = "~" + folder.substr(parts_start);
                            } else {
                                folder = "~/Downloads";
                            }
                            args["folder"] = folder;
                            std::cout << "[PLANNER-S] Fixed folder path to: " << folder << "\n";
                        }
                    }

                    structured_steps.push_back(item);
                    if (structured_steps.size() >= 4) break;
                }
            }
        } catch (const json::parse_error& e) {
            std::cout << "[PLANNER-S] JSON parse failed: " << e.what() << "\n";
        }
    }

    // If JSON parsing failed, fall back to plan_steps (text-based) and wrap in JSON
    if (structured_steps.empty()) {
        auto text_steps = plan_steps(command, bridge, cancel);
        // Can't convert text to structured JSON reliably — return empty
        // so caller falls back to chat_with_tools_streaming
        std::cout << "[PLANNER-S] Could not produce structured steps, returning empty.\n";
    }

    std::cout << "[PLANNER-S] " << structured_steps.size() << " structured steps:\n";
    for (size_t i = 0; i < structured_steps.size(); ++i) {
        std::cout << "  " << (i + 1) << ". " << structured_steps[i].value("tool", "?")
                  << "(" << structured_steps[i].value("args", json::object()).dump() << ")\n";
    }

    return structured_steps;
}

// ── PATH 4: Deep Intent — makes 3B think like 70B ──────────────────

bool LLMClient::is_deep_intent_command(const std::string& command) const {
    std::string cmd = command;
    std::transform(cmd.begin(), cmd.end(), cmd.begin(), ::tolower);

    // Commands that should go to PATH 3 (tool ReAct) NOT deep intent:
    // "compose an email", "draft an email", "click the X" → these need tools,
    // not multi-phase reasoning.  Deep intent is for ambiguous/complex tasks.
    static const char* TOOL_PATH_KEYWORDS[] = {
        "compose", "draft", "click the", "click on", "tap the",
        "fill in", "fill out", "type into", "navigate to",
    };
    for (auto& kw : TOOL_PATH_KEYWORDS) {
        if (cmd.find(kw) != std::string::npos)
            return false;
    }

    // Communication tasks — benefit most from deep intent
    static const char* DEEP_KEYWORDS[] = {
        "reply to", "respond to", "write a reply", "smart reply",
        "research and", "find out", "summarize",
        "tell me about", "give me a report",
        "schedule", "remind me", "set up a meeting",
        "extract contact", "get contact",
        "message", "send a message",
    };

    for (auto& kw : DEEP_KEYWORDS) {
        if (cmd.find(kw) != std::string::npos)
            return true;
    }

    return false;
}

LLMClient::DeepIntentAction LLMClient::parse_deep_intent(
    const std::string& raw_text) const {

    DeepIntentAction action;

    auto objs = extract_json_objects(raw_text);
    for (const auto& obj : objs) {
        try {
            auto j = json::parse(obj);

            // Check if it's a done response
            if (j.contains("done") && j.value("done", false)) {
                action.is_done = true;
                action.say = j.value("say", "");
                action.intent = j.value("intent", "");
                action.task_type = j.value("task_type", "conversation");
                return action;
            }

            // Check for deep intent structure
            if (j.contains("intent")) {
                action.intent = j.value("intent", "");
                action.task_type = j.value("task_type", "general");
                action.needs_confirm = j.value("needs_confirm", false);

                // Extract plan
                if (j.contains("plan") && j["plan"].is_array()) {
                    for (auto& step : j["plan"]) {
                        if (step.is_string())
                            action.plan.push_back(step.get<std::string>());
                    }
                }

                // Extract first action
                if (j.contains("first_action") && j["first_action"].is_object()) {
                    auto& fa = j["first_action"];
                    action.first_tool = fa.value("tool", "");
                    action.first_args = fa.value("args", json::object());
                }

                return action;
            }

            // Fallback: regular tool call format
            if (j.contains("tool") && j["tool"].is_string()) {
                action.first_tool = j["tool"].get<std::string>();
                action.first_args = j.value("args", json::object());
                action.intent = "direct action";
                return action;
            }

        } catch (...) {}
    }

    // No parseable action — treat as spoken text
    action.is_done = true;
    action.say = raw_text;
    return action;
}

std::string LLMClient::deep_intent_streaming(
    const std::string& user_msg,
    ToolBridge& bridge,
    SentenceCallback on_sentence,
    std::atomic<bool>& cancel) {

    history_.clear();
    last_tools_.clear();
    last_path_ = "deep_intent";

    std::cout << "[🧠 DEEP INTENT] Phase 1: Understanding...\n";

    // ── Fetch deep intent context from Python server ────────────
    auto di = bridge.fetch_deep_intent(user_msg);
    std::cout << "[🧠 DEEP INTENT] Task type: " << di.task_type << "\n";

    // ── Phase 1: Understand + Plan ──────────────────────────────
    // Use the deep intent prompt for structured understanding
    std::string system_content = di.deep_intent_prompt;
    if (system_content.empty()) {
        // Fallback — basic deep intent prompt
        system_content =
            "You are DEMASCAS. Analyze the user's command and respond with JSON:\n"
            "{\"intent\":\"<summary>\",\"task_type\":\"<type>\","
            "\"plan\":[\"step1\",\"step2\"],\"needs_confirm\":false,"
            "\"first_action\":{\"tool\":\"<tool>\",\"args\":{...}}}\n"
            "Or if you can answer directly: {\"done\":true,\"say\":\"<response>\"}";
    }

    json phase1_messages;
    phase1_messages.push_back({{"role", "system"}, {"content", system_content}});
    phase1_messages.push_back({{"role", "user"}, {"content", user_msg}});

    auto silent_cb = [](const std::string&) {};
    auto phase1_raw = call_llm_streaming(
        phase1_messages, silent_cb, cancel,
        cfg::TOOL_PREDICT + 100, cfg::LLM_TEMPERATURE);

    if (phase1_raw.empty() || cancel.load()) {
        std::string fallback = "I couldn't understand that. Could you rephrase?";
        on_sentence(fallback);
        return fallback;
    }

    auto intent = parse_deep_intent(phase1_raw);
    std::cout << "[🧠 DEEP INTENT] Intent: " << intent.intent << "\n";
    std::cout << "[🧠 DEEP INTENT] Plan: " << intent.plan.size() << " steps\n";

    // ── Direct answer — no tools needed ─────────────────────────
    if (intent.is_done) {
        std::string answer = intent.say.empty() ? phase1_raw : intent.say;
        on_sentence(answer);
        return answer;
    }

    // ── Phase 2: Execute plan steps ─────────────────────────────
    std::cout << "[🧠 DEEP INTENT] Phase 2: Executing plan...\n";

    // If there's a direct first_action, execute it
    if (!intent.first_tool.empty()) {
        std::cout << "[🧠 DEEP INTENT] First action: " << intent.first_tool
                  << "(" << intent.first_args.dump() << ")\n";

        last_tools_.push_back(intent.first_tool);
        auto tr = bridge.execute_with_screen(intent.first_tool, intent.first_args);
        std::cout << "[TOOL RESULT] " << tr.result << "\n";

        // If the tool returned a confirmation request, handle it
        try {
            auto result_json = json::parse(tr.result);
            if (result_json.contains("status") &&
                result_json.value("status", "") == "pending_confirmation") {
                std::string confirm_speech = result_json.value("confirm_speech", "Should I proceed?");
                on_sentence(confirm_speech);
                return confirm_speech;
            }
            // If compose_email returned a draft, speak the confirmation
            if (result_json.contains("confirm")) {
                std::string confirm = result_json.value("confirm", "Done.");
                on_sentence(confirm);
                return confirm;
            }
        } catch (...) {
            // Not JSON — continue normally
        }
    }

    // Execute remaining plan steps through the regular tool path
    if (intent.plan.size() > 1) {
        std::string learned_ctx;
        try {
            learned_ctx = bridge.fetch_learned_context(user_msg);
        } catch (...) {}

        // Skip first step if we already executed first_action
        size_t start_step = intent.first_tool.empty() ? 0 : 1;

        std::string step_reply;
        for (size_t i = start_step; i < intent.plan.size() && !cancel.load(); ++i) {
            std::cout << "[🧠 DEEP INTENT STEP " << (i + 1) << "/"
                      << intent.plan.size() << "] " << intent.plan[i] << "\n";

            step_reply = chat_with_tools_streaming(
                intent.plan[i], bridge, on_sentence, cancel,
                learned_ctx);
        }
        return step_reply.empty() ? "Done." : step_reply;
    }

    // Single-step action already executed
    std::string confirm = "Done.";
    on_sentence(confirm);
    return confirm;
}

}  // namespace demascas
