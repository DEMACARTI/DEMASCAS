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

void AudioEngine::list_devices() {
    int count = Pa_GetDeviceCount();
    if (count <= 0) {
        std::cout << "[*] No audio devices found.\n";
        return;
    }
    int def_in = Pa_GetDefaultInputDevice();
    std::cout << "\n── Audio Input Devices ─────────────────────────\n";
    for (int i = 0; i < count; ++i) {
        const PaDeviceInfo* info = Pa_GetDeviceInfo(i);
        if (info && info->maxInputChannels > 0) {
            const char* marker = (i == def_in) ? "  ← DEFAULT" : "";
            std::cout << "  [" << i << "] " << info->name << marker << "\n";
        }
    }
    std::cout << "────────────────────────────────────────────────\n\n";
}

bool AudioEngine::start(int device_index) {
    if (active_) return true;

    PaError err;

    if (device_index < 0) {
        // Use PortAudio default device (original behaviour)
        err = Pa_OpenDefaultStream(
            &stream_,
            1,                        // mono input
            0,                        // no output
            paFloat32,
            cfg::SAMPLE_RATE,
            cfg::FRAMES_PER_BUFFER,
            pa_callback,
            this);
    } else {
        // Open a specific device by index
        const PaDeviceInfo* info = Pa_GetDeviceInfo(device_index);
        if (!info || info->maxInputChannels < 1) {
            std::cerr << "[!] Device " << device_index
                      << " has no input channels — falling back to default.\n";
            return start(-1);
        }
        PaStreamParameters params{};
        params.device                    = device_index;
        params.channelCount              = 1;
        params.sampleFormat              = paFloat32;
        params.suggestedLatency          = info->defaultLowInputLatency;
        params.hostApiSpecificStreamInfo = nullptr;

        err = Pa_OpenStream(
            &stream_,
            &params,               // input
            nullptr,               // no output
            cfg::SAMPLE_RATE,
            cfg::FRAMES_PER_BUFFER,
            paNoFlag,
            pa_callback,
            this);
    }

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
    std::cout << "[✓] Microphone opened (16 kHz mono, device="
              << (device_index < 0 ? "default" : std::to_string(device_index))
              << ")\n";
    return true;
}

void AudioEngine::stop() {
    if (!active_) return;
    Pa_StopStream(stream_);
    Pa_CloseStream(stream_);
    stream_  = nullptr;
    active_  = false;
}

bool AudioEngine::is_stream_healthy() const {
    if (!active_ || stream_ == nullptr) return false;
    PaError err = Pa_IsStreamActive(stream_);
    return err == 1;  // 1 = active, 0 = stopped, <0 = error
}

bool AudioEngine::reinit_on_wake(int device_index) {
    std::cout << "[AUDIO] Reinitializing after sleep/wake...\n";

    // Tear down everything
    if (stream_) {
        Pa_StopStream(stream_);
        Pa_CloseStream(stream_);
        stream_ = nullptr;
    }
    active_ = false;
    Pa_Terminate();

    // Give macOS a moment to re-enumerate audio devices
    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    // Re-initialize PortAudio from scratch
    PaError err = Pa_Initialize();
    if (err != paNoError) {
        std::cerr << "[!] PortAudio re-init failed: "
                  << Pa_GetErrorText(err) << "\n";
        return false;
    }

    // Clear the ring buffer
    {
        std::lock_guard<std::mutex> lk(mu_);
        ring_.clear();
    }

    // Re-open the stream
    bool ok = start(device_index);
    if (ok) {
        std::cout << "[AUDIO] Successfully reinitialized after wake.\n";
    } else {
        std::cerr << "[!] Failed to reopen audio stream after wake.\n";
    }
    return ok;
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
