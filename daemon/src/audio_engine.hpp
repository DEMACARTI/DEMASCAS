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
#include <chrono>
#include <functional>
#include <vector>

#include <portaudio.h>

namespace demascas {

class SpeechDetector;

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

    /// Wait until at least `seconds` of NEW audio has arrived since the last
    /// drain and return everything buffered.  Unlike capture_chunk() it never
    /// discards audio, so nothing is lost while whisper is busy (used by the
    /// sliding wake-word window).
    std::vector<float> collect(float seconds);

    /// Wait for speech onset, then record until silence.
    /// Returns an empty vector on timeout (no speech detected).
    std::vector<float> capture_utterance(float timeout_sec,
                                          float silence_sec = 2.0f,
                                          const std::vector<float>& already_spoken = {},
                                          const std::function<bool()>& abort_before_speech = nullptr,
                                          const std::function<void()>& on_speech_start = nullptr);

    /// Use a speech detector (Silero VAD) instead of loudness to decide when
    /// the user starts/stops talking — fans, typing and music no longer start
    /// or prolong a recording.
    void set_speech_detector(SpeechDetector* vad) { vad_ = vad; }

    /// drain() plus the capture time of the LAST returned sample
    /// (input latency already subtracted) — for echo cancellation.
    std::vector<float> drain_timed(std::chrono::steady_clock::time_point& last_sample_time);

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
    SpeechDetector* vad_ = nullptr;
    std::chrono::steady_clock::time_point last_cb_{};
    double             input_latency_ = 0.0;
};

}  // namespace demascas
