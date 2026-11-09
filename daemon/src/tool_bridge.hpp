/**
 * DEMASCAS — daemon/src/tool_bridge.hpp
 * HTTP bridge to the Python tool micro-service (Option A: Local Sockets).
 *
 * The C++ daemon sends JSON payloads to localhost:5001; the Python
 * Flask server executes the tool and returns the result.  Latency
 * is sub-millisecond on loopback.
 */

#pragma once

#include <string>
#include <vector>
#include <chrono>
#include <mutex>
#include <condition_variable>
#include <nlohmann/json.hpp>

namespace demascas {

class ToolBridge {
public:
    ToolBridge() = default;

    /// Block until the Python tool server responds to /health.
    bool wait_for_server(int max_retries = 10);

    /// GET /tools — fetch tool schemas (OpenAI function-calling format).
    nlohmann::json fetch_tool_schemas();

    /// POST /tool — execute a named tool with JSON arguments.
    std::string execute(const std::string& name,
                        const nlohmann::json& args);

    // ── Learning integration ────────────────────────────────────

    /// GET /context?query=... — fetch learned context for the query.
    /// Returns the "context_text" field (pre-formatted for prompt injection),
    /// or empty string on failure.
    std::string fetch_learned_context(const std::string& query);

    /// POST /learn — log a completed interaction for learning.
    /// Fire-and-forget: errors are silently ignored.
    void log_interaction(const std::string& command,
                         const std::string& path,
                         const std::vector<std::string>& tools_used,
                         const std::string& result,
                         bool success = true);

    // ── Tool filtering ──────────────────────────────────────────

    /// POST /tools_for_query — fetch filtered tool schemas for a specific query.
    /// Returns only the most relevant 3-8 tools instead of all 30.
    nlohmann::json fetch_filtered_tools(const std::string& query,
                                         int max_tools = 8);

    // ── Screen context (PATH 4) ─────────────────────────────────

    /// GET /screen_context — fetch structured screen digest.
    /// Returns context_block string for system prompt injection.
    std::string fetch_screen_context(bool force = false);

    struct AgentResult {
        bool        success = false;
        bool        unavailable = false;   // agent system not loaded / unreachable
        std::string summary;
        int         steps_taken = 0;
        std::string error;
        std::string raw_json;
        std::vector<std::string> tool_calls;
        // Multi-match file search → CLARIFYING state
        std::vector<std::pair<std::string, std::string>> file_choices;  // {name, path}
        // Destructive action waiting for a spoken yes/no → CONFIRMING state
        std::string confirm_token;
        std::string confirm_speech;
        std::string pending_tool;
    };

    AgentResult execute_agent(const std::string& command,
                              const std::string& context_json = "{}");
    std::string fetch_active_context();

    /// GET /llm — the user's plugged-in LLM + hardware-aware thread counts.
    struct LLMRuntimeConfig {
        bool        configured = false;
        std::string runtime;
        std::string chat_url;
        std::string model;
        int         context_tokens  = 4096;
        int         whisper_threads = 4;
        int         tts_threads     = 4;
    };
    LLMRuntimeConfig fetch_llm_config();

    /// POST /confirm — approve or reject a pending destructive action.
    /// Returns the tool result (or "Cancelled.").
    std::string confirm(const std::string& token, bool approve);

    /// Fetch user preferences and rules from the Python server
    std::string fetch_preferences();

    /// POST /deep_intent — fetch deep intent analysis for a command.
    /// Returns task_type, task-specific prompt, people context, screen context.
    struct DeepIntentResult {
        std::string task_type;          // "compose_email", "smart_reply", etc.
        std::string task_prompt;        // task-specific system prompt
        std::string deep_intent_prompt; // deep intent system prompt
        std::string screen_context;
        std::string user_profile;
        std::string people_context;
    };
    DeepIntentResult fetch_deep_intent(const std::string& command);

    // ── Screen state extraction ─────────────────────────────────

    /// Execute a tool and return both result and screen_state (if present).
    /// screen_state is auto-captured by the server for UI-mutating tools.
    struct ToolResult {
        std::string result;
        std::string screen_state;  // empty if not a UI-mutating tool
        bool        has_success = false;  // server reported an explicit success flag
        bool        success     = true;
    };
    /// include_screen=false skips the server's ~1-2 s accessibility-tree read.
    ToolResult execute_with_screen(const std::string& name,
                                    const nlohmann::json& args,
                                    bool include_screen = true);

    // ── Structured Task Executor (STE) ──────────────────────────

    /// POST /plan_and_execute — plan and execute a multi-step task.
    /// Returns structured result with spoken progress updates.
    struct TaskExecResult {
        std::string status;              // "ok" | "partial" | "failed" | "clarifying"
        std::string result;              // final result text
        int steps_completed = 0;         // how many steps successfully ran
        int total_steps = 0;             // total planned steps
        std::vector<std::string> spoken; // progress messages for TTS
        // For CLARIFYING state (find_file multi-match)
        std::vector<std::pair<std::string, std::string>> file_choices; // {name, path}
    };
    TaskExecResult execute_task(const std::string& command);

    /// Read a string field, tolerating missing / null / non-string values.
    static std::string json_str(const nlohmann::json& j, const char* key,
                                const std::string& def = "");

    /// Percent-encode a string for use in a URL query component.
    static std::string url_encode(const std::string& s);

    // ── Static HTTP helpers (used by LLMClient too) ──────────
    static std::string http_post(const std::string& url,
                                  const std::string& body);
    static std::string http_get(const std::string& url);
    /// Last known user preferences block
    std::string last_preferences_;
    std::chrono::steady_clock::time_point last_prefs_fetch_;

};

}  // namespace demascas
