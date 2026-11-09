/**
 * DEMASCAS — daemon/src/tts.hpp
 * The Mouth: dual-backend TTS with streaming sentence queue.
 *
 * Architecture:
 *   • Primary backend: Kokoro TTS via sherpa-onnx (native arm64)
 *     – In-process neural inference (82 M params, StyleTTS 2)
 *     – Float32 PCM (24 kHz mono) streamed to PortAudio output
 *     – CPU-only ONNX Runtime — GPU stays free for whisper.cpp Metal
 *
 *   • Fallback backend: macOS `say` (when sherpa-onnx not compiled in
 *     or model files absent)
 *
 *   • Sentence queue (thread-safe) with dedicated worker thread
 *   • interrupt() aborts playback + clears queue (< 5 ms)
 */

#pragma once
#include <atomic>
#include <cstdint>
#include <deque>
#include <unordered_map>
#include <vector>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <string>
#include <thread>

#include <portaudio.h>

namespace demascas {

class TTS {
public:
    /// Which synthesis engine is active.
    enum class Backend { SAY, KOKORO };

    /// threads: Kokoro synthesis threads (from GET /llm; 0 = config default).
    explicit TTS(int threads = 0);
    ~TTS();

    TTS(const TTS&)            = delete;
    TTS& operator=(const TTS&) = delete;

    /// Enqueue a sentence for speaking (thread-safe, non-blocking).
    void enqueue(const std::string& text);

    /// Speak text synchronously (blocks until finished or interrupted).
    void speak(const std::string& text);

    /// Speak text asynchronously (fire-and-forget).
    void speak_async(const std::string& text);

    /// Kill current speech and discard all pending sentences.
    /// Target latency: < 5 ms.
    void interrupt();

    /// Close the output stream and drop TTS's PortAudio reference so a full
    /// PortAudio re-initialisation (after macOS sleep / device change) can
    /// actually happen.  Call only while nothing is playing.
    void release_audio();

    /// Re-open the output stream after release_audio().
    void reacquire_audio();

    /// True if audio is currently being spoken or queued.
    bool is_playing() const;

    /// Block until all queued sentences are spoken (or interrupted).
    void wait_done();

    /// Play a macOS system sound asynchronously.
    static void play_sound(const std::string& name);

    /// JARVIS-style greeting ("At your service.").
    void greet();

    /// JARVIS-style farewell ("Standing by.").
    void goodbye();

    /// Clean shutdown — stop worker, destroy TTS engine.
    void shutdown();

    /// Which backend is active?
    Backend backend() const { return backend_; }

private:
    // ── Text processing ────────────────────────────────────────
    static std::string sanitize(const std::string& text);

    // ── Worker ─────────────────────────────────────────────────
    void worker_loop();

    // ── Backend dispatchers ────────────────────────────────────
    void speak_one(const std::string& text);
    void speak_one_kokoro(const std::string& text);
    void speak_one_say(const std::string& text);
    void play_pcm(const void* audio_ptr, uint64_t gen);
    void play_samples(const float* samples, int32_t n, uint64_t gen);

    /// Write frames, but never block forever: if the output device doesn't
    /// accept audio in time (stalled/vanished device), return false.
    /// Caller must hold pa_mu_.  gen=0 → no interrupt check.
    bool write_frames(const float* data, int32_t n, uint64_t gen);

    /// Split text into sentences so long answers start playing after the
    /// first sentence is synthesized instead of after the whole answer.
    static std::vector<std::string> split_sentences(const std::string& text);

    /// Worker-thread only: synthesize one queued fixed phrase into the cache.
    void warm_one_cached_phrase();

    // ── Kokoro initialisation ──────────────────────────────────
    bool init_kokoro();
    bool open_output_stream(int sample_rate, bool test_tone = true);

    // ── State ──────────────────────────────────────────────────
    Backend                     backend_     = Backend::SAY;

    // sherpa-onnx TTS handle (opaque — cast in .cpp with #ifdef)
    void*                       tts_handle_  = nullptr;

    // PortAudio output stream (float32 PCM playback)
    PaStream*                   out_stream_  = nullptr;

    // Sentence queue
    std::queue<std::string>     queue_;
    mutable std::mutex          mu_;
    std::condition_variable     cv_;

    // Process tracking (for macOS `say` fallback only)
    std::atomic<pid_t>          current_pid_{0};
    std::atomic<uint64_t>       gen_{0};        // bumped by interrupt(); stale audio is dropped
    std::mutex                  pa_mu_;         // guards out_stream_ open/close vs. writes
    int                         sample_rate_ = 0;
    int                         threads_     = 4;
    // Pre-synthesized PCM for fixed phrases (worker thread only).
    std::unordered_map<std::string, std::vector<float>> phrase_pcm_;
    std::deque<std::string>     warm_queue_;
    bool                        pa_held_     = false;  // TTS holds a Pa_Initialize reference
    std::atomic<bool>           playing_{false};
    std::atomic<bool>           stop_{false};
    std::thread                 worker_;
};

}  // namespace demascas
