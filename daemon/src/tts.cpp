/**
 * DEMASCAS — daemon/src/tts.cpp
 * Streaming TTS with sentence queue, worker thread, and instant interrupt.
 *
 * Each sentence runs as a separate `say` subprocess whose PID is tracked.
 * interrupt() sends SIGTERM to the running process + drains the queue.
 * Kill latency: < 5 ms on macOS.
 */

#include "tts.hpp"
#include "config.hpp"

#include <csignal>
#include <cstdlib>
#include <iostream>
#include <regex>
#include <sys/wait.h>
#include <unistd.h>

namespace demascas {

namespace cfg = config;

// ── Constructor / Destructor ────────────────────────────────────────

TTS::TTS() : worker_(&TTS::worker_loop, this) {
    std::cout << "[✓] Streaming TTS ready  (voice=" << cfg::TTS_VOICE
              << ", rate=" << cfg::TTS_RATE
              << " wpm, interrupt=enabled)\n";
}

TTS::~TTS() { shutdown(); }

// ── Text sanitisation ───────────────────────────────────────────────

std::string TTS::sanitize(const std::string& text) {
    std::string s = text;

    // Strip markdown formatting
    s = std::regex_replace(s, std::regex(R"(\*+)"), "");
    // Strip inline code
    s = std::regex_replace(s, std::regex(R"(`[^`]*`)"), "");
    // Strip markdown links → keep label text
    s = std::regex_replace(s, std::regex(R"(\[([^\]]*)\]\([^\)]*\))"), "$1");
    // Strip raw URLs
    s = std::regex_replace(s, std::regex(R"(https?://\S+)"), "");

    // Remove characters dangerous in a shell double-quoted string
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (c == '"' || c == '\\' || c == '$' || c == '`') continue;
        out += c;
    }

    // Truncate to 500 chars
    if (out.size() > 500) out.resize(500);
    return out;
}

// ── Worker thread ───────────────────────────────────────────────────

void TTS::worker_loop() {
    while (!stop_) {
        std::string sentence;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_.wait_for(lk, std::chrono::milliseconds(100),
                         [this]{ return !queue_.empty() || stop_.load(); });
            if (stop_) break;
            if (queue_.empty()) {
                if (playing_) playing_ = false;
                continue;
            }
            sentence = std::move(queue_.front());
            queue_.pop();
        }

        playing_ = true;
        speak_one(sentence);

        // Check if queue is now empty
        std::lock_guard<std::mutex> lk(mu_);
        if (queue_.empty()) playing_ = false;
    }
}

void TTS::speak_one(const std::string& text) {
    if (text.empty() || stop_) return;
    std::string safe = sanitize(text);
    if (safe.empty()) return;

    // Fork + exec `say` — track the child PID for interrupt
    pid_t pid = fork();
    if (pid == 0) {
        // Child process
        std::string rate_str = std::to_string(cfg::TTS_RATE);
        execlp("say", "say",
               "-v", cfg::TTS_VOICE.c_str(),
               "-r", rate_str.c_str(),
               "--", safe.c_str(),
               nullptr);
        _exit(1);  // exec failed
    } else if (pid > 0) {
        current_pid_ = pid;
        int status = 0;
        waitpid(pid, &status, 0);
        current_pid_ = 0;
    }
}

// ── Public API ──────────────────────────────────────────────────────

void TTS::enqueue(const std::string& text) {
    if (text.empty()) return;
    {
        std::lock_guard<std::mutex> lk(mu_);
        queue_.push(text);
    }
    cv_.notify_one();
}

void TTS::speak(const std::string& text) {
    enqueue(text);
    wait_done();
}

void TTS::speak_async(const std::string& text) {
    enqueue(text);
}

void TTS::interrupt() {
    // Drain the queue
    {
        std::lock_guard<std::mutex> lk(mu_);
        std::queue<std::string> empty;
        queue_.swap(empty);
    }

    // Kill the running `say` process
    pid_t pid = current_pid_.exchange(0);
    if (pid > 0) {
        kill(pid, SIGTERM);
        // Reap the child to avoid zombies
        waitpid(pid, nullptr, WNOHANG);
    }

    playing_ = false;
}

bool TTS::is_playing() const {
    return playing_;
}

void TTS::wait_done() {
    while (playing_ && !stop_) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

// ── Sound effects ───────────────────────────────────────────────────

void TTS::play_sound(const std::string& name) {
    std::string path = "/System/Library/Sounds/" + name + ".aiff";
    // Fire-and-forget in a detached thread
    std::thread([path]() {
        pid_t pid = fork();
        if (pid == 0) {
            execlp("afplay", "afplay", path.c_str(), nullptr);
            _exit(1);
        } else if (pid > 0) {
            waitpid(pid, nullptr, 0);
        }
    }).detach();
}

// ── Canned lines ────────────────────────────────────────────────────

void TTS::greet()   { speak("At your service."); }
void TTS::goodbye() { speak("Standing by."); }

// ── Shutdown ────────────────────────────────────────────────────────

void TTS::shutdown() {
    stop_ = true;
    interrupt();
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
}

}  // namespace demascas
