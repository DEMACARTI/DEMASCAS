#!/usr/bin/env python3
"""
DEMASCAS — server/tool_server.py
Unified Tool Gateway — serves both native + MCP tools over HTTP.

The C++ daemon hits this single endpoint; routing to native in-process
Python functions or to MCP subprocess servers is invisible to the caller.

Endpoints:
  POST /tool       — Execute a named tool  {"name": "...", "arguments": {...}}
  GET  /tools      — Return ALL tool schemas (native + MCP, Ollama format)
  GET  /health     — Readiness check  {"status": "ok", "native": N, "mcp": M}
  GET  /mcp/status — MCP-specific server status (RSS, tool list per server)

Start:
  python server/tool_server.py
"""

import atexit
import sys
import os

# Ensure the DEMASCAS package root is on the path so we can import
# actuators/, sensors/, core/, and mcp/ directly.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from flask import Flask, request, jsonify

# ── Import all native tool functions ─────────────────────────────────

from actuators.file_system import summarize_latest_pdf, list_files_in_folder
from actuators.network import search_web, read_webpage
from actuators.system_control import (
    click_ui_element,
    open_application,
    press_keyboard_shortcut,
    type_text_into_element,
    quit_application,
    activate_application,
    list_running_applications,
    scroll_direction,
    drag_element,
)
from core.memory import save_memory, retrieve_memories
from sensors.vision import get_active_window_tree, list_open_windows
from core.toolbox import TOOLS as NATIVE_TOOL_SCHEMAS

# ── Import MCP manager (lazy — zero cost if no servers configured) ───

from mcp.manager import get_manager, MCPManager

# ── Native tool dispatch map (mirrors core/agent.py TOOL_MAP) ────────

TOOL_MAP = {
    "click_ui_element":          click_ui_element,
    "type_text_into_element":    type_text_into_element,
    "open_application":          open_application,
    "press_keyboard_shortcut":   press_keyboard_shortcut,
    "summarize_latest_pdf":      summarize_latest_pdf,
    "get_active_window_tree":    get_active_window_tree,
    "quit_application":          quit_application,
    "activate_application":      activate_application,
    "list_running_applications": list_running_applications,
    "list_files_in_folder":      list_files_in_folder,
    "search_web":                search_web,
    "read_webpage":              read_webpage,
    "save_memory":               save_memory,
    "retrieve_memories":         retrieve_memories,
    "scroll_direction":          scroll_direction,
    "drag_element":              drag_element,
    "list_open_windows":         list_open_windows,
}

_NATIVE_NAMES = frozenset(TOOL_MAP.keys())

# ── Flask app ────────────────────────────────────────────────────────

app = Flask(__name__)

# ── MCP singleton — booted once at server start ─────────────────────

_mcp: MCPManager | None = None


def _boot_mcp() -> MCPManager:
    """Boot MCP servers.  Called once at startup.  Non-fatal on failure."""
    global _mcp
    _mcp = get_manager()
    try:
        n = _mcp.boot_all()
        if n > 0:
            print(_mcp.status_summary())
    except Exception as e:
        print(f"[!] MCP boot failed (non-fatal): {e}")
    return _mcp


def _shutdown_mcp() -> None:
    """Registered via atexit — reclaims every MCP subprocess."""
    if _mcp is not None:
        try:
            _mcp.shutdown_all()
        except Exception:
            pass


# ── Unified tool list (native + MCP, no name collisions) ────────────

def _all_tool_schemas() -> list[dict]:
    """
    Merge NATIVE_TOOL_SCHEMAS + MCP Ollama schemas.
    Native wins on name collision (same rule as mcp/bridge.py).
    """
    if _mcp is None:
        return NATIVE_TOOL_SCHEMAS

    mcp_tools = _mcp.get_all_tools_ollama()
    if not mcp_tools:
        return NATIVE_TOOL_SCHEMAS

    # Filter collisions — native always wins (Rule 1: fast path stays native)
    filtered = [t for t in mcp_tools if t["function"]["name"] not in _NATIVE_NAMES]
    return NATIVE_TOOL_SCHEMAS + filtered


# ── Endpoints ────────────────────────────────────────────────────────

@app.route("/tool", methods=["POST"])
def execute_tool():
    """Execute a named tool — routes to native or MCP automatically."""
    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"error": "Missing 'name' in request body"}), 400

    name = data["name"]
    args = data.get("arguments", {})

    # Priority 1: native (in-process, ~0 µs overhead)
    func = TOOL_MAP.get(name)
    if func is not None:
        try:
            result = str(func(**args))
            return jsonify({"result": result})
        except Exception as e:
            return jsonify({"result": f"[-] Tool error ({name}): {e}"}), 200

    # Priority 2: MCP (JSON-RPC over stdio, ~5-10 ms)
    if _mcp is not None and _mcp.is_mcp_tool(name):
        try:
            result = _mcp.call_tool(name, args)
            return jsonify({"result": result})
        except Exception as e:
            return jsonify({"result": f"[-] MCP tool error ({name}): {e}"}), 200

    return jsonify({"error": f"Unknown tool: {name}"}), 404


@app.route("/tools", methods=["GET"])
def get_tool_schemas():
    """Return ALL tool schemas (native + MCP) in Ollama format."""
    return jsonify(_all_tool_schemas())


@app.route("/health", methods=["GET"])
def health_check():
    """Readiness probe — includes native + MCP counts."""
    mcp_count = len(_mcp.get_mcp_tool_names()) if _mcp else 0
    return jsonify({
        "status": "ok",
        "native": len(TOOL_MAP),
        "mcp": mcp_count,
        "tools": len(TOOL_MAP) + mcp_count,
    })


@app.route("/mcp/status", methods=["GET"])
def mcp_server_status():
    """MCP-specific details — server names, RSS, tool lists."""
    if _mcp is None:
        return jsonify({"status": "not_initialized"})
    # Trigger RSS watchdog while we're at it
    health = _mcp.health_check()
    return jsonify({
        "servers": health,
        "summary": _mcp.status_summary(),
    })


# ── Learning endpoints (used by C++ daemon) ─────────────────────────

@app.route("/context", methods=["GET"])
def get_learned_context():
    """
    Fetch learned context for a query.  The C++ daemon calls this BEFORE
    sending the user's command to Ollama, so the LLM sees personalized
    context from past interactions.

    Query params:
      ?query=open+chrome   — the user's command (URL-encoded)
    """
    from core.learning import get_context_for_http
    query = request.args.get("query", "")
    if not query:
        return jsonify({"context_text": "", "profile_summary": ""})
    try:
        ctx = get_context_for_http(query)
        return jsonify(ctx)
    except Exception as e:
        return jsonify({"context_text": "", "error": str(e)})


@app.route("/learn", methods=["POST"])
def log_learning():
    """
    Log an interaction for learning.  The C++ daemon calls this AFTER
    each command completes so the learning engine can track episodes
    and update the user profile.

    Body: {
      "command": "open chrome",
      "path": "tool",
      "tools": ["open_application"],
      "result": "Opened Chrome.",
      "success": true
    }
    """
    from core.learning import after_command, track_app_open
    data = request.get_json(silent=True)
    if not data or "command" not in data:
        return jsonify({"error": "Missing 'command'"}), 400

    try:
        after_command(
            command=data["command"],
            path_taken=data.get("path", "tool"),
            tools_used=data.get("tools", []),
            result=data.get("result", ""),
            success=data.get("success", True),
        )
        # Track app opens if present
        for tool in data.get("tools", []):
            if tool in ("open_application", "activate_application"):
                # Try to extract app name from the result
                pass
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 200


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    # Boot MCP servers before Flask starts accepting requests
    _boot_mcp()
    atexit.register(_shutdown_mcp)

    mcp_count = len(_mcp.get_mcp_tool_names()) if _mcp else 0
    total = len(TOOL_MAP) + mcp_count

    print(f"[*] DEMASCAS Tool Server starting ({total} tools: "
          f"{len(TOOL_MAP)} native + {mcp_count} MCP)...")
    print(f"[*] Endpoints:")
    print(f"      POST http://127.0.0.1:5001/tool")
    print(f"      GET  http://127.0.0.1:5001/tools")
    print(f"      GET  http://127.0.0.1:5001/health")
    print(f"      GET  http://127.0.0.1:5001/mcp/status")
    print(f"      GET  http://127.0.0.1:5001/context?query=...")
    print(f"      POST http://127.0.0.1:5001/learn")
    print()

    # Disable Flask's default banner and request logging for clean output
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
