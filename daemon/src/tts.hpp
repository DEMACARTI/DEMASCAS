/**
 * DEMASCAS — daemon/src/tts.hpp
 * The Mouth: macOS TTS with streaming sentence queue and instant interrupt.
 *
 * Architecture:
 *   • Sentence queue (thread-safe) with dedicated worker thread
 *   • Each sentence is a separate `say` subprocess
 *   • interrupt() kills the running process + clears the queue (< 5 ms)
 *   • Supports barge-in: if the user speaks while DEMASCAS is talking,
 *     the audio stops instantly and the new command is processed.
 */

#pragma once
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <queue>
#include <string>
#include <thread>

namespace demascas {

class TTS {
public:
    TTS();
    ~TTS();

    TTS(const TTS&)            = delete;
    TTS& operator=(const TTS&) = delete;

    /// Enqueue a sentence for speaking (thread-safe, non-blocking).
    /// Sentences are spoken sequentially by the worker thread.
    void enqueue(const std::string& text);

    /// Speak text synchronously (blocks until finished or interrupted).
    /// For backward compat — wraps enqueue() + wait_done().
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

    /// Clean shutdown — stop worker, kill any active speech.
    void shutdown();

private:
    /// Strip markdown, URLs, and shell-dangerous characters.
    static std::string sanitize(const std::string& text);

    /// Worker loop — pops sentences and speaks them.
    void worker_loop();

    /// Speak a single sentence (blocks). Tracks PID for interrupt.
    void speak_one(const std::string& text);

    std::queue<std::string>     queue_;
    mutable std::mutex          mu_;
    std::condition_variable     cv_;
    std::atomic<pid_t>          current_pid_{0};
    std::atomic<bool>           playing_{false};
    std::atomic<bool>           stop_{false};
    std::thread                 worker_;
};

}  // namespace demascas
