"""
DEMASCAS — main.py
Entry point: Launches the Python tool server that the bare-metal
C++ daemon connects to over HTTP.

Architecture (polyglot):
  C++ daemon (daemon/build/demascas_daemon)
    → PortAudio mic capture + whisper.cpp STT
    → Streaming MLX LLM (libcurl SSE, OpenAI-compatible)
    → Streaming TTS with barge-in interrupt
    → Calls this tool server for OS actions, MCP, memory, learning

  Python tool server (this process)
    → Flask on localhost:5001
    → /tool   — execute a native or MCP tool
    → /tools  — list all available tool schemas
    → /health — liveness check
    → /context, /learn — learning endpoints
    → /agent, /confirm — domain agents + human confirmation

Usage:
    python main.py              # start tool server (port 5001)
    python main.py setup        # plug in a local LLM sized to this laptop
    python main.py doctor       # hardware / model / runtime status (--bench for speed)
    ./daemon/build/demascas_daemon  # start voice engine (separate terminal)
"""

import os
import sys

# Ensure the DEMASCAS project root is on sys.path so sibling packages
# (core/, sensors/, actuators/, mcp/) resolve regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


LLM_COMMANDS = {"setup", "doctor", "detect", "llm-check"}


def main() -> None:
    # `python main.py setup|doctor|detect|llm-check` → LLM plug-in CLI
    if len(sys.argv) > 1 and sys.argv[1] in LLM_COMMANDS:
        from core.llm.cli import main as llm_cli
        sys.exit(llm_cli(sys.argv[1:]))

    print("========================================")
    print("   DEMASCAS SYSTEM CORE INITIALIZED     ")
    print("========================================")
    print("[*] Starting Python tool server...")
    print("[*] The C++ daemon connects to this server over HTTP.")
    print("[*] Launch the daemon separately:")
    print("      ./daemon/build/demascas_daemon\n")

    from server.tool_server import run_server  # noqa: E402

    # Same startup path as `python server/tool_server.py` (MCP boot,
    # shutdown hooks, threaded Flask) — host/port come from
    # DEMASCAS_HOST / DEMASCAS_PORT.
    run_server()


if __name__ == "__main__":
    main()
