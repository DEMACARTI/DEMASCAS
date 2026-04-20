cmake_minimum_required(VERSION 3.20)
project(demascas_daemon LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ── System libraries ─────────────────────────────────────────────────

# libcurl (ships with macOS)
find_package(CURL REQUIRED)

# PortAudio (brew install portaudio)
find_package(PkgConfig REQUIRED)
pkg_check_modules(PORTAUDIO REQUIRED IMPORTED_TARGET portaudio-2.0)

# ── External dependencies (fetched at configure time) ────────────────

include(FetchContent)

# whisper.cpp — bare-metal C/C++ Whisper inference
# Exploits Apple Silicon Accelerate & ANE for maximum throughput.
FetchContent_Declare(
    whisper
    GIT_REPOSITORY https://github.com/ggerganov/whisper.cpp
    GIT_TAG        v1.7.3
    GIT_SHALLOW    TRUE
)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER   OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(whisper)

# nlohmann/json — header-only JSON (used for Ollama API payloads)
FetchContent_Declare(
    json
    GIT_REPOSITORY https://github.com/nlohmann/json
    GIT_TAG        v3.11.3
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(json)

# ── Build the daemon executable ──────────────────────────────────────

add_executable(demascas_daemon
    src/main.cpp
    src/audio_engine.cpp
    src/whisper_engine.cpp
    src/llm_client.cpp
    src/tool_bridge.cpp
    src/tts.cpp
)

target_include_directories(demascas_daemon PRIVATE
    src
    ${whisper_SOURCE_DIR}/include          # whisper.h
)

target_link_libraries(demascas_daemon PRIVATE
    whisper
    CURL::libcurl
    PkgConfig::PORTAUDIO
    nlohmann_json::nlohmann_json
)

# ── sherpa-onnx  (pre-built shared library — Kokoro neural TTS) ──────

set(SHERPA_ONNX_DIR "${CMAKE_SOURCE_DIR}/models/sherpa-onnx")
if(EXISTS "${SHERPA_ONNX_DIR}/lib")
    message(STATUS "sherpa-onnx found at ${SHERPA_ONNX_DIR}")
    target_include_directories(demascas_daemon PRIVATE "${SHERPA_ONNX_DIR}/include")
    target_link_directories(demascas_daemon PRIVATE "${SHERPA_ONNX_DIR}/lib")
    target_link_libraries(demascas_daemon PRIVATE sherpa-onnx-c-api)
    target_compile_definitions(demascas_daemon PRIVATE HAS_SHERPA_ONNX=1)

    # Embed rpath so the binary finds .dylibs without DYLD_LIBRARY_PATH
    set_target_properties(demascas_daemon PROPERTIES
        BUILD_RPATH   "${SHERPA_ONNX_DIR}/lib"
        INSTALL_RPATH "${SHERPA_ONNX_DIR}/lib"
    )
else()
    message(WARNING "sherpa-onnx NOT found — Kokoro TTS disabled, using macOS say")
endif()

# ── whisper.cpp library paths for RPATH ──────────────────────────────
set(WHISPER_LIB_DIR "${whisper_BINARY_DIR}/src")
set(GGML_LIB_DIR "${whisper_BINARY_DIR}/ggml/src")

# ── Platform-specific libraries ──────────────────────────────────────

if(APPLE)
    # macOS: Apple Silicon optimizations
    target_link_libraries(demascas_daemon PRIVATE
        "-framework Accelerate"            # BLAS for whisper.cpp
        "-framework CoreAudio"
        "-framework AudioToolbox"
    )
    # Ensure we build native ARM binary
    if(NOT CMAKE_OSX_ARCHITECTURES)
        set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "" FORCE)
    endif()
elseif(UNIX AND NOT APPLE)
    # Linux: Link against required system libraries
    find_package(Threads REQUIRED)
    target_link_libraries(demascas_daemon PRIVATE
        Threads::Threads
        rt  # Real-time extensions
    )

    # Linux x86_64 architecture
    if(NOT CMAKE_HOST_SYSTEM_PROCESSOR STREQUAL "aarch64")
        set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -march=x86-64")
    endif()

    # Linux: Set RPATH to find whisper and ggml libraries at runtime
    set_target_properties(demascas_daemon PROPERTIES
        BUILD_RPATH   "${WHISPER_LIB_DIR};${GGML_LIB_DIR};${SHERPA_ONNX_DIR}/lib"
        INSTALL_RPATH "${WHISPER_LIB_DIR};${GGML_LIB_DIR};${SHERPA_ONNX_DIR}/lib"
        BUILD_WITH_INSTALL_RPATH TRUE
    )
endif()

# ── Release build flags ──────────────────────────────────────────────

if(CMAKE_BUILD_TYPE STREQUAL "Release")
    target_compile_options(demascas_daemon PRIVATE -O3 -DNDEBUG)
endif()
