/**
 * DEMASCAS — aec.cpp  (see aec.hpp)
 */
#include "aec.hpp"

#include <algorithm>
#include <cmath>

#ifdef HAS_SPEEXDSP
#include <speex/speex_echo.h>
#include <speex/speex_preprocess.h>
#include <speex/speex_resampler.h>
#endif

namespace demascas {

namespace {
inline int16_t to_s16(float x) {
    return static_cast<int16_t>(std::max(-32768.0f, std::min(32767.0f, x * 32767.0f)));
}
}  // namespace

EchoCanceller::EchoCanceller(int sample_rate, int tail_ms, int suppress_active_db, bool residual)
    : sr_(sample_rate), suppress_active_db_(suppress_active_db), residual_(residual), epoch_(clk::now()), ring_(static_cast<size_t>(sample_rate) * 4, 0.0f) {
    frame_ = sr_ / 100;
#ifdef HAS_SPEEXDSP
    auto* st = speex_echo_state_init(frame_, sr_ * tail_ms / 1000);
    int rate = sr_;
    speex_echo_ctl(st, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);
    auto* pp = speex_preprocess_state_init(frame_, sr_);
    speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_ECHO_STATE, st);
    int on = 1;
    speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_DENOISE, &on);
    // Residual-echo suppression while the USER is talking (double-talk).
    // speex's default (-15 dB) also crushes the first words of an
    // interruption ("Stop."), so keep it gentle.
    int active = suppress_active_db_;
    speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE, &active);
    echo_ = st;
    pre_ = pp;
#else
    (void)tail_ms;
#endif
}

EchoCanceller::~EchoCanceller() {
#ifdef HAS_SPEEXDSP
    if (resampler_) speex_resampler_destroy(static_cast<SpeexResamplerState*>(resampler_));
    if (pre_) speex_preprocess_state_destroy(static_cast<SpeexPreprocessState*>(pre_));
    if (echo_) speex_echo_state_destroy(static_cast<SpeexEchoState*>(echo_));
#endif
}

bool EchoCanceller::available() const { return echo_ != nullptr; }

void EchoCanceller::push_reference(const float* samples, size_t n, int rate,
                                   clk::time_point when) {
    if (!samples || n == 0 || rate <= 0) return;
    std::lock_guard<std::mutex> lk(mu_);
    // Absolute 16 kHz index where this chunk starts playing.
    auto us = std::chrono::duration_cast<std::chrono::microseconds>(when - epoch_).count();
    int64_t start = us * sr_ / 1000000;
    const int64_t cap = static_cast<int64_t>(ring_.size());
    std::vector<float> res;
    const float* src = samples;
    size_t out_n = n;
#ifdef HAS_SPEEXDSP
    if (rate != sr_) {
        // Proper band-limited resampling (Kokoro 24 kHz → 16 kHz).  The real
        // echo reaches the mic through CoreAudio's high-quality resampler; a
        // dull linear-interpolated reference cancelled it much worse.
        if (!resampler_ || resampler_rate_ != rate) {
            if (resampler_) speex_resampler_destroy(static_cast<SpeexResamplerState*>(resampler_));
            int err = 0;
            resampler_ = speex_resampler_init(1, static_cast<spx_uint32_t>(rate),
                                              static_cast<spx_uint32_t>(sr_), 5, &err);
            resampler_rate_ = rate;
        }
        res.resize(n * static_cast<size_t>(sr_) / static_cast<size_t>(rate) + 16);
        spx_uint32_t in_len = static_cast<spx_uint32_t>(n), out_len = static_cast<spx_uint32_t>(res.size());
        speex_resampler_process_float(static_cast<SpeexResamplerState*>(resampler_), 0,
                                      samples, &in_len, res.data(), &out_len);
        res.resize(out_len);
        src = res.data();
        out_n = out_len;
    }
#endif
    for (size_t i = 0; i < out_n; ++i) {
        int64_t idx = start + static_cast<int64_t>(i);
        ring_[static_cast<size_t>(((idx % cap) + cap) % cap)] = src[i];
    }
    written_upto_ = std::max(written_upto_, start + static_cast<int64_t>(out_n));
}

float EchoCanceller::ref_at(int64_t idx) {
    const int64_t cap = static_cast<int64_t>(ring_.size());
    if (idx < 0 || idx >= written_upto_ || idx < written_upto_ - cap) return 0.0f;
    size_t k = static_cast<size_t>(((idx % cap) + cap) % cap);
    float v = ring_[k];
    return v;
}

std::vector<float> EchoCanceller::process(const std::vector<float>& mic,
                                          clk::time_point captured_end) {
#ifndef HAS_SPEEXDSP
    (void)captured_end;
    return mic;
#else
    if (!echo_ || mic.empty()) return mic;
    std::lock_guard<std::mutex> lk(mu_);
    auto us = std::chrono::duration_cast<std::chrono::microseconds>(captured_end - epoch_).count();
    int64_t end_idx = us * sr_ / 1000000;
    int64_t start_idx = end_idx - static_cast<int64_t>(mic.size());
    if (pending_.empty() || pending_start_ + static_cast<int64_t>(pending_.size()) != start_idx) {
        // Gap or first call: realign (drop the stale partial frame).
        pending_.clear();
        pending_start_ = start_idx;
    }
    pending_.insert(pending_.end(), mic.begin(), mic.end());

    std::vector<float> out;
    out.reserve(pending_.size());
    std::vector<int16_t> m(frame_), r(frame_), o(frame_);
    size_t pos = 0;
    while (pending_.size() - pos >= static_cast<size_t>(frame_)) {
        bool live = false;
        for (int k = 0; k < frame_; ++k) {
            m[k] = to_s16(pending_[pos + k]);
            float rv = ref_at(pending_start_ + static_cast<int64_t>(pos) + k);
            live |= std::fabs(rv) > 1e-4f;
            r[k] = to_s16(rv);
        }
        speex_echo_cancellation(static_cast<SpeexEchoState*>(echo_), m.data(), r.data(), o.data());
        if (residual_) speex_preprocess_run(static_cast<SpeexPreprocessState*>(pre_), o.data());
        for (int k = 0; k < frame_; ++k) out.push_back(o[k] / 32768.0f);
        if (live) {
            adapted_samples_ += frame_;
            double me = 0, oe = 0;
            for (int k = 0; k < frame_; ++k) {
                me += double(m[k]) * m[k];
                oe += double(o[k]) * o[k];
            }
            mic_e_ = 0.98 * mic_e_ + 0.02 * me;          // ~0.5 s smoothing
            out_e_ = 0.98 * out_e_ + 0.02 * oe;
            erle_db_ = static_cast<float>(10.0 * std::log10((mic_e_ + 1.0) / (out_e_ + 1.0)));
            if (!converged_ && adapted_samples_ >= 2 * sr_ && erle_db_ >= 12.0f)
                converged_ = true;
        }
        pos += frame_;
    }
    pending_.erase(pending_.begin(), pending_.begin() + static_cast<long>(pos));
    pending_start_ += static_cast<int64_t>(pos);
    return out;
#endif
}

}  // namespace demascas
