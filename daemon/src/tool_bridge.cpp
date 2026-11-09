/**
 * DEMASCAS — daemon/src/tool_bridge.cpp
 * libcurl-based HTTP bridge to the Python tool server.
 */

#include "tool_bridge.hpp"
#include "config.hpp"

#include <curl/curl.h>
#include <algorithm>
#include <chrono>
#include <iostream>
#include <thread>

namespace demascas {

namespace cfg = config;
using json = nlohmann::json;

// ── libcurl write callback ──────────────────────────────────────────

static size_t write_cb(void* ptr, size_t size, size_t nmemb,
                        std::string* out) {
    out->append(static_cast<char*>(ptr), size * nmemb);
    return size * nmemb;
}

// ── JSON / URL helpers ──────────────────────────────────────────────

std::string ToolBridge::json_str(const json& j, const char* key,
                                 const std::string& def) {
    if (!j.is_object()) return def;
    auto it = j.find(key);
    if (it == j.end() || it->is_null()) return def;
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

std::string ToolBridge::url_encode(const std::string& s) {
    CURL* curl = curl_easy_init();
    if (!curl) return s;
    char* out = curl_easy_escape(curl, s.c_str(), static_cast<int>(s.size()));
    std::string r = out ? out : s;
    if (out) curl_free(out);
    curl_easy_cleanup(curl);
    return r;
}

// ── HTTP helpers ────────────────────────────────────────────────────

std::string ToolBridge::http_post(const std::string& url,
                                   const std::string& body) {
    CURL* curl = curl_easy_init();
    if (!curl) return "";

    std::string response;
    struct curl_slist* hdrs = nullptr;
    hdrs = curl_slist_append(hdrs, "Content-Type: application/json");

    curl_easy_setopt(curl, CURLOPT_URL,           url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER,     hdrs);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS,     body.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION,  write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA,      &response);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT,        30L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL,       1L);

    CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK)
        std::cerr << "[!] CURL POST " << url << " → "
                  << curl_easy_strerror(res) << "\n";

    curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);
    return response;
}

std::string ToolBridge::http_get(const std::string& url) {
    CURL* curl = curl_easy_init();
    if (!curl) return "";

    std::string response;
    curl_easy_setopt(curl, CURLOPT_URL,           url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION,  write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA,      &response);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT,        5L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL,       1L);

    CURLcode res = curl_easy_perform(curl);
    // Silently ignore errors — used for health-check polling.
    (void)res;

    curl_easy_cleanup(curl);
    return response;
}

// ── Server readiness ────────────────────────────────────────────────

bool ToolBridge::wait_for_server(int max_retries) {
    for (int i = 0; i < max_retries; ++i) {
        auto resp = http_get(cfg::TOOL_HEALTH_URL);
        if (!resp.empty()) {
            try {
                auto j = json::parse(resp);
                if (json_str(j, "status") == "ok") {
                    int n = (j.contains("tools") && j["tools"].is_number_integer())
                                ? j["tools"].get<int>() : 0;
                    std::cout << "[✓] Tool server ready (" << n
                              << " tools on localhost:5001)\n";
                    return true;
                }
            } catch (...) {}
        }
        std::cout << "[*] Waiting for tool server… ("
                  << i + 1 << "/" << max_retries << ")\n";
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    std::cerr << "[!] Tool server not responding after "
              << max_retries << " retries.\n";
    return false;
}

// ── Schema fetch ────────────────────────────────────────────────────

json ToolBridge::fetch_tool_schemas() {
    auto resp = http_get(cfg::TOOL_SCHEMA_URL);
    if (resp.empty()) return json::array();
    try {
        return json::parse(resp);
    } catch (...) {
        return json::array();
    }
}

// ── Tool execution ──────────────────────────────────────────────────

std::string ToolBridge::execute(const std::string& name,
                                 const json& args) {
    json payload = {{"name", name}, {"arguments", args}};
    auto resp = http_post(cfg::TOOL_EXEC_URL, payload.dump());

    if (resp.empty())
        return "[-] Tool server did not respond.";

    try {
        auto j = json::parse(resp);
        std::string err = json_str(j, "error");
        if (!err.empty()) return "[-] " + err;
        return json_str(j, "result", "[-] No result in response.");
    } catch (...) {
        return "[-] Invalid JSON from tool server.";
    }
}

// ── Tool execution with screen state ────────────────────────────────

ToolBridge::ToolResult ToolBridge::execute_with_screen(
    const std::string& name, const json& args, bool include_screen) {

    json payload = {{"name", name}, {"arguments", args},
                    {"include_screen", include_screen}};
    auto resp = http_post(cfg::TOOL_EXEC_URL, payload.dump());

    ToolResult tr;
    if (resp.empty()) {
        tr.result = "[-] Tool server did not respond.";
        return tr;
    }

    try {
        auto j = json::parse(resp);
        std::string err = json_str(j, "error");
        if (!err.empty()) {
            tr.result = "[-] " + err;
            tr.has_success = true;
            tr.success = false;
            return tr;
        }
        tr.result = json_str(j, "result", "[-] No result in response.");
        tr.screen_state = json_str(j, "screen_state");
        if (j.contains("success") && j["success"].is_boolean()) {
            tr.has_success = true;
            tr.success = j["success"].get<bool>();
        }
    } catch (...) {
        tr.result = "[-] Invalid JSON from tool server.";
    }

    return tr;
}

// ── Filtered tool fetch ─────────────────────────────────────────────

json ToolBridge::fetch_filtered_tools(const std::string& query,
                                       int max_tools) {
    json payload = {{"query", query}, {"max_tools", max_tools}};
    auto resp = http_post(cfg::TOOL_SERVER_BASE + "/tools_for_query",
                          payload.dump());
    if (resp.empty()) return json::array();
    try {
        auto j = json::parse(resp);
        if (j.is_array()) {
            std::cout << "[ROUTER] Filtered to " << j.size()
                      << " tools for query.\n";
            return j;
        }
        return json::array();
    } catch (...) {
        return json::array();
    }
}

// ── Learning: fetch learned context ─────────────────────────────────

std::string ToolBridge::fetch_learned_context(const std::string& query) {
    // Full percent-encoding (handles '+', '#', '%', non-ASCII, ...)
    auto resp = http_get(cfg::TOOL_SERVER_BASE + "/context?query=" + url_encode(query));
    if (resp.empty()) return "";

    try {
        auto j = json::parse(resp);
        return json_str(j, "context_text");
    } catch (...) {
        return "";
    }
}

ToolBridge::AgentResult ToolBridge::execute_agent(
    const std::string& command, const std::string& context_json) {

    AgentResult out;

    json context = json::object();
    try {
        auto parsed = json::parse(context_json.empty() ? "{}" : context_json);
        if (parsed.is_object()) context = parsed;
    } catch (...) {}

    json payload = {
        {"command", command},
        {"context", context},
    };

    CURL* curl = curl_easy_init();
    if (!curl) {
        out.error = "HTTP client init failed.";
        return out;
    }

    std::string url = cfg::TOOL_SERVER_BASE + "/agent";
    std::string body = payload.dump();
    std::string response;

    struct curl_slist* hdrs = nullptr;
    hdrs = curl_slist_append(hdrs, "Content-Type: application/json");

    curl_easy_setopt(curl, CURLOPT_URL,           url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER,    hdrs);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS,    body.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA,     &response);
    // Must exceed the agent's own wall-clock budget (AGENT_TIME_BUDGET
    // in server/agents/base_agent.py) so the agent always stops first.
    curl_easy_setopt(curl, CURLOPT_TIMEOUT,       cfg::AGENT_TIMEOUT_SEC);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL,      1L);

    CURLcode res = curl_easy_perform(curl);
    long http_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);
    curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK) {
        out.error = std::string("HTTP error: ") + curl_easy_strerror(res);
        out.unavailable = (res == CURLE_COULDNT_CONNECT);
        return out;
    }
    if (http_code == 503) out.unavailable = true;

    if (response.empty()) {
        out.error = "Empty response from /agent endpoint.";
        return out;
    }

    out.raw_json = response;
    try {
        auto j = json::parse(response);
        // Null-safe reads: the server sends "error": null on success, and
        // j.value("error", "") throws on null.
        if (j.contains("success") && j["success"].is_boolean())
            out.success = j["success"].get<bool>();
        out.summary = json_str(j, "summary");
        if (j.contains("steps_taken") && j["steps_taken"].is_number_integer())
            out.steps_taken = j["steps_taken"].get<int>();
        out.error = json_str(j, "error");
        if (j.contains("tool_calls") && j["tool_calls"].is_array())
            for (auto& t : j["tool_calls"])
                if (t.is_string()) out.tool_calls.push_back(t.get<std::string>());

        if (j.contains("data") && j["data"].is_object()) {
            const auto& d = j["data"];
            if (d.contains("pending_file_choices") && d["pending_file_choices"].is_array()) {
                for (auto& fc : d["pending_file_choices"]) {
                    std::string path = json_str(fc, "path");
                    if (path.empty()) continue;
                    std::string name = json_str(fc, "name", path);
                    out.file_choices.push_back({name, path});
                    if (out.file_choices.size() >= 5) break;
                }
            }
        }
        if (j.contains("pending_confirmation") && j["pending_confirmation"].is_object()) {
            const auto& pc = j["pending_confirmation"];
            out.confirm_token  = json_str(pc, "confirm_token");
            out.confirm_speech = json_str(pc, "confirm_speech", "Should I go ahead?");
            out.pending_tool   = json_str(pc, "pending_tool");
        }
    } catch (const std::exception& e) {
        out.error = std::string("JSON parse error: ") + e.what();
    }

    return out;
}

ToolBridge::LLMRuntimeConfig ToolBridge::fetch_llm_config() {
    LLMRuntimeConfig c;
    auto resp = http_get(cfg::TOOL_SERVER_BASE + "/llm");
    if (resp.empty()) return c;
    try {
        auto j = json::parse(resp);
        auto num = [&](const char* k, int def) {
            return (j.contains(k) && j[k].is_number_integer()) ? j[k].get<int>() : def;
        };
        c.configured      = j.contains("configured") && j["configured"].is_boolean() && j["configured"].get<bool>();
        c.runtime         = json_str(j, "runtime");
        c.chat_url        = json_str(j, "chat_url");
        c.model           = json_str(j, "model");
        c.context_tokens  = std::max(1024, num("context_tokens", 4096));
        c.whisper_threads = std::max(1, num("whisper_threads", 4));
        c.tts_threads     = std::max(1, num("tts_threads", 4));
    } catch (...) {}
    return c;
}

std::string ToolBridge::confirm(const std::string& token, bool approve) {
    json payload = {{"token", token}, {"approve", approve}};
    auto resp = http_post(cfg::TOOL_SERVER_BASE + "/confirm", payload.dump());
    if (resp.empty()) return "[-] Tool server did not respond.";
    try {
        auto j = json::parse(resp);
        return json_str(j, "result", "[-] No result in response.");
    } catch (...) {
        return "[-] Invalid JSON from tool server.";
    }
}

std::string ToolBridge::fetch_active_context() {
    CURL* curl = curl_easy_init();
    if (!curl) return "{}";

    std::string response;
    std::string url = cfg::TOOL_SERVER_BASE + "/session";
    curl_easy_setopt(curl, CURLOPT_URL,           url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA,     &response);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT,       5L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL,      1L);

    CURLcode res = curl_easy_perform(curl);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK || response.empty()) return "{}";
    return response;
}

std::string ToolBridge::fetch_preferences() {
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - last_prefs_fetch_).count();
    
    // Cache preferences for 60 seconds to avoid per-turn HTTP overhead
    if (!last_preferences_.empty() && elapsed < 60) {
        return last_preferences_;
    }
    
    try {
        std::string reply = http_get(cfg::TOOL_SERVER_BASE + "/preferences");
        if (reply.empty()) return last_preferences_;
        auto j = nlohmann::json::parse(reply);
        last_preferences_ = json_str(j, "preferences");
        last_prefs_fetch_ = now;
        return last_preferences_;
    } catch (...) {
        return last_preferences_;
    }
}

// ── Learning: log interaction ───────────────────────────────────────

void ToolBridge::log_interaction(const std::string& command,
                                  const std::string& path,
                                  const std::vector<std::string>& tools_used,
                                  const std::string& result,
                                  bool success) {
    json tools_arr = json::array();
    for (auto& t : tools_used) tools_arr.push_back(t);

    json payload = {
        {"command", command},
        {"path",    path},
        {"tools",   tools_arr},
        {"result",  result.substr(0, 300)},   // truncate for storage
        {"success", success},
    };

    // Fire-and-forget — ignore errors
    http_post(cfg::TOOL_SERVER_BASE + "/learn", payload.dump());
}

// ── Screen context fetch (PATH 4) ───────────────────────────────────

std::string ToolBridge::fetch_screen_context(bool force) {
    std::string url = cfg::TOOL_SERVER_BASE + "/screen_context";
    if (force) url += "?force=true";

    auto resp = http_get(url);
    if (resp.empty()) return "";

    try {
        auto j = json::parse(resp);
        return json_str(j, "context_block");
    } catch (...) {
        return "";
    }
}

// ── Deep intent analysis (PATH 4) ───────────────────────────────────

ToolBridge::DeepIntentResult ToolBridge::fetch_deep_intent(
    const std::string& command) {

    DeepIntentResult result;
    json payload = {{"command", command}};
    auto resp = http_post(cfg::TOOL_SERVER_BASE + "/deep_intent",
                          payload.dump());

    if (resp.empty()) return result;

    try {
        auto j = json::parse(resp);
        result.task_type          = json_str(j, "task_type", "general");
        result.task_prompt        = json_str(j, "task_prompt");
        result.deep_intent_prompt = json_str(j, "deep_intent_prompt");
        result.screen_context     = json_str(j, "screen_context");
        result.user_profile       = json_str(j, "user_profile");
        result.people_context     = json_str(j, "people_context");
    } catch (...) {}

    return result;
}

// ── Structured Task Executor (STE) ──────────────────────────────────

ToolBridge::TaskExecResult ToolBridge::execute_task(
    const std::string& command) {

    TaskExecResult result;
    json payload = {{"command", command}};

    // Use a longer timeout — tasks can take 30-60s with waits
    CURL* curl = curl_easy_init();
    if (!curl) {
        result.status = "failed";
        result.result = "[-] HTTP client init failed.";
        return result;
    }

    std::string url = cfg::TOOL_SERVER_BASE + "/plan_and_execute";
    std::string body = payload.dump();
    std::string response;

    struct curl_slist* hdrs = nullptr;
    hdrs = curl_slist_append(hdrs, "Content-Type: application/json");

    curl_easy_setopt(curl, CURLOPT_URL,           url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER,     hdrs);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS,     body.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION,  write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA,      &response);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT,        cfg::TASK_EXEC_TIMEOUT_SEC);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL,       1L);

    CURLcode res = curl_easy_perform(curl);
    curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK) {
        result.status = "failed";
        result.result = std::string("[-] HTTP error: ") + curl_easy_strerror(res);
        return result;
    }

    if (response.empty()) {
        result.status = "failed";
        result.result = "[-] Empty response from task executor.";
        return result;
    }

    try {
        auto j = json::parse(response);
        result.status          = json_str(j, "status", "failed");
        result.result          = json_str(j, "result");
        result.steps_completed = j.value("steps_completed", 0);
        result.total_steps     = j.value("total_steps", 0);

        // Parse spoken updates
        if (j.contains("spoken_updates") && j["spoken_updates"].is_array()) {
            for (auto& s : j["spoken_updates"])
                if (s.is_string()) result.spoken.push_back(s.get<std::string>());
        }

        // Parse file choices for CLARIFYING state
        if (j.contains("pending_file_choices") &&
            j["pending_file_choices"].is_array()) {
            for (auto& fc : j["pending_file_choices"]) {
                result.file_choices.push_back({
                    json_str(fc, "name", "?"),
                    json_str(fc, "path", "?"),
                });
            }
        }
    } catch (const std::exception& e) {
        result.status = "failed";
        result.result = "[-] JSON parse error: " + std::string(e.what());
    }

    return result;
}

}  // namespace demascas
