# DEMASCAS → Plug-and-Play AI Harness — Roadmap

**Goal:** ship DEMASCAS as a tool/service for any laptop (macOS, Windows, Linux) **without a bundled LLM**. The user plugs in a local open-source model that fits their machine, so it never lags, overheats or fails to run.

**Decisions (2026-11-09):**
- Platforms: macOS + Windows + Linux.
- LLM: detect an installed runtime; if none, recommend a model for the hardware and guide installation. DEMASCAS never owns the model lifecycle (except starting MLX, which has no background service, when the user opts in).
- Every change is latency-checked (before/after numbers) — see the latency table in the README.

---

## Phase 0 — Plug-in LLM layer ✅ (done)

| Piece | Where |
|---|---|
| Hardware profile (CPU, RAM, Apple/NVIDIA GPU, battery; psutil or OS-native) | `core/llm/hardware.py` |
| Runtime detection: Ollama, LM Studio, llama.cpp, MLX, Jan, custom (parallel, ~10 ms) | `core/llm/runtimes.py` |
| Curated model catalog with per-runtime ids + memory needs | `core/llm/catalog.py` |
| No-lag memory budget + latency cap, installed models first | `core/llm/recommend.py` |
| Real speed test (TTFT cold/warm, tokens/s) → fast / ok / slow | `core/llm/benchmark.py` |
| Keep model loaded during use; skip preload when RAM is short | `core/llm/governor.py` |
| `setup` / `doctor` / `detect` / `llm-check` CLI; config in `~/.demascas/config.json` | `core/llm/cli.py`, `settings.py` |
| Tool server `GET /llm`; daemon reads endpoint/model/context/threads at startup | `server/tool_server.py`, `daemon/src/*` |
| Context-window fitting for small runtimes (Ollama default 4096) | `LLMClient::fit_history_to_context` |

Measured: detection 6–12 ms; `/llm` 0.4–0.9 ms (once per daemon start); per-command tool list 2.6k → 0.9k chars; no regression on the pre-LLM path (~6.7 ms).
Not measured yet: real model speed (no runtime installed on the dev Mac) — `python main.py doctor --bench` does it on any laptop.

## Phase 1 — Cross-platform OS control (next)

Today everything that touches the desktop is AppleScript / macOS Accessibility. Introduce a platform layer and three backends:

```
platform/
  base.py        PlatformBackend interface
  macos.py       existing AppleScript / AX code moved behind the interface
  windows.py     UI Automation (pywinauto 'uia' or uiautomation), win32
  linux.py       AT-SPI (pyatspi) + xdotool/wmctrl on X11
  common.py      pynput (keys/mouse/typing), mss (screenshots), pyperclip,
                 send2trash, psutil (processes/system info)
```

Interface areas: apps (open/quit/activate/list), input (type, key combo, click, scroll, drag), accessibility (read tree, find/click element by name), clipboard, screenshot, files (open with default app, move to trash, reveal/selected file), notifications, system info.

Notes:
- **Shortcuts:** the C++ map uses macOS combos (`command+w`). Translate per OS (`command` → `ctrl` on Windows/Linux) with per-action overrides (screenshot: Win+Shift+S / PrtSc; lock: Win+L / Super+L; mission control has no equivalent).
- **Linux Wayland** blocks synthetic input and window inspection for normal apps; X11 works fully. Detect the session type and report limits clearly.
- **Safe shell tool:** per-OS allowlists (Windows has no `ps`/`df`); prefer psutil-based structured tools.
- Latency check: element search must stay single-pass (as on macOS today); measure each backend's tree read.

## Phase 2 — Cross-platform browser automation

Replace "Brave via AppleScript JS injection" with the **Chrome DevTools Protocol** (Chrome, Brave, Edge on all OSes), keeping the existing JS logic. Chrome 136+ ignores `--remote-debugging-port` for the default profile, so DEMASCAS uses its own browser profile (user signs in once). Gmail draft-only rule stays.

## Phase 3 — Portable daemon, launcher and background service

- C++: POSIX-only pieces (`fork`/`exec` of `say`/`afplay`, `unistd.h`, `getcwd`, `mkdir`, `chmod`) behind `#ifdef`s; play earcons through PortAudio from bundled WAVs (faster than spawning `afplay`); libcurl via vcpkg on Windows; whisper.cpp Metal on macOS, CPU/CUDA/Vulkan elsewhere; sherpa-onnx prebuilt per OS.
- Replace `run.sh` / `build.sh` with a Python launcher (`demascas start|stop|setup|doctor`) that works on all three OSes.
- Run at login as a user service: launchd agent (macOS), per-user Scheduled Task / Startup entry (Windows — not a system service, which can't use the mic/desktop), systemd `--user` unit (Linux).

## Phase 4 — Packaging & CI

GitHub Actions matrix (macos-latest, windows-latest, ubuntu-latest): build the daemon, run the Python test suite, produce per-OS artifacts/installers. Latency benchmarks run in CI on fixed stand-in runtimes so regressions are caught.

---

## Testing limits (be explicit)

- The dev machine is an 8 GB M1 Mac: macOS code is tested here; **Windows code cannot be run here**; Linux only via Docker (Docker Desktop must be running) or CI.
- Real model speed needs a downloaded model; stand-in servers that mimic each runtime's API are used for detection/setup/daemon tests.
