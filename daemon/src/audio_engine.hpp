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

    /// Print all available audio input devices to stdout.
    static void list_devices();

    /// Open an input device and start capturing.
    /// @param device_index  -1 = system default; otherwise a PortAudio device index.
    bool start(int device_index = -1);

    /// Stop capturing and close the stream.
    void stop();

    /// Check if the PortAudio stream is still active.
    /// Returns false after macOS sleep/wake kills the audio device.
    bool is_stream_healthy() const;

    /// Full reinit: tear down PortAudio, re-enumerate devices, re-open stream.
    /// Call this when is_stream_healthy() returns false (e.g. after sleep/wake).
    bool reinit_on_wake(int device_index = -1);

    /// Record a fixed-duration chunk (for periodic wake-word scans).
    std::vector<float> capture_chunk(float seconds);

    /// Wait for speech onset, then record until silence.
    /// Returns an empty vector on timeout (no speech detected).
    std::vector<float> capture_utterance(float timeout_sec,
                                          float silence_sec = 2.0f);

    /// RMS energy of a buffer segment.
    static float rms(const float* data, size_t n);

    /// Drain the ring buffer, returning all accumulated samples.
    /// Used by barge-in detection to poll mic data while TTS plays.
    std::vector<float> drain();

private:
    /// PortAudio callback — runs on a high-priority audio thread.
    static int pa_callback(const void* in, void* out,
                           unsigned long frames,
                           const PaStreamCallbackTimeInfo*,
                           PaStreamCallbackFlags, void* ud);

    PaStream*          stream_  = nullptr;
    std::vector<float> ring_;
    mutable std::mutex mu_;
    std::atomic<bool>  active_{false};
};

}  // namespace demascas
