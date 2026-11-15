/**
 * DEMASCAS — aec.hpp
 * Acoustic echo cancellation: remove DEMASCAS's own voice from the mic so
 * the user can interrupt at a normal speaking volume.
 *
 * TTS pushes every frame it sends to the speakers, stamped with when it will
 * be heard; the mic side pulls the matching reference for each 10 ms frame
 * and speexdsp's adaptive filter subtracts the echo (+ residual-echo and
 * noise suppression).  Cost measured on an M1: ~0.065 ms per 10 ms frame.
 *
 * Without speexdsp (HAS_SPEEXDSP undefined) process() is a pass-through and
 * converged() stays false, so callers fall back to the loud-voice rule.
 */
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

namespace demascas {

class EchoCanceller {
public:
    using clk = std::chrono::steady_clock;

    explicit EchoCanceller(int sample_rate = 16000, int tail_ms = 200,
                           int suppress_active_db = -5, bool residual = true);
    ~EchoCanceller();
    EchoCanceller(const EchoCanceller&) = delete;
    EchoCanceller& operator=(const EchoCanceller&) = delete;

    /// TTS thread: `n` samples at `rate` Hz that will start playing at `when`.
    void push_reference(const float* samples, size_t n, int rate, clk::time_point when);

    /// Main thread: clean mic samples (16 kHz) whose LAST sample was captured
    /// at `captured_end`.  Output has the same length.
    std::vector<float> process(const std::vector<float>& mic, clk::time_point captured_end);

    /// True once the filter has adapted to enough of our own speech to be
    /// trusted for barge-in (≈2 s of playback heard through the mic).
    /// Converged = ≥2 s of our speech heard AND the filter measurably removes
    /// it (echo reduction ≥ 12 dB).  Latches: a converged filter keeps its model.
    bool converged() const { return converged_; }
    float echo_reduction_db() const { return erle_db_; }

    bool available() const;

private:
    float ref_at(int64_t abs_index);          // caller holds mu_

    int sr_;
    int suppress_active_db_;
    bool residual_;
    int frame_ = 160;                          // 10 ms
    void* echo_ = nullptr;                     // SpeexEchoState*
    void* pre_  = nullptr;                     // SpeexPreprocessState*
    void* resampler_ = nullptr;                // SpeexResamplerState* (playback rate → 16 kHz)
    int   resampler_rate_ = 0;

    // Reference timeline: sample k of the ring is absolute 16 kHz index
    // (base_ + k), where index 0 ≙ epoch_.
    std::mutex          mu_;
    clk::time_point     epoch_;
    std::vector<float>  ring_;                 // 4 s circular timeline
    int64_t             written_upto_ = 0;     // highest absolute index written
    int64_t             adapted_samples_ = 0;  // mic samples processed with live reference
    double              mic_e_ = 0.0, out_e_ = 0.0;   // smoothed energies on echo-only-ish frames
    float               erle_db_ = 0.0f;
    bool                converged_ = false;
    std::vector<float>  pending_;              // mic samples not yet a full frame
    int64_t             pending_start_ = 0;    // absolute index of pending_[0]
};

}  // namespace demascas
