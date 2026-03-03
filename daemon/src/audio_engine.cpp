/**
 * DEMASCAS — daemon/src/audio_engine.cpp
 * PortAudio-based audio capture with energy-based VAD.
 */

#include "audio_engine.hpp"
#include "config.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <thread>

namespace demascas {

using clk = std::chrono::steady_clock;
namespace cfg = config;

// ── PortAudio callback ──────────────────────────────────────────────
int AudioEngine::pa_callback(const void* in, void* /*out*/,
                              unsigned long frames,
                              const PaStreamCallbackTimeInfo*,
                              PaStreamCallbackFlags, void* ud) {
    auto* self = static_cast<AudioEngine*>(ud);
    const auto* samples = static_cast<const float*>(in);
    {
        std::lock_guard<std::mutex> lk(self->mu_);
        self->ring_.insert(self->ring_.end(), samples, samples + frames);
    }
    return paContinue;
}

// ── Lifecycle ───────────────────────────────────────────────────────

AudioEngine::AudioEngine()  { Pa_Initialize(); }

AudioEngine::~AudioEngine() { stop(); Pa_Terminate(); }

bool AudioEngine::start() {
    if (active_) return true;

    PaError err = Pa_OpenDefaultStream(
        &stream_,
        1,                        // mono input
        0,                        // no output
        paFloat32,
        cfg::SAMPLE_RATE,
        cfg::FRAMES_PER_BUFFER,
        pa_callback,
        this);

    if (err != paNoError) {
        std::cerr << "[!] PortAudio open error: "
                  << Pa_GetErrorText(err) << "\n";
        return false;
    }

    err = Pa_StartStream(stream_);
    if (err != paNoError) {
        std::cerr << "[!] PortAudio start error: "
                  << Pa_GetErrorText(err) << "\n";
        Pa_CloseStream(stream_);
        stream_ = nullptr;
        return false;
    }

    active_ = true;
    std::cout << "[✓] Microphone opened (16 kHz mono, PortAudio)\n";
    return true;
}

void AudioEngine::stop() {
    if (!active_) return;
    Pa_StopStream(stream_);
    Pa_CloseStream(stream_);
    stream_  = nullptr;
    active_  = false;
}

// ── Helpers ─────────────────────────────────────────────────────────

std::vector<float> AudioEngine::drain() {
    std::lock_guard<std::mutex> lk(mu_);
    std::vector<float> out;
    out.swap(ring_);
    return out;
}

float AudioEngine::rms(const float* data, size_t n) {
    if (n == 0) return 0.0f;
    double sum = 0.0;
    for (size_t i = 0; i < n; ++i)
        sum += static_cast<double>(data[i]) * data[i];
    return static_cast<float>(std::sqrt(sum / static_cast<double>(n)));
}

// ── Public capture methods ──────────────────────────────────────────

std::vector<float> AudioEngine::capture_chunk(float seconds) {
    drain();  // flush stale samples
    auto deadline = clk::now()
        + std::chrono::milliseconds(static_cast<int>(seconds * 1000));

    while (clk::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(50));

    return drain();
}

std::vector<float> AudioEngine::capture_utterance(float timeout_sec,
                                                    float silence_sec) {
    drain();  // start clean

    auto deadline = clk::now()
        + std::chrono::milliseconds(static_cast<int>(timeout_sec * 1000));

    std::vector<float> recording;
    bool speech_started = false;
    auto last_speech     = clk::now();

    while (clk::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));

        auto buf = drain();
        if (buf.empty()) continue;

        float energy = rms(buf.data(), buf.size());

        if (energy >= cfg::VAD_ENERGY_THRESHOLD) {
            speech_started = true;
            last_speech    = clk::now();
        }

        if (speech_started) {
            recording.insert(recording.end(), buf.begin(), buf.end());

            float silence_elapsed = std::chrono::duration<float>(
                clk::now() - last_speech).count();
            if (silence_elapsed >= silence_sec)
                break;  // silence detected — utterance complete
        }
    }

    return recording;
}

}  // namespace demascas
