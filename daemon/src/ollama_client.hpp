/**
 * DEMASCAS — daemon/src/ollama_client.hpp
 * The Brain: HTTP client for the local Ollama (llama.cpp) server.
 *
 * Manages conversation history, system prompt generation (IST clock),
 * and the ReAct tool-calling loop — all in C++ with zero Python overhead.
 *
 * Streaming support:
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

class OllamaClient {
public:
    OllamaClient() = default;

    /// Prewarm the model into GPU RAM (eliminates cold-start latency).
    void prewarm();

    /// Set tool schemas (fetched from the Python tool server).
    void set_tools(const nlohmann::json& schemas);

    // ── Non-streaming (original) ────────────────────────────────

    /// Fast-path: simple Q&A — no tool schemas sent, smaller context.
    std::string fast_chat(const std::string& user_msg);

    /// Full-path: ReAct tool-calling loop with up to MAX_TOOL_DEPTH rounds.
    std::string chat_with_tools(const std::string& user_msg,
                                 ToolBridge& bridge,
                                 const std::string& learned_context = "");

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

    /// Returns the list of tool names used in the last chat_with_tools call.
    const std::vector<std::string>& last_tools_used() const { return last_tools_; }

    /// Returns the path taken in the last call ("shortcut", "fast", "tool").
    const std::string& last_path() const { return last_path_; }

private:
    nlohmann::json build_system_prompt(const std::string& learned_context = "") const;
    nlohmann::json build_simple_prompt() const;

    /// POST to Ollama /api/chat (non-streaming) and return the message object.
    nlohmann::json call_ollama(const nlohmann::json& messages,
                                bool use_tools, bool fast);

    /// POST to Ollama /api/chat with stream=true.
    /// Calls on_sentence for each sentence boundary.
    /// Returns (full_text, tool_calls_json).
    std::pair<std::string, nlohmann::json>
    call_ollama_streaming(const nlohmann::json& messages,
                          SentenceCallback on_sentence,
                          std::atomic<bool>& cancel,
                          bool use_tools, bool fast);

    void trim_history();

    std::vector<nlohmann::json> history_;
    nlohmann::json              tool_schemas_;
    std::vector<std::string>    last_tools_;   // tools used in last interaction
    std::string                 last_path_;    // "shortcut" | "fast" | "tool"
};

}  // namespace demascas
