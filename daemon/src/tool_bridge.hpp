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
    };
    ToolResult execute_with_screen(const std::string& name,
                                    const nlohmann::json& args);

    // ── Static HTTP helpers (used by LLMClient too) ──────────
    static std::string http_post(const std::string& url,
                                  const std::string& body);
    static std::string http_get(const std::string& url);
};

}  // namespace demascas
