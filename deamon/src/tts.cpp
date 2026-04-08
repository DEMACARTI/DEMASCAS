/**
 * DEMASCAS — daemon/src/tts.cpp
 * Dual-backend TTS: Kokoro (neural, via sherpa-onnx) with platform-specific fallback.
 *
 * Kokoro path:
 *   SherpaOnnxCreateOfflineTts() → SherpaOnnxOfflineTtsGenerate()
 *   → float32 PCM (24 kHz mono) → chunk to PortAudio blocking output.
 *   Interrupt: set stop_ flag → worker exits loop → < 5 ms kill.
 *
 * Fallback paths:
 *   macOS: fork+exec `say` → track child PID → SIGTERM for interrupt.
 *   Linux: fork+exec `espeak-ng` → track child PID → SIGTERM for interrupt.
 */

#include "tts.hpp"
#include "config.hpp"

#include <algorithm>         // std::min
#include <climits>          // PATH_MAX
#include <cmath>            // sinf
#include <csignal>
#include <cstdlib>
#include <cstring>          // memset
#include <iostream>
#include <sys/wait.h>
#include <unistd.h>

// Platform-specific headers
#ifdef __linux__
#include <fcntl.h>          // O_WRONLY
#include <linux/input.h>    // input_event (for dummy device)
#endif

#ifdef __APPLE__
#include <CoreAudio/CoreAudio.h>
#include <AudioToolbox/AudioToolbox.h>
#endif

#ifdef HAS_SHERPA_ONNX
#include "sherpa-onnx/c-api/c-api.h"
#endif

namespace demascas {

namespace cfg = config;

// ── Kokoro initialisation (sherpa-onnx) ─────────────────────────────

bool TTS::init_kokoro() {
#ifdef HAS_SHERPA_ONNX
    // Resolve absolute paths (daemon runs from daemon/ directory)
    char cwd[PATH_MAX];
    if (!getcwd(cwd, sizeof(cwd))) return false;
    std::string base = std::string(cwd) + "/" + cfg::KOKORO_MODEL_DIR;

    std::string model_path  = base + "/model.onnx";
    std::string voices_path = base + "/voices.bin";
    std::string tokens_path = base + "/tokens.txt";
    std::string data_dir    = base + "/espeak-ng-data";

    // Check files exist
    if (access(model_path.c_str(),  R_OK) != 0) {
        std::cout << "[*] Kokoro model not found at " << model_path << "\n";
        return false;
    }
    if (access(voices_path.c_str(), R_OK) != 0) {
        std::cout << "[*] Kokoro voices not found at " << voices_path << "\n";
        return false;
    }
    if (access(tokens_path.c_str(), R_OK) != 0) {
        std::cout << "[*] Kokoro tokens not found at " << tokens_path << "\n";
        return false;
    }

    // Build sherpa-onnx offline TTS config
    SherpaOnnxOfflineTtsConfig tts_cfg;
    memset(&tts_cfg, 0, sizeof(tts_cfg));

    tts_cfg.model.kokoro.model     = model_path.c_str();
    tts_cfg.model.kokoro.voices    = voices_path.c_str();
    tts_cfg.model.kokoro.tokens    = tokens_path.c_str();
    tts_cfg.model.kokoro.data_dir  = data_dir.c_str();
    tts_cfg.model.kokoro.length_scale = 1.0f / cfg::KOKORO_SPEED;

    tts_cfg.model.num_threads = 2;       // keep 2 threads — plenty for M1
    tts_cfg.model.provider    = "cpu";   // GPU stays free for whisper Metal
    tts_cfg.model.debug       = 0;
    tts_cfg.max_num_sentences = 1;       // generate one sentence at a time

    const SherpaOnnxOfflineTts* tts = SherpaOnnxCreateOfflineTts(&tts_cfg);
    if (!tts) {
        std::cerr << "[!] SherpaOnnxCreateOfflineTts() failed\n";
        return false;
    }

    tts_handle_ = const_cast<void*>(reinterpret_cast<const void*>(tts));

    int sr = SherpaOnnxOfflineTtsSampleRate(tts);
    std::cout << "[*] Kokoro model loaded (sample_rate=" << sr
              << " Hz, speakers=" << SherpaOnnxOfflineTtsNumSpeakers(tts)
              << ")\n";

    // Open PortAudio output stream for float32 playback
    if (!open_output_stream(sr)) {
        std::cerr << "[!] Cannot open audio output stream.\n";
        SherpaOnnxDestroyOfflineTts(tts);
        tts_handle_ = nullptr;
        return false;
    }

    return true;
#else
    std::cout << "[*] sherpa-onnx not compiled in — Kokoro unavailable\n";
    return false;
#endif
}

bool TTS::open_output_stream(int sample_rate) {
    if (out_stream_) return true;

    // TTS manages its own PA reference count
    Pa_Initialize();

    // Find and log the default output device
    PaDeviceIndex out_dev = Pa_GetDefaultOutputDevice();
    if (out_dev == paNoDevice) {
        std::cerr << "[!] PortAudio: no default output device found!\n";
        return false;
    }
    const PaDeviceInfo* info = Pa_GetDeviceInfo(out_dev);
    if (info) {
        std::cout << "[*] TTS output device: " << info->name
                  << " (idx=" << out_dev
                  << ", maxOut=" << info->maxOutputChannels
                  << ", defaultSR=" << info->defaultSampleRate << ")\n";
    }

    // Use explicit output parameters to target the correct device
    PaStreamParameters out_params;
    out_params.device = out_dev;
    out_params.channelCount = 1;            // mono
    out_params.sampleFormat = paFloat32;    // Kokoro outputs float32 [-1, 1]
    out_params.suggestedLatency = info ? info->defaultLowOutputLatency : 0.01;
    out_params.hostApiSpecificStreamInfo = nullptr;

    PaError err = Pa_OpenStream(
        &out_stream_,
        nullptr,       // no input
        &out_params,
        sample_rate,
        256,           // frames per buffer (low latency)
        paClipOff,     // no clipping
        nullptr,       // no callback — blocking Pa_WriteStream
        nullptr);

    if (err != paNoError) {
        std::cerr << "[!] PA output open: " << Pa_GetErrorText(err) << "\n";
        out_stream_ = nullptr;
        return false;
    }

    err = Pa_StartStream(out_stream_);
    if (err != paNoError) {
        std::cerr << "[!] PA output start: " << Pa_GetErrorText(err) << "\n";
        Pa_CloseStream(out_stream_);
        out_stream_ = nullptr;
        return false;
    }

    std::cout << "[✓] TTS PortAudio stream open (" << sample_rate << " Hz, float32, mono)\n";

    // Play a short test tone (440 Hz, 0.15s) to verify audio output works
    {
        constexpr int TEST_SAMPLES = 3600;  // 0.15s at 24kHz
        float test_buf[TEST_SAMPLES];
        for (int i = 0; i < TEST_SAMPLES; ++i)
            test_buf[i] = 0.2f * sinf(2.0f * 3.14159265f * 440.0f * i / sample_rate);
        PaError te = Pa_WriteStream(out_stream_, test_buf, TEST_SAMPLES);
        if (te == paNoError)
            std::cout << "[✓] TTS test tone played (440 Hz, 0.15 s)\n";
        else
            std::cerr << "[!] TTS test tone FAILED: " << Pa_GetErrorText(te) << "\n";
    }

    return true;
}

// ── Constructor / Destructor ────────────────────────────────────────

TTS::TTS() {
    // Try Kokoro first (if enabled and model files exist)
    if (cfg::USE_KOKORO && init_kokoro()) {
        backend_ = Backend::KOKORO;
    } else {
        backend_ = Backend::SAY;
    }

    // Start worker thread
    worker_ = std::thread(&TTS::worker_loop, this);

    if (backend_ == Backend::KOKORO) {
        std::cout << "[✓] TTS ready  (backend=Kokoro, model="
                  << cfg::KOKORO_MODEL_DIR << ", rate="
                  << cfg::KOKORO_SAMPLE_RATE << " Hz, CPU)\n";
    } else {
        std::cout << "[✓] TTS ready  (backend=macOS say, voice="
                  << cfg::TTS_VOICE << ", rate=" << cfg::TTS_RATE
                  << " wpm)\n";
    }
}

TTS::~TTS() { shutdown(); }

// ── Text sanitisation (zero-regex, single-pass) ────────────────────

std::string TTS::sanitize(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    int n = static_cast<int>(text.size());
    int i = 0;
    while (i < n) {
        char c = text[i];

        // Skip markdown bold/italic asterisks
        if (c == '*') { ++i; continue; }

        // Skip inline code backtick spans
        if (c == '`') {
            ++i;
            while (i < n && text[i] != '`') ++i;
            if (i < n) ++i; // skip closing backtick
            continue;
        }

        // Markdown link [label](url) → keep label only
        if (c == '[') {
            int j = i + 1;
            while (j < n && text[j] != ']') ++j;
            if (j < n && j + 1 < n && text[j + 1] == '(') {
                // Emit label text
                for (int k = i + 1; k < j; ++k) out += text[k];
                // Skip past closing paren
                int p = j + 2;
                while (p < n && text[p] != ')') ++p;
                i = (p < n) ? p + 1 : p;
                continue;
            }
            // Not a valid link — emit the bracket
            out += c; ++i; continue;
        }

        // Strip raw URLs (http:// or https://)
        if (c == 'h' && i + 7 < n &&
            (text.compare(i, 7, "http://") == 0 ||
             text.compare(i, 8, "https://") == 0)) {
            while (i < n && text[i] != ' ' && text[i] != '\n') ++i;
            continue;
        }

        // Skip embedded JSON objects (tool call leaks)
        if (c == '{') {
            int depth = 1;
            ++i;
            while (i < n && depth > 0) {
                if (text[i] == '{') ++depth;
                else if (text[i] == '}') --depth;
                ++i;
            }
            continue;
        }

        // Skip shell-dangerous chars
        if (c == '"' || c == '\\' || c == '$') { ++i; continue; }

        out += c;
        ++i;
    }

    if (out.size() > 500) out.resize(500);
    return out;
}

// ── Worker thread (double-buffered: pre-synthesize next while playing) ──

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

        // ── Double-buffer: synthesize THIS sentence's audio while
        //    we peek at the NEXT sentence so we can pre-synthesize it
        //    during playback.  This eliminates the gap between sentences.
#ifdef HAS_SHERPA_ONNX
        if (backend_ == Backend::KOKORO && tts_handle_ && !stop_) {
            std::string safe = sanitize(sentence);
            if (!safe.empty()) {
                auto* tts = reinterpret_cast<const SherpaOnnxOfflineTts*>(tts_handle_);

                // Synthesize current sentence
                const SherpaOnnxGeneratedAudio* audio =
                    SherpaOnnxOfflineTtsGenerate(tts, safe.c_str(),
                                                 cfg::KOKORO_SPEAKER_ID,
                                                 cfg::KOKORO_SPEED);

                if (audio && audio->n > 0) {
                    // Peek at next sentence — synthesize it in a background thread
                    //   while we play the current one.
                    std::string next_safe;
                    const SherpaOnnxGeneratedAudio* next_audio = nullptr;
                    std::thread prefetch;
                    {
                        std::lock_guard<std::mutex> lk(mu_);
                        if (!queue_.empty()) {
                            next_safe = sanitize(queue_.front());
                        }
                    }
                    if (!next_safe.empty() && !stop_) {
                        prefetch = std::thread([&]() {
                            next_audio = SherpaOnnxOfflineTtsGenerate(
                                tts, next_safe.c_str(),
                                cfg::KOKORO_SPEAKER_ID, cfg::KOKORO_SPEED);
                        });
                    }

                    // Play current sentence via PortAudio (blocks)
                    play_pcm(audio);
                    SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio);

                    // If we pre-synthesized the next sentence, play it immediately
                    if (prefetch.joinable()) {
                        prefetch.join();
                        if (next_audio && next_audio->n > 0 && !stop_) {
                            // Pop the sentence we already synthesized
                            {
                                std::lock_guard<std::mutex> lk(mu_);
                                if (!queue_.empty()) {
                                    std::string front_safe = sanitize(queue_.front());
                                    if (front_safe == next_safe) {
                                        queue_.pop();
                                    }
                                }
                            }
                            play_pcm(next_audio);
                        }
                        if (next_audio)
                            SherpaOnnxDestroyOfflineTtsGeneratedAudio(next_audio);
                    }
                } else {
                    if (audio) SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio);
                    speak_one_say(sentence);  // fallback
                }
            }
        } else
#endif
        {
            speak_one(sentence);
        }

        // Check if queue is now empty
        std::lock_guard<std::mutex> lk(mu_);
        if (queue_.empty()) playing_ = false;
    }
}

// ── Backend dispatcher ──────────────────────────────────────────────

void TTS::speak_one(const std::string& text) {
    if (backend_ == Backend::KOKORO)
        speak_one_kokoro(text);
    else
        speak_one_say(text);
}

// ── Kokoro backend — in-process neural TTS via sherpa-onnx ──────────

// Shared PCM playback — used by both the double-buffer worker loop
// and the legacy speak_one_kokoro fallback path.
void TTS::play_pcm(const void* audio_ptr) {
#ifdef HAS_SHERPA_ONNX
    auto* audio = static_cast<const SherpaOnnxGeneratedAudio*>(audio_ptr);
    if (!audio || audio->n <= 0 || !out_stream_) return;

    // Ensure stream is active
    if (!Pa_IsStreamActive(out_stream_))
        Pa_StartStream(out_stream_);

    constexpr int32_t CHUNK = 2400;  // ~100 ms at 24 kHz
    int32_t offset = 0;
    while (offset < audio->n && !stop_) {
        int32_t count = std::min(CHUNK, audio->n - offset);
        PaError err = Pa_WriteStream(out_stream_, audio->samples + offset,
                                      static_cast<unsigned long>(count));
        if (err != paNoError && err != paOutputUnderflowed) {
            std::cerr << "[!] PA write error: " << Pa_GetErrorText(err) << "\n";
            break;
        }
        offset += count;
    }
#endif
}

void TTS::speak_one_kokoro(const std::string& text) {
#ifdef HAS_SHERPA_ONNX
    if (text.empty() || stop_ || !tts_handle_) return;
    std::string safe = sanitize(text);
    if (safe.empty()) return;

    // Ensure output stream is running (may have been aborted by interrupt)
    if (out_stream_ && !Pa_IsStreamActive(out_stream_)) {
        Pa_StartStream(out_stream_);
    }

    auto* tts = reinterpret_cast<const SherpaOnnxOfflineTts*>(tts_handle_);

    // Generate float32 PCM audio
    const SherpaOnnxGeneratedAudio* audio =
        SherpaOnnxOfflineTtsGenerate(tts, safe.c_str(),
                                     cfg::KOKORO_SPEAKER_ID,
                                     cfg::KOKORO_SPEED);
    if (!audio || audio->n <= 0) {
        if (audio) SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio);
        // Fall back to macOS say
        std::cerr << "[!] Kokoro generated 0 samples — falling back to say\n";
        speak_one_say(text);
        return;
    }

    std::cout << "[*] Kokoro generated " << audio->n << " samples ("
              << (audio->n / (float)audio->sample_rate) << " s)\n";

    // Stream float32 samples to PortAudio in ~100 ms chunks (2400 samples at 24 kHz)
    constexpr int32_t CHUNK_SAMPLES = 2400;
    int32_t offset = 0;

    // Ensure output stream is running
    if (out_stream_ && !Pa_IsStreamActive(out_stream_)) {
        std::cout << "[*] Restarting PA output stream...\n";
        Pa_StartStream(out_stream_);
    }

    while (offset < audio->n) {
        if (stop_) break;

        int32_t remaining = audio->n - offset;
        int32_t count = (remaining < CHUNK_SAMPLES) ? remaining : CHUNK_SAMPLES;

        if (out_stream_) {
            PaError err = Pa_WriteStream(out_stream_, audio->samples + offset,
                                         static_cast<unsigned long>(count));
            if (err != paNoError && err != paOutputUnderflowed) {
                std::cerr << "[!] PA write error: " << Pa_GetErrorText(err) << "\n";
                break;
            }
        } else {
            std::cerr << "[!] out_stream_ is null during playback!\n";
            break;
        }

        offset += count;
    }

    SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio);
#else
    // Shouldn't reach here, but fallback just in case
    speak_one_say(text);
#endif
}

// ── Platform-specific fallback backends ─────────────────────────────

// macOS `say` backend
#ifdef __APPLE__
void TTS::speak_one_say(const std::string& text) {
    if (text.empty() || stop_) return;
    std::string safe = sanitize(text);
    if (safe.empty()) return;

    pid_t pid = fork();
    if (pid == 0) {
        std::string rate_str = std::to_string(cfg::TTS_RATE);
        execlp("say", "say",
               "-v", cfg::TTS_VOICE.c_str(),
               "-r", rate_str.c_str(),
               "--", safe.c_str(),
               nullptr);
        _exit(1);
    } else if (pid > 0) {
        current_pid_ = pid;
        int status = 0;
        waitpid(pid, &status, 0);
        current_pid_ = 0;
    }
}
#endif

// Linux `espeak-ng` backend
#ifdef __linux__
void TTS::speak_one_say(const std::string& text) {
    if (text.empty() || stop_) return;
    std::string safe = sanitize(text);
    if (safe.empty()) return;

    pid_t pid = fork();
    if (pid == 0) {
        std::string rate_str = std::to_string(cfg::TTS_RATE);
        // espeak-ng rate: -10 to +10 (words per second adjustment)
        // Convert TTS_RATE (wpm) to espeak rate: rate = (wpm - 160) / 10
        int espeak_rate = (cfg::TTS_RATE - 160) / 10;
        execlp("espeak-ng", "espeak-ng",
               "-v", cfg::TTS_VOICE.c_str(),
               "-s", rate_str.c_str(),
               "-g", "5",
               "-w", "/tmp/demascas_tts.wav",
               "--", safe.c_str(),
               nullptr);
        _exit(1);
    } else if (pid > 0) {
        current_pid_ = pid;
        int status = 0;
        waitpid(pid, &status, 0);
        current_pid_ = 0;
    }
}
#endif

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
    // 1. Drain the sentence queue
    {
        std::lock_guard<std::mutex> lk(mu_);
        std::queue<std::string> empty;
        queue_.swap(empty);
    }

    // 2. Kill the running subprocess (macOS say fallback only)
    pid_t pid = current_pid_.exchange(0);
    if (pid > 0) {
        kill(pid, SIGTERM);
        waitpid(pid, nullptr, WNOHANG);
    }

    // 3. Abort PortAudio output to unblock Pa_WriteStream immediately
    //    (will be restarted before the next sentence)
    if (out_stream_) {
        Pa_AbortStream(out_stream_);
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
#ifdef __APPLE__
    // macOS: use built-in sounds with afplay
    std::string path = "/System/Library/Sounds/" + name + ".aiff";
    std::thread([path]() {
        pid_t pid = fork();
        if (pid == 0) {
            execlp("afplay", "afplay", path.c_str(), nullptr);
            _exit(1);
        } else if (pid > 0) {
            waitpid(pid, nullptr, 0);
        }
    }).detach();
#elif defined(__linux__)
    // Linux: use aplay for system sounds (optional - if no sound file, do nothing)
    // Try common Linux sound locations
    static const char* sound_locations[] = {
        "/usr/share/sounds/gnome/default/audio/window_focus.ogg",
        "/usr/share/sounds/ubuntu/notifications/glass.ogg",
        "/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga",
    };
    std::thread([name]() {
        // Try to play a simple beep if aplay is available
        pid_t pid = fork();
        if (pid == 0) {
            execlp("aplay", "aplay", "-q", "/dev/zero", "-d", "0.1", nullptr);
            _exit(1);
        } else if (pid > 0) {
            waitpid(pid, nullptr, 0);
        }
    }).detach();
#endif
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

    // Close PortAudio output
    if (out_stream_) {
        Pa_StopStream(out_stream_);
        Pa_CloseStream(out_stream_);
        out_stream_ = nullptr;
        Pa_Terminate();
    }

    // Destroy sherpa-onnx TTS engine
#ifdef HAS_SHERPA_ONNX
    if (tts_handle_) {
        SherpaOnnxDestroyOfflineTts(
            reinterpret_cast<const SherpaOnnxOfflineTts*>(tts_handle_));
        tts_handle_ = nullptr;
    }
#endif
}

}  // namespace demascas
