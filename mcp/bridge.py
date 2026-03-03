"""
DEMASCAS — mcp/bridge.py
Hybrid Tool Bridge — bare-metal optimized.

Merges native in-process tools + MCP remote tools into one array for
Qwen 2.5.  The agent sees a single tools list; routing is invisible.

BARE-METAL OPTIMIZATIONS:
  ▸ Combined tools list is CACHED — rebuilt only when MCP topology
    changes (server added/removed/restarted).  Zero allocation on
    every _call_ollama() hot path.
  ▸ native_names set computed ONCE at registration — not per-call.
  ▸ execute_tool() is a flat if/else — no dict rebuilds, no lookups
    through manager unless the tool is actually an MCP tool.

DISPATCH PRIORITY:
   native (in-process, ~0 µs)  →  MCP (JSON-RPC, ~5-10 ms)
"""

from __future__ import annotations

from typing import Any

from core.toolbox import TOOLS as NATIVE_TOOLS
from mcp.manager import get_manager

# ---------------------------------------------------------------------------
# Native tool registry — populated once by agent.py at import time
# ---------------------------------------------------------------------------
_native_tool_map: dict[str, Any] = {}
_native_names: set[str] = set()

# Combined tools cache — invalidated only when MCP changes
_combined_cache: list[dict] | None = None


def register_native_tools(tool_map: dict[str, Any]) -> None:
    """Register TOOL_MAP from agent.py.  Called once at startup."""
    global _native_names, _combined_cache
    _native_tool_map.update(tool_map)
    _native_names = set(tool_map.keys())
    _combined_cache = None  # force rebuild on next get_combined_tools()


def _invalidate_combined_cache() -> None:
    """Called when MCP topology changes (boot, restart, evict)."""
    global _combined_cache
    _combined_cache = None


# ---------------------------------------------------------------------------
# Unified tool list  (for ollama.chat(tools=[...]))
# ---------------------------------------------------------------------------

def get_combined_tools() -> list[dict]:
    """
    Native TOOLS + MCP tools in Ollama format.
    CACHED — zero allocation on repeated calls.
    """
    global _combined_cache
    if _combined_cache is not None:
        return _combined_cache

    manager = get_manager()
    mcp_tools = manager.get_all_tools_ollama()

    if mcp_tools:
        # Native wins on name collision (Rule 1: fast path stays native)
        filtered = [t for t in mcp_tools if t["function"]["name"] not in _native_names]
        if len(filtered) < len(mcp_tools):
            print(f"[BRIDGE] Skipped {len(mcp_tools) - len(filtered)} MCP tool(s) shadowed by native.")
        _combined_cache = NATIVE_TOOLS + filtered
    else:
        _combined_cache = NATIVE_TOOLS

    return _combined_cache


# ---------------------------------------------------------------------------
# Unified tool execution
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, arguments: dict) -> str:
    """
    Route by priority: native first (0 µs), MCP second (~5 ms).
    """
    # Fast path — in-process
    func = _native_tool_map.get(tool_name)
    if func is not None:
        try:
            return str(func(**arguments))
        except Exception as e:
            return f"[-] Native tool error ({tool_name}): {e}"

    # MCP path
    manager = get_manager()
    if manager.is_mcp_tool(tool_name):
        return manager.call_tool(tool_name, arguments)

    return f"[-] Unknown tool: {tool_name}"


def is_tool_available(tool_name: str) -> bool:
    if tool_name in _native_tool_map:
        return True
    return get_manager().is_mcp_tool(tool_name)


# ---------------------------------------------------------------------------
# Lifecycle helpers (called from main.py)
# ---------------------------------------------------------------------------

def boot_mcp_servers() -> int:
    """Boot MCP servers.  Returns count of live servers."""
    global _combined_cache
    _combined_cache = None
    return get_manager().boot_all()


def shutdown_mcp_servers() -> None:
    global _combined_cache
    _combined_cache = None
    get_manager().shutdown_all()


def mcp_status() -> str:
    return get_manager().status_summary()
