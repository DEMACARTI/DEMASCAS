/**
 * DEMASCAS — vad.hpp
 * "Is a HUMAN talking right now?" — Silero VAD (644 KB model, ~0.2 ms per
 * 32 ms window on CPU) through sherpa-onnx, which the daemon already links
 * for Kokoro.  Rejects fans, typing, music, door bangs; a voice must last
 * min_speech_ms before it counts.
 *
 * Falls back to an RMS energy test when sherpa-onnx or the model is missing.
 */
#pragma once

#include <algorithm>
#include <cstddef>
#include <deque>
#include <string>
#include <vector>

namespace demascas {

class SpeechDetector {
public:
    SpeechDetector() = default;
    ~SpeechDetector();
    SpeechDetector(const SpeechDetector&) = delete;
    SpeechDetector& operator=(const SpeechDetector&) = delete;

    /// Load the Silero model.  Returns false → energy fallback is used.
    bool init(const std::string& model_path, int sample_rate = 16000,
              float threshold = 0.5f, float min_speech_sec = 0.25f,
              float min_silence_sec = 0.25f);

    /// Feed mic samples (any length).
    void accept(const float* samples, size_t n);

    /// True while speech is being detected (after min_speech_sec).
    bool speaking() const;

    /// Forget all state (call before a new capture).
    void reset();

    bool using_model() const { return handle_ != nullptr; }

private:
    const void* handle_ = nullptr;     // SherpaOnnxVoiceActivityDetector*
    float       fallback_energy_ = 0.0f;
    float       energy_threshold_ = 0.008f;
};

/**
 * Barge-in decision while DEMASCAS is speaking.  Pure logic (unit-tested).
 *
 * Inputs per mic poll (~50 ms): raw mic level, the echo-cancelled level,
 * whether the VAD hears a human in the CANCELLED audio, and whether the
 * echo canceller has converged.
 *
 * Silero is nearly volume-blind, so the faint residue of our own voice
 * (~20 dB down after AEC) can still look like "speech".  The user's voice,
 * though, survives cancellation: it keeps ≥ ~45 % of the mic energy even
 * when 6 dB quieter than the speakers, while echo residue keeps ~10 %.
 * Before the canceller converges, fall back to "speak over the speakers".
 * Measured on waveform simulations: no false triggers from echo, fan,
 * typing or door bangs; normal and soft voices interrupt in ~0.3–0.6 s.
 */
class BargeInDetector {
public:
    /// polls: ~50 ms each.  window: how far back energy evidence may be —
    /// matches the VAD's min-speech lag (it says "speech" ~250 ms after a
    /// word starts, often after a short word like "Stop." has ended).
    explicit BargeInDetector(int window_polls = 6, int strong_needed = 2,
                             float min_keep = 0.4f, float min_level = 0.008f,
                             float legacy_threshold = 0.18f)
        : window_(window_polls), strong_needed_(strong_needed), min_keep_(min_keep),
          min_level_(min_level), legacy_(legacy_threshold) {}

    bool update(float mic_rms, float clean_rms, bool speech_in_clean, bool aec_converged,
                bool raw_speech = false) {
        if (!aec_converged) {                      // canceller still learning
            strong_.clear();
            return raw_speech && mic_rms >= legacy_;
        }
        bool strong = clean_rms >= min_level_ &&
                      clean_rms >= min_keep_ * std::max(mic_rms, 1e-6f);
        strong_.push_back(strong);
        if (static_cast<int>(strong_.size()) > window_) strong_.pop_front();
        int n = 0;
        for (bool v : strong_) n += v;
        return speech_in_clean && n >= strong_needed_;
    }

    void reset() { strong_.clear(); }

private:
    int   window_;
    int   strong_needed_;
    float min_keep_;
    float min_level_;
    float legacy_;
    std::deque<bool> strong_;
};

}  // namespace demascas
