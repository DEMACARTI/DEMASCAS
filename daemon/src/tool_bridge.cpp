/**
 * DEMASCAS — daemon/src/tool_bridge.cpp
 * libcurl-based HTTP bridge to the Python tool server.
 */

#include "tool_bridge.hpp"
#include "config.hpp"

#include <curl/curl.h>
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
                if (j.value("status", "") == "ok") {
                    int n = j.value("tools", 0);
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
        if (j.contains("error"))
            return "[-] " + j["error"].get<std::string>();
        return j.value("result", "[-] No result in response.");
    } catch (...) {
        return "[-] Invalid JSON from tool server.";
    }
}

// ── Tool execution with screen state ────────────────────────────────

ToolBridge::ToolResult ToolBridge::execute_with_screen(
    const std::string& name, const json& args) {

    json payload = {{"name", name}, {"arguments", args}};
    auto resp = http_post(cfg::TOOL_EXEC_URL, payload.dump());

    ToolResult tr;
    if (resp.empty()) {
        tr.result = "[-] Tool server did not respond.";
        return tr;
    }

    try {
        auto j = json::parse(resp);
        if (j.contains("error")) {
            tr.result = "[-] " + j["error"].get<std::string>();
            return tr;
        }
        tr.result = j.value("result", "[-] No result in response.");
        tr.screen_state = j.value("screen_state", "");
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
    // URL-encode the query (minimal: just spaces → +)
    std::string encoded;
    for (char c : query) {
        if (c == ' ') encoded += '+';
        else if (c == '&') encoded += "%26";
        else if (c == '=') encoded += "%3D";
        else if (c == '?') encoded += "%3F";
        else encoded += c;
    }

    auto resp = http_get(cfg::TOOL_SERVER_BASE + "/context?query=" + encoded);
    if (resp.empty()) return "";

    try {
        auto j = json::parse(resp);
        return j.value("context_text", "");
    } catch (...) {
        return "";
    }
}

ToolBridge::AgentResult ToolBridge::execute_agent(
    const std::string& command, const std::string& context_json) {

    AgentResult out;
    out.success = false;
    out.summary = "";
    out.steps_taken = 0;
    out.error = "";
    out.raw_json = "";

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
    curl_easy_setopt(curl, CURLOPT_TIMEOUT,       90L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL,      1L);

    CURLcode res = curl_easy_perform(curl);
    curl_slist_free_all(hdrs);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK) {
        out.error = std::string("HTTP error: ") + curl_easy_strerror(res);
        return out;
    }

    if (response.empty()) {
        out.error = "Empty response from /agent endpoint.";
        return out;
    }

    out.raw_json = response;
    try {
        auto j = json::parse(response);
        out.success = j.value("success", false);
        out.summary = j.value("summary", "");
        out.steps_taken = j.value("steps_taken", 0);
        out.error = j.value("error", "");
    } catch (const std::exception& e) {
        out.error = std::string("JSON parse error: ") + e.what();
    }

    return out;
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
        last_preferences_ = j.value("preferences", "");
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
        return j.value("context_block", "");
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
        result.task_type          = j.value("task_type", "general");
        result.task_prompt        = j.value("task_prompt", "");
        result.deep_intent_prompt = j.value("deep_intent_prompt", "");
        result.screen_context     = j.value("screen_context", "");
        result.user_profile       = j.value("user_profile", "");
        result.people_context     = j.value("people_context", "");
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
        result.status          = j.value("status", "failed");
        result.result          = j.value("result", "");
        result.steps_completed = j.value("steps_completed", 0);
        result.total_steps     = j.value("total_steps", 0);

        // Parse spoken updates
        if (j.contains("spoken_updates") && j["spoken_updates"].is_array()) {
            for (auto& s : j["spoken_updates"])
                result.spoken.push_back(s.get<std::string>());
        }

        // Parse file choices for CLARIFYING state
        if (j.contains("pending_file_choices") &&
            j["pending_file_choices"].is_array()) {
            for (auto& fc : j["pending_file_choices"]) {
                result.file_choices.push_back({
                    fc.value("name", "?"),
                    fc.value("path", "?"),
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
