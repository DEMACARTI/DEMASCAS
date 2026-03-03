/**
 * DEMASCAS — daemon/src/audio_engine.hpp
 * Real-time audio capture via PortAudio.
 *
 * Replaces the Python SpeechRecognition/PyAudio stack with a
 * direct C callback into a ring buffer — no GIL, no garbage-
 * collector pauses, deterministic memory.
 */

#pragma once

#include <atomic>
#include <mutex>
#include <vector>

#include <portaudio.h>

namespace demascas {

class AudioEngine {
public:
    AudioEngine();
    ~AudioEngine();

    // Non-copyable — owns a PortAudio stream handle.
    AudioEngine(const AudioEngine&)            = delete;
    AudioEngine& operator=(const AudioEngine&) = delete;

    /// Open the default input device and start capturing.
    bool start();

    /// Stop capturing and close the stream.
    void stop();

    /// Record a fixed-duration chunk (for periodic wake-word scans).
    std::vector<float> capture_chunk(float seconds);

    /// Wait for speech onset, then record until silence.
    /// Returns an empty vector on timeout (no speech detected).
    std::vector<float> capture_utterance(float timeout_sec,
                                          float silence_sec = 2.0f);

    /// RMS energy of a buffer segment.
    static float rms(const float* data, size_t n);

private:
    /// PortAudio callback — runs on a high-priority audio thread.
    /// NOTE: Uses a mutex (acceptable for 16 kHz voice on Apple
    /// Silicon; swap for a lock-free ring if hard-RT is needed).
    static int pa_callback(const void* in, void* out,
                           unsigned long frames,
                           const PaStreamCallbackTimeInfo*,
                           PaStreamCallbackFlags, void* ud);

    /// Drain the ring buffer, returning all accumulated samples.
    std::vector<float> drain();

    PaStream*          stream_  = nullptr;
    std::vector<float> ring_;
    mutable std::mutex mu_;
    std::atomic<bool>  active_{false};
};

}  // namespace demascas
