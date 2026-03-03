"""
DEMASCAS — main.py
Entry point: Launches the Python tool server that the bare-metal
C++ daemon connects to over HTTP.

Architecture (polyglot):
  C++ daemon (daemon/build/demascas_daemon)
    → PortAudio mic capture + whisper.cpp STT
    → Streaming Ollama (libcurl NDJSON)
    → Streaming TTS with barge-in interrupt
    → Calls this tool server for OS actions, MCP, memory, learning

  Python tool server (this process)
    → Flask on localhost:5001
    → /tool   — execute a native or MCP tool
    → /tools  — list all available tool schemas
    → /health — liveness check
    → /context, /learn — learning endpoints

Usage:
    python main.py              # start tool server (port 5001)
    ./daemon/build/demascas_daemon  # start voice engine (separate terminal)
"""

import os
import sys

# Ensure the DEMASCAS project root is on sys.path so sibling packages
# (core/, sensors/, actuators/, mcp/) resolve regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    print("========================================")
    print("   DEMASCAS SYSTEM CORE INITIALIZED     ")
    print("========================================")
    print("[*] Starting Python tool server...")
    print("[*] The C++ daemon connects to this server over HTTP.")
    print("[*] Launch the daemon separately:")
    print("      ./daemon/build/demascas_daemon\n")

    from server.tool_server import app  # noqa: E402

    # Run the Flask tool server — the C++ daemon talks to this.
    # threaded=True so the daemon can make concurrent tool calls.
    app.run(host="127.0.0.1", port=5001, threaded=True)


if __name__ == "__main__":
    main()
