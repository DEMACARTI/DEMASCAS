/**
 * DEMASCAS — daemon/src/whisper_engine.cpp
 * whisper.cpp integration for bare-metal STT.
 */

#include "whisper_engine.hpp"
#include "config.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <iostream>
#include <vector>

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
    // tiny + reduced audio_ctx measured unstable (repetition loops) → full window
    return run(tiny_, pcm, /*adaptive_ctx=*/false);
}

std::string WhisperEngine::transcribe_command(const std::vector<float>& pcm) {
    return run(base_, pcm, demascas::config::WHISPER_ADAPTIVE_CTX);
}

void WhisperEngine::warm_up() {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<float> silence(
        static_cast<size_t>(demascas::config::WHISPER_MIN_SEC * 16000), 0.0f);
    run(tiny_, silence, false);
    run(base_, silence, demascas::config::WHISPER_ADAPTIVE_CTX);
    float s = std::chrono::duration<float>(std::chrono::steady_clock::now() - t0).count();
    std::cout << "[✓] Whisper warm-up done (" << s << " s)\n";
}

/// "X. X." / "X? X?" → "X." — guards against whisper repetition loops.
static std::string collapse_repeated_sentences(const std::string& text) {
    std::vector<std::string> parts;
    std::string cur;
    for (char c : text) {
        cur += c;
        if (c == '.' || c == '?' || c == '!') { parts.push_back(cur); cur.clear(); }
    }
    if (!cur.empty()) parts.push_back(cur);
    if (parts.size() < 2) return text;
    auto norm = [](const std::string& s) {
        std::string o;
        for (char c : s)
            if (std::isalnum(static_cast<unsigned char>(c)) || c == ' ')
                o += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        auto a = o.find_first_not_of(' '), b = o.find_last_not_of(' ');
        return a == std::string::npos ? std::string() : o.substr(a, b - a + 1);
    };
    std::string out, prev;
    for (auto& p : parts) {
        std::string n = norm(p);
        if (!n.empty() && n == prev) continue;   // drop consecutive duplicate
        out += p;
        prev = n;
    }
    return out;
}

std::string WhisperEngine::run(struct whisper_context* ctx,
                                const std::vector<float>& pcm_in,
                                bool adaptive_ctx) {
    if (!ctx || pcm_in.empty()) return "";
    namespace cfg = demascas::config;

    // whisper.cpp ignores clips shorter than ~1 s → pad with silence.
    std::vector<float> pcm = pcm_in;
    const size_t min_samples = static_cast<size_t>(cfg::WHISPER_MIN_SEC * 16000);
    if (pcm.size() < min_samples) pcm.resize(min_samples, 0.0f);

    int audio_ctx = 0;  // 0 = full 30 s window
    if (adaptive_ctx) {
        // 50 encoder frames per second of audio + a safety margin
        // (+256 frames, min 512 — tighter values caused repetition in tests).
        int frames = static_cast<int>(pcm.size() / 16000.0 * 50.0) + 256;
        frames = ((frames + 63) / 64) * 64;
        audio_ctx = std::max(512, frames);
        if (audio_ctx >= 1500) audio_ctx = 0;
    }

    std::string text = run_once(ctx, pcm, audio_ctx);
    if (audio_ctx != 0 && text.empty())
        text = run_once(ctx, pcm, 0);  // fall back to the full window
    return collapse_repeated_sentences(text);
}

std::string WhisperEngine::run_once(struct whisper_context* ctx,
                                     const std::vector<float>& pcm,
                                     int audio_ctx) {
    if (!ctx || pcm.empty()) return "";

    struct whisper_full_params params =
        whisper_full_default_params(WHISPER_SAMPLING_GREEDY);

    params.print_progress   = false;
    params.print_timestamps = false;
    params.print_realtime   = false;
    params.print_special    = false;
    params.single_segment   = true;
    params.language         = "en";
    params.n_threads        = n_threads_;  // hardware-aware (4 on an M1)
    params.no_context       = true;
    params.audio_ctx        = audio_ctx;

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
