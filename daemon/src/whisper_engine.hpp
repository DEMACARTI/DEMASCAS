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

    /// Fast transcription (tiny model, ~50 ms on M1).
    std::string transcribe_wake(const std::vector<float>& pcm);

    /// Accurate transcription (base model, ~200 ms on M1).
    std::string transcribe_command(const std::vector<float>& pcm);

    /// Set initial prompt context for better recognition.
    void set_prompt(const std::string& prompt);

private:
    std::string run(struct whisper_context* ctx,
                    const std::vector<float>& pcm);

    std::string prompt_;

    struct whisper_context* tiny_ = nullptr;
    struct whisper_context* base_ = nullptr;
};

}  // namespace demascas
