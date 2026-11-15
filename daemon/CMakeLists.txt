cmake_minimum_required(VERSION 3.20)

# Must be set BEFORE project() to take effect (native Apple Silicon build).
if(APPLE AND NOT CMAKE_OSX_ARCHITECTURES)
    set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "Target architecture" FORCE)
endif()

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

# Reduce warning noise from Apple SDK headers pulled by whisper.cpp's
# Objective-C Metal source. Keep project warnings for our own daemon target.
if(APPLE AND CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    set(_DEMASCAS_THIRDPARTY_WARNING_SUPPRESS
        -Wno-pedantic
        -Wno-invalid-utf8
        -Wno-variadic-macro-arguments-omitted
        -Wno-extra-semi
    )

    foreach(_tgt ggml-metal ggml whisper)
        if(TARGET ${_tgt})
            target_compile_options(${_tgt} PRIVATE ${_DEMASCAS_THIRDPARTY_WARNING_SUPPRESS})
        endif()
    endforeach()

    if(TARGET ggml-metal)
        target_compile_options(ggml-metal PRIVATE
            -Wno-missing-method-return-type
            -Wno-deprecated-declarations
        )
    endif()
endif()

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
    src/vad.cpp
    src/aec.cpp
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

# ── Apple Silicon optimisations ──────────────────────────────────────

if(APPLE)
    target_link_libraries(demascas_daemon PRIVATE
        "-framework Accelerate"            # BLAS for whisper.cpp
        "-framework CoreAudio"
        "-framework AudioToolbox"
    )
endif()

# ── Release build flags ──────────────────────────────────────────────

if(CMAKE_BUILD_TYPE STREQUAL "Release")
    target_compile_options(demascas_daemon PRIVATE -O3 -DNDEBUG)
endif()

# ── speexdsp — acoustic echo cancellation (barge-in at a normal voice) ──
# Use a system copy if pkg-config finds one; otherwise fetch and compile its
# few C files so DEMASCAS needs no extra install (Mac / Linux / Windows).
find_package(PkgConfig QUIET)
if(PKG_CONFIG_FOUND AND NOT DEMASCAS_VENDOR_SPEEXDSP)
    pkg_check_modules(SPEEXDSP QUIET speexdsp)
endif()
if(SPEEXDSP_FOUND)
    message(STATUS "speexdsp (system) ${SPEEXDSP_VERSION}")
    target_include_directories(demascas_daemon PRIVATE ${SPEEXDSP_INCLUDE_DIRS})
    target_link_directories(demascas_daemon PRIVATE ${SPEEXDSP_LIBRARY_DIRS})
    target_link_libraries(demascas_daemon PRIVATE ${SPEEXDSP_LIBRARIES})
else()
    message(STATUS "speexdsp: fetching source (BSD) for echo cancellation")
    include(FetchContent)
    FetchContent_Declare(speexdsp_src
        GIT_REPOSITORY https://github.com/xiph/speexdsp.git
        GIT_TAG        SpeexDSP-1.2.1
        GIT_SHALLOW    TRUE
        SOURCE_SUBDIR  no-cmake-build)   # sources only — compiled below
    FetchContent_MakeAvailable(speexdsp_src)
    file(WRITE ${speexdsp_src_BINARY_DIR}/include/speex/speexdsp_config_types.h
"#ifndef __SPEEX_TYPES_H__\n#define __SPEEX_TYPES_H__\n#include <stdint.h>\n"
"typedef int16_t spx_int16_t;\ntypedef uint16_t spx_uint16_t;\n"
"typedef int32_t spx_int32_t;\ntypedef uint32_t spx_uint32_t;\n#endif\n")
    set(_SPX ${speexdsp_src_SOURCE_DIR}/libspeexdsp)
    add_library(speexdsp_vendored STATIC
        ${_SPX}/mdf.c ${_SPX}/preprocess.c ${_SPX}/filterbank.c ${_SPX}/fftwrap.c
        ${_SPX}/kiss_fft.c ${_SPX}/kiss_fftr.c ${_SPX}/resample.c ${_SPX}/buffer.c)
    target_include_directories(speexdsp_vendored PUBLIC
        ${speexdsp_src_SOURCE_DIR}/include ${speexdsp_src_BINARY_DIR}/include)
    target_compile_definitions(speexdsp_vendored PRIVATE FLOATING_POINT USE_KISS_FFT EXPORT=)
    if(NOT MSVC)
        target_compile_options(speexdsp_vendored PRIVATE -w)
        target_link_libraries(speexdsp_vendored PRIVATE m)
    endif()
    target_link_libraries(demascas_daemon PRIVATE speexdsp_vendored)
endif()
target_compile_definitions(demascas_daemon PRIVATE HAS_SPEEXDSP=1)

