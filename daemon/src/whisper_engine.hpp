/**
 * DEMASCAS — daemon/src/whisper_engine.hpp
 * Speech-to-text via whisper.cpp — zero-dependency, bare-metal
 * C/C++ Whisper inference optimised for Apple Silicon ANE.
 *
 * Maintains two contexts:
 *   • tiny  (~39 MB)  — ultra-fast wake-word scanning
 *   • base  (~140 MB) — accurate command transcription
 */

#pragma once

#include <string>
#include <vector>

#include "whisper.h"

namespace demascas {

class WhisperEngine {
public:
    WhisperEngine() = default;
    ~WhisperEngine();

    WhisperEngine(const WhisperEngine&)            = delete;
    WhisperEngine& operator=(const WhisperEngine&) = delete;

    /// Load both GGML models.  Returns false if either fails.
    bool init(const std::string& tiny_path, const std::string& base_path);

    /// Release VRAM / model weights.
    void shutdown();

    /// Wake-word transcription (tiny model, full window; ~130 ms on M1).
    std::string transcribe_wake(const std::vector<float>& pcm);

    /// Command transcription (base model, clip-sized window; ~90 ms on M1).
    std::string transcribe_command(const std::vector<float>& pcm);

    /// Run both models once on silence so Metal pipelines/graphs are built
    /// at startup instead of during the user's first command.
    void warm_up();

    /// Set initial prompt context for better recognition.
    void set_prompt(const std::string& prompt);

    /// CPU threads for decoding (hardware-aware, from GET /llm).
    void set_threads(int n) { n_threads_ = n > 0 ? n : 4; }

private:
    std::string run(struct whisper_context* ctx, const std::vector<float>& pcm,
                    bool adaptive_ctx);
    std::string run_once(struct whisper_context* ctx, const std::vector<float>& pcm,
                         int audio_ctx);

    std::string prompt_;
    int n_threads_ = 4;

    struct whisper_context* tiny_ = nullptr;
    struct whisper_context* base_ = nullptr;
};

}  // namespace demascas
