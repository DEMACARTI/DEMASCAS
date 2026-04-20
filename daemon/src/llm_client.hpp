/**
 * DEMASCAS — daemon/src/llm_client.hpp
 * The Brain: HTTP client for the local LLM server (Ollama or MLX).
 *
 * Manages conversation history, system prompt generation (IST clock),
 * and the ReAct tool-calling loop — all in C++ with zero Python overhead.
 *
 * Uses OpenAI-compatible SSE streaming via /v1/chat/completions.
 * Tool calling is pure-JSON: the model outputs {"tool":"name","args":{}}
 * or {"done":true,"say":"text"} — no native tool_calls API.
 *
 * Streaming:
 *   chat_with_tools_streaming() and fast_chat_streaming() stream tokens
 *   and call an on_sentence callback as sentence boundaries are detected.
 *   This lets the TTS start speaking while the LLM is still generating.
 */

#pragma once

#include <atomic>
#include <functional>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace demascas {

class ToolBridge;   // forward declaration

/// Callback type for streaming — called with each complete sentence.
using SentenceCallback = std::function<void(const std::string&)>;

/// Parsed action from the LLM's raw JSON output.
struct LLMAction {
    bool        is_done = false;    // true = final answer, no more tools
    std::string tool;               // tool name (empty if done)
    nlohmann::json args;            // tool arguments
    std::string say;                // spoken text (when done)
    std::string raw;                // raw LLM output for debugging
};

class LLMClient {
public:
    LLMClient() = default;

    /// Health-check the LLM text server.
    void prewarm();

    // ── Streaming ───────────────────────────────────────────────

    /// Streaming fast-path: calls on_sentence for each sentence as generated.
    /// Cancel via cancel flag (atomic bool).
    std::string fast_chat_streaming(const std::string& user_msg,
                                     SentenceCallback on_sentence,
                                     std::atomic<bool>& cancel);

    /// Streaming full-path: ReAct loop + sentence-by-sentence TTS.
    std::string chat_with_tools_streaming(const std::string& user_msg,
                                           ToolBridge& bridge,
                                           SentenceCallback on_sentence,
                                           std::atomic<bool>& cancel,
                                           const std::string& learned_context = "");

    /// Clear conversation history (between independent interactions).
    void reset();

    /// Save conversation history to disk for session persistence.
    void save_session() const;

    /// Load conversation history from disk (call on startup).
    void load_session();

    /// Returns the list of tool names used in the last chat_with_tools call.
    const std::vector<std::string>& last_tools_used() const { return last_tools_; }

    /// Returns the path taken in the last call ("shortcut", "fast", "tool").
    const std::string& last_path() const { return last_path_; }

    // ── Planner — decompose multi-step commands ─────────────────

    /// Check if a command requires multiple steps (heuristic).
    bool is_multi_step_command(const std::string& command) const;

    /// Use the LLM to decompose a complex command into ordered steps.
    /// Returns a vector of step descriptions (natural language).
    std::vector<std::string> plan_steps(const std::string& command,
                                         ToolBridge& bridge,
                                         std::atomic<bool>& cancel);

    /// Use the LLM to decompose a complex command into structured JSON steps.
    /// Returns a vector of JSON objects: [{"tool": "...", "args": {...}}, ...]
    /// Supports inter-step result passing via {result_of_step_N} tokens.
    std::vector<nlohmann::json> plan_steps_structured(const std::string& command,
                                                       ToolBridge& bridge,
                                                       std::atomic<bool>& cancel);

    // ── PATH 4: Deep Intent ─────────────────────────────────────

    /// Check if a command requires deep intent analysis (PATH 4).
    /// High-complexity commands that benefit from Understand→Plan→Execute.
    bool is_deep_intent_command(const std::string& command) const;

    /// Deep intent path: 3-phase (Understand → Plan → Execute).
    /// Returns the final spoken response.
    std::string deep_intent_streaming(
        const std::string& user_msg,
        ToolBridge& bridge,
        SentenceCallback on_sentence,
        std::atomic<bool>& cancel);

    /// Parsed deep intent from the LLM's structured output.
    struct DeepIntentAction {
        std::string intent;         // 1-sentence intent summary
        std::string task_type;      // email, message, research, etc.
        std::vector<std::string> plan;  // ordered steps
        bool needs_confirm = false;
        bool is_done = false;
        std::string say;            // spoken text (if done)
        std::string first_tool;     // first tool to call
        nlohmann::json first_args;  // first tool arguments
    };

    DeepIntentAction parse_deep_intent(const std::string& raw_text) const;

private:
    /// Build the agentic system prompt with tool list embedded as text.
    nlohmann::json build_tool_prompt(const nlohmann::json& tool_schemas,
                                      const std::string& learned_context = "") const;

    /// Build the simple Q&A system prompt (no tools).
    nlohmann::json build_simple_prompt() const;

    /// POST to LLM /v1/chat/completions with SSE stream=true.
    /// Calls on_sentence for each sentence boundary.
    /// Returns the full generated text.
    std::string call_mlx_streaming(const nlohmann::json& messages,
                                    SentenceCallback on_sentence,
                                    std::atomic<bool>& cancel,
                                    int max_tokens,
                                    float temperature);

    /// POST to LLM /v1/chat/completions with stream=false.
    /// Used for planner (no streaming needed).
    std::string call_mlx_blocking(const nlohmann::json& messages,
                                   int max_tokens);

    /// Parse the LLM's raw text output into a structured action.
    LLMAction parse_llm_action(const std::string& raw_text) const;

    /// Format tool schemas as compact text for system prompt injection.
    std::string format_tools_as_text(const nlohmann::json& schemas) const;

    void trim_history();

    std::vector<nlohmann::json> history_;
    std::vector<std::string>    last_tools_;    // tools used in last interaction
    std::string                 last_path_;     // "shortcut" | "fast" | "tool"
    int                         error_count_ = 0;  // consecutive tool errors in current ReAct loop
};

}  // namespace demascas
