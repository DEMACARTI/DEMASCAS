/**
 * DEMASCAS — daemon/src/tool_bridge.hpp
 * HTTP bridge to the Python tool micro-service (Option A: Local Sockets).
 *
 * The C++ daemon sends JSON payloads to localhost:5001; the Python
 * Flask server executes the tool and returns the result.  Latency
 * is sub-millisecond on loopback.
 *
 * Learning endpoints:
 *   GET  /context?query=...  — fetch learned context before Ollama call
 *   POST /learn              — log interaction after command completes
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

    /// GET /tools — fetch Ollama-compatible tool schemas.
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

    // ── Static HTTP helpers (used by OllamaClient too) ─────────
    static std::string http_post(const std::string& url,
                                  const std::string& body);
    static std::string http_get(const std::string& url);
};

}  // namespace demascas
