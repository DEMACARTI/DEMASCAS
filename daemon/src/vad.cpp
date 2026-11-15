/**
 * DEMASCAS — vad.cpp  (see vad.hpp)
 */
#include "vad.hpp"

#include <cmath>
#include <fstream>
#include <iostream>

#include "config.hpp"

#ifdef HAS_SHERPA_ONNX
#include "sherpa-onnx/c-api/c-api.h"
#endif

namespace demascas {

SpeechDetector::~SpeechDetector() {
#ifdef HAS_SHERPA_ONNX
    if (handle_)
        SherpaOnnxDestroyVoiceActivityDetector(
            static_cast<const SherpaOnnxVoiceActivityDetector*>(handle_));
#endif
}

bool SpeechDetector::init(const std::string& model_path, int sample_rate,
                          float threshold, float min_speech_sec,
                          float min_silence_sec) {
    energy_threshold_ = config::VAD_ENERGY_THRESHOLD;
#ifdef HAS_SHERPA_ONNX
    if (!std::ifstream(model_path).good()) {
        std::cerr << "[!] Speech detector model missing (" << model_path
                  << ") — using loudness only.\n";
        return false;
    }
    SherpaOnnxVadModelConfig c{};
    c.silero_vad.model = model_path.c_str();
    c.silero_vad.threshold = threshold;
    c.silero_vad.min_silence_duration = min_silence_sec;
    c.silero_vad.min_speech_duration = min_speech_sec;
    c.silero_vad.window_size = 512;            // 32 ms at 16 kHz
    c.silero_vad.max_speech_duration = 30.0f;
    c.sample_rate = sample_rate;
    c.num_threads = 1;
    c.provider = "cpu";
    c.debug = 0;
    handle_ = SherpaOnnxCreateVoiceActivityDetector(&c, 30.0f);
    if (!handle_) {
        std::cerr << "[!] Speech detector failed to load — using loudness only.\n";
        return false;
    }
    return true;
#else
    (void)model_path; (void)sample_rate; (void)threshold;
    (void)min_speech_sec; (void)min_silence_sec;
    return false;
#endif
}

void SpeechDetector::accept(const float* samples, size_t n) {
    if (!samples || n == 0) return;
#ifdef HAS_SHERPA_ONNX
    if (handle_) {
        auto* v = static_cast<const SherpaOnnxVoiceActivityDetector*>(handle_);
        SherpaOnnxVoiceActivityDetectorAcceptWaveform(v, samples, static_cast<int32_t>(n));
        // We only use the live "speaking" flag; drop finished segments so
        // the internal buffer never grows.
        while (!SherpaOnnxVoiceActivityDetectorEmpty(v))
            SherpaOnnxVoiceActivityDetectorPop(v);
        return;
    }
#endif
    double s = 0;
    for (size_t i = 0; i < n; ++i) s += samples[i] * samples[i];
    fallback_energy_ = static_cast<float>(std::sqrt(s / n));
}

bool SpeechDetector::speaking() const {
#ifdef HAS_SHERPA_ONNX
    if (handle_)
        return SherpaOnnxVoiceActivityDetectorDetected(
                   static_cast<const SherpaOnnxVoiceActivityDetector*>(handle_)) != 0;
#endif
    return fallback_energy_ >= energy_threshold_;
}

void SpeechDetector::reset() {
#ifdef HAS_SHERPA_ONNX
    if (handle_) {
        SherpaOnnxVoiceActivityDetectorReset(
            static_cast<const SherpaOnnxVoiceActivityDetector*>(handle_));
        return;
    }
#endif
    fallback_energy_ = 0.0f;
}

}  // namespace demascas
