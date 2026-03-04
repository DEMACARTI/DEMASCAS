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

    TTS();
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
    void play_pcm(const void* audio_ptr);

    // ── Kokoro initialisation ──────────────────────────────────
    bool init_kokoro();
    bool open_output_stream(int sample_rate);

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
    std::atomic<bool>           playing_{false};
    std::atomic<bool>           stop_{false};
    std::thread                 worker_;
};

}  // namespace demascas
