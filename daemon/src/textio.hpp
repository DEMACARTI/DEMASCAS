/**
 * DEMASCAS — daemon/src/textio.hpp
 * Text mode (DEMASCAS_TEXT_MODE=1) — for the JARVIS exam only.
 *
 * Typed lines on stdin replace the microphone + recogniser; everything
 * DEMASCAS would say is printed instead of played.  No audio device is
 * opened and no speech models are loaded.  The daemon logic (routing,
 * tool loop, confirmations, background tasks) runs unchanged.
 *
 * Protocol (one line each):
 *   in   /wake            the wake word
 *   in   /reset           forget the conversation, cancel work, sleep
 *   in   ~0.4|text        a request heard with recogniser confidence 0.4
 *   in   text             a request heard clearly
 *   out  @@READY <state>  waiting for the next line
 *   out  @@SAY <text>     DEMASCAS speaks
 *   out  @@WAKE / @@SLEEP / @@RESET
 */
#pragma once

#include <atomic>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <string>

namespace demascas::textio {

inline bool enabled() {
    static const bool on = [] {
        const char* v = std::getenv("DEMASCAS_TEXT_MODE");
        return v && *v && std::string(v) != "0";
    }();
    return on;
}

struct Shared {
    std::mutex mu;
    std::string last;
    float conf = 1.0f, unsure = 0.0f;
    std::atomic<bool> eof{false}, reset{false};
};
inline Shared& st() { static Shared s; return s; }

/// One whole line per write, so log output from other threads can't split it.
inline void emit(const char* tag, const std::string& text = "") {
    std::string line = "\n@@";
    line += tag;
    if (!text.empty()) {
        line += ' ';
        for (char c : text) line += (c == '\n' || c == '\r') ? ' ' : c;
    }
    line += '\n';
    std::cout << line << std::flush;
}

/// Block for the next harness line.  false = input closed (exam over).
inline bool read_line(const char* where, std::string& out) {
    emit("READY", where);
    std::string line;
    if (!std::getline(std::cin, line)) { st().eof = true; out.clear(); return false; }
    while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
    float conf = 1.0f;
    if (line.size() > 2 && line[0] == '~') {
        auto bar = line.find('|');
        if (bar != std::string::npos) {
            conf = std::strtof(line.c_str() + 1, nullptr);
            line = line.substr(bar + 1);
        }
    }
    if (line == "/reset") { st().reset = true; line.clear(); }
    {
        std::lock_guard<std::mutex> lk(st().mu);
        st().last = line;
        st().conf = conf;
        st().unsure = conf < 0.5f ? 0.4f : 0.0f;
    }
    out = line;
    return true;
}

inline std::string last() { std::lock_guard<std::mutex> lk(st().mu); return st().last; }
inline float conf()      { std::lock_guard<std::mutex> lk(st().mu); return st().conf; }
inline float unsure()    { std::lock_guard<std::mutex> lk(st().mu); return st().unsure; }
inline bool take_reset() { return st().reset.exchange(false); }
inline bool eof()        { return st().eof.load(); }

}  // namespace demascas::textio
