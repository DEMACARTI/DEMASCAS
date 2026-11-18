"""
DEMASCAS — main.py
Entry point: Launches the Python tool server that the Rust daemon
(core-rs) connects to over HTTP.

Architecture:
  Rust daemon (core-rs/target/release/demascasd)
    → mic capture + whisper STT, wake word, barge-in
    → the brain: reflexes, router, planner, governance
    → streaming local LLM (OpenAI-compatible) + Kokoro TTS
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
    ./scripts/run.sh            # everything: LLM check, tool server, Rust daemon
"""

import os
import sys

# Ensure the DEMASCAS project root is on sys.path so sibling packages
# (core/, sensors/, actuators/, mcp/) resolve regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


LLM_COMMANDS = {"setup", "doctor", "detect", "llm-check",
                "learn", "adapters", "rollback", "skills", "web"}


def main() -> None:
    # `python main.py setup|doctor|detect|llm-check` → LLM plug-in CLI
    if len(sys.argv) > 1 and sys.argv[1] in LLM_COMMANDS:
        from core.llm.cli import main as llm_cli
        sys.exit(llm_cli(sys.argv[1:]))

    print("========================================")
    print("   DEMASCAS SYSTEM CORE INITIALIZED     ")
    print("========================================")
    print("[*] Starting Python tool server...")
    print("[*] The Rust daemon connects to this server over HTTP.")
    print("[*] Launch everything with:")
    print("      ./scripts/run.sh\n")

    from server.tool_server import run_server  # noqa: E402

    # Same startup path as `python server/tool_server.py` (MCP boot,
    # shutdown hooks, threaded Flask) — host/port come from
    # DEMASCAS_HOST / DEMASCAS_PORT.
    run_server()


if __name__ == "__main__":
    main()
