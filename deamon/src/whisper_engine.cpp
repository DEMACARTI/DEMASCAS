/**
 * DEMASCAS — daemon/src/whisper_engine.cpp
 * whisper.cpp integration for bare-metal STT.
 */

#include "whisper_engine.hpp"

#include <chrono>
#include <iostream>

namespace demascas {

WhisperEngine::~WhisperEngine() { shutdown(); }

bool WhisperEngine::init(const std::string& tiny_path,
                          const std::string& base_path) {
    auto t0 = std::chrono::steady_clock::now();

    std::cout << "[*] Loading whisper tiny  → " << tiny_path << "\n";
    struct whisper_context_params cparams = whisper_context_default_params();
    tiny_ = whisper_init_from_file_with_params(tiny_path.c_str(), cparams);
    if (!tiny_) {
        std::cerr << "[!] Failed to load: " << tiny_path << "\n";
        return false;
    }

    std::cout << "[*] Loading whisper base  → " << base_path << "\n";
    base_ = whisper_init_from_file_with_params(base_path.c_str(), cparams);
    if (!base_) {
        std::cerr << "[!] Failed to load: " << base_path << "\n";
        return false;
    }

    float elapsed = std::chrono::duration<float>(
        std::chrono::steady_clock::now() - t0).count();
    std::cout << "[✓] Whisper models loaded in " << elapsed << " s\n";
    return true;
}

void WhisperEngine::shutdown() {
    if (tiny_) { whisper_free(tiny_); tiny_ = nullptr; }
    if (base_) { whisper_free(base_); base_ = nullptr; }
}

void WhisperEngine::set_prompt(const std::string& prompt) {
    prompt_ = prompt;
}

std::string WhisperEngine::transcribe_wake(const std::vector<float>& pcm) {
    return run(tiny_, pcm);
}

std::string WhisperEngine::transcribe_command(const std::vector<float>& pcm) {
    return run(base_, pcm);
}

std::string WhisperEngine::run(struct whisper_context* ctx,
                                const std::vector<float>& pcm) {
    if (!ctx || pcm.empty()) return "";

    struct whisper_full_params params =
        whisper_full_default_params(WHISPER_SAMPLING_GREEDY);

    params.print_progress   = false;
    params.print_timestamps = false;
    params.print_realtime   = false;
    params.print_special    = false;
    params.single_segment   = true;
    params.language         = "en";
    params.n_threads        = 4;   // M1 has 4 performance cores
    params.no_context       = true;

    // Initial prompt — biases whisper towards expected vocabulary.
    // Dramatically improves name recognition and reduces hallucinations.
    if (!prompt_.empty()) {
        params.initial_prompt = prompt_.c_str();
    }

    if (whisper_full(ctx, params, pcm.data(),
                     static_cast<int>(pcm.size())) != 0) {
        std::cerr << "[!] whisper_full() failed\n";
        return "";
    }

    std::string result;
    int n_seg = whisper_full_n_segments(ctx);
    for (int i = 0; i < n_seg; ++i) {
        const char* txt = whisper_full_get_segment_text(ctx, i);
        if (txt) result += txt;
    }

    // Trim leading/trailing whitespace
    auto a = result.find_first_not_of(" \t\n\r");
    auto b = result.find_last_not_of(" \t\n\r");
    if (a == std::string::npos) return "";
    return result.substr(a, b - a + 1);
}

}  // namespace demascas
