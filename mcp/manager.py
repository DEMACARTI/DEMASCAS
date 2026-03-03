"""
DEMASCAS — mcp/manager.py
MCP Server Lifecycle Manager — bare-metal optimized.

Boots configured MCP servers at startup, keeps stdio connections alive,
and provides unified tool discovery + routing.

BARE-METAL OPTIMIZATIONS:
  ▸ RSS watchdog — measures actual memory of each server process via
    resource.getrusage / subprocess, kills anything over its budget.
  ▸ Ollama tool schema cache — built ONCE at boot, not on every LLM call.
    Invalidated only when server topology changes.
  ▸ Max 1 restart attempt — if a server crashes twice, it's dead weight.
  ▸ Node.js blocker — rejects `npx`/`node` commands with a loud warning.
    Node.js servers eat 80-150 MB each; on 8 GB that's suicide.
  ▸ Hard cap: max 3 servers total — enforced at config load time.
  ▸ Zero sleep on restart — uses poll() instead.
"""

from __future__ import annotations

import os
import time
from typing import Any

from mcp.client import MCPClient
from mcp.config import load_server_configs, save_default_config

# ---------------------------------------------------------------------------
# Hard limits for M1 Air (8 GB unified RAM)
# ---------------------------------------------------------------------------
_MAX_SERVERS = 3               # Hard ceiling — never exceed this
_MAX_RETRIES = 1               # One restart attempt, then permanent disable
_DEFAULT_MAX_RSS_MB = 100      # Per-server RSS budget (MB); kill if exceeded
_NODE_COMMANDS = frozenset({"npx", "node", "nodejs"})  # RAM-heavy blocklist


def _get_rss_mb(pid: int) -> float | None:
    """
    Get Resident Set Size of a process in MB.  macOS-native, zero deps.
    Uses /usr/bin/ps (always available) — avoids importing psutil.
    """
    try:
        # ps -o rss= returns RSS in KB on macOS
        import subprocess
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return int(out.strip()) / 1024.0   # KB → MB
    except Exception:
        return None


class MCPManager:
    """
    Lifecycle manager for all MCP server connections.

    Memory cost ≈ a few dicts + the MCPClient objects (~400 B each).
    The heavy cost is the server *processes* — monitored via RSS watchdog.
    """

    __slots__ = (
        "_clients", "_tool_to_server", "_retry_counts",
        "_ollama_cache", "_ollama_dirty", "_booted", "_server_configs",
    )

    def __init__(self):
        self._clients: dict[str, MCPClient] = {}
        self._tool_to_server: dict[str, str] = {}
        self._retry_counts: dict[str, int] = {}
        self._server_configs: dict[str, dict] = {}   # name → original config
        # Ollama schema cache — rebuilt only when topology changes
        self._ollama_cache: list[dict] = []
        self._ollama_dirty: bool = True
        self._booted: bool = False

    # ── startup / shutdown ──────────────────────────────────────────

    def boot_all(self) -> int:
        """Boot all enabled MCP servers.  Returns count of live servers."""
        save_default_config()
        configs = load_server_configs()
        if not configs:
            print("[MCP] No servers configured — native tools only.")
            self._booted = True
            return 0

        # Hard cap
        if len(configs) > _MAX_SERVERS:
            print(f"[MCP] WARNING: {len(configs)} servers configured, cap is {_MAX_SERVERS}. Truncating.")
            configs = configs[:_MAX_SERVERS]

        print(f"[MCP] Booting {len(configs)} server(s)...")
        ok = 0

        for cfg in configs:
            name = cfg.get("name", "unnamed")
            command = cfg.get("command", "")
            if not command:
                continue

            # Block Node.js servers — they're RAM hogs on 8 GB
            base_cmd = os.path.basename(command)
            if base_cmd in _NODE_COMMANDS:
                print(f"[MCP] BLOCKED '{name}': Node.js servers use 80-150 MB RAM.")
                print(f"[MCP]   Rewrite as Python or Rust, or set enabled=false.")
                continue

            client = MCPClient(
                name=name,
                command=command,
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                timeout=cfg.get("timeout", 5.0),
            )

            if client.start():
                self._clients[name] = client
                self._server_configs[name] = cfg
                self._retry_counts[name] = 0

                tools = client.list_tools()
                for t in tools:
                    tn = t.get("name", "")
                    if tn:
                        self._tool_to_server[tn] = name
                self._ollama_dirty = True

                # Immediate RSS check
                rss = _get_rss_mb(client.get_pid()) if client.get_pid() else None
                rss_str = f" ({rss:.1f} MB)" if rss else ""
                budget = cfg.get("max_rss_mb", _DEFAULT_MAX_RSS_MB)
                if rss and rss > budget:
                    print(f"[MCP] KILLED '{name}': RSS {rss:.0f} MB > budget {budget} MB.")
                    client.stop()
                    del self._clients[name]
                    continue

                print(f"[MCP] ✓ {name}: {len(tools)} tool(s){rss_str}")
                ok += 1
            else:
                print(f"[MCP] ✗ {name}: failed to start.")

        self._booted = True
        print(f"[MCP] Ready — {ok}/{len(configs)} server(s), {len(self._tool_to_server)} tool(s).")
        return ok

    def shutdown_all(self) -> None:
        """Kill all servers — reclaim every byte."""
        for client in self._clients.values():
            try:
                client.stop()
            except Exception:
                pass
        self._clients.clear()
        self._tool_to_server.clear()
        self._retry_counts.clear()
        self._server_configs.clear()
        self._ollama_cache.clear()
        self._ollama_dirty = True
        self._booted = False
        print("[MCP] All servers shut down.")

    # ── tool discovery ──────────────────────────────────────────────

    def get_all_tools_mcp(self) -> list[dict]:
        """Raw MCP tool schemas from all live servers."""
        tools = []
        for client in self._clients.values():
            if client.is_alive():
                tools.extend(client.list_tools())
        return tools

    def get_all_tools_ollama(self) -> list[dict]:
        """
        MCP tools in Ollama format.  CACHED — only rebuilt when servers
        change.  Zero allocation on the hot path.
        """
        if not self._ollama_dirty:
            return self._ollama_cache
        self._ollama_cache = [self._to_ollama(t) for t in self.get_all_tools_mcp()]
        self._ollama_dirty = False
        return self._ollama_cache

    def get_mcp_tool_names(self) -> set[str]:
        return set(self._tool_to_server.keys())

    # ── tool execution ──────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        server = self._tool_to_server.get(tool_name)
        if not server:
            return f"[-] Unknown MCP tool: {tool_name}"
        client = self._clients.get(server)
        if not client:
            return f"[-] MCP server '{server}' gone."
        if not client.is_alive():
            if not self._try_restart(server):
                return f"[-] MCP:{server} crashed, restart failed."
            client = self._clients[server]
        return client.call_tool(tool_name, arguments)

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_server

    # ── health check + RSS watchdog ─────────────────────────────────

    def health_check(self) -> dict[str, bool]:
        """Check liveness + RSS budget for every server."""
        out: dict[str, bool] = {}
        for name, client in list(self._clients.items()):
            alive = client.is_alive()
            out[name] = alive

            if not alive:
                self._try_restart(name)
                continue

            # RSS watchdog
            pid = client.get_pid()
            if pid:
                rss = _get_rss_mb(pid)
                budget = self._server_configs.get(name, {}).get("max_rss_mb", _DEFAULT_MAX_RSS_MB)
                if rss and rss > budget:
                    print(f"[MCP] KILLING '{name}': RSS {rss:.0f} MB > budget {budget} MB.")
                    client.stop()
                    self._evict_server(name)
                    out[name] = False
        return out

    def _evict_server(self, name: str) -> None:
        """Remove a server and all its tools from the routing table."""
        dead = [t for t, s in self._tool_to_server.items() if s == name]
        for t in dead:
            del self._tool_to_server[t]
        self._clients.pop(name, None)
        self._ollama_dirty = True

    def _try_restart(self, name: str) -> bool:
        count = self._retry_counts.get(name, 0)
        if count >= _MAX_RETRIES:
            print(f"[MCP] '{name}' exceeded {_MAX_RETRIES} retries — disabled.")
            self._evict_server(name)
            return False

        self._retry_counts[name] = count + 1
        client = self._clients.get(name)
        if not client:
            return False

        client.stop()
        # No sleep — poll-based start handles its own timing
        if client.start():
            client.invalidate_cache()
            for t in client.list_tools():
                tn = t.get("name", "")
                if tn:
                    self._tool_to_server[tn] = name
            self._ollama_dirty = True
            print(f"[MCP] ✓ '{name}' restarted.")
            return True
        print(f"[MCP] ✗ '{name}' restart failed.")
        return False

    # ── schema conversion ───────────────────────────────────────────

    @staticmethod
    def _to_ollama(mcp_tool: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": mcp_tool.get("name", "unknown"),
                "description": mcp_tool.get("description", "MCP tool."),
                "parameters": mcp_tool.get("inputSchema", {
                    "type": "object", "properties": {}, "required": [],
                }),
            },
        }

    # ── info ────────────────────────────────────────────────────────

    def status_summary(self) -> str:
        if not self._clients:
            return "[MCP] No servers."
        lines = ["[MCP STATUS]"]
        for name, client in self._clients.items():
            alive = client.is_alive()
            pid = client.get_pid()
            rss = _get_rss_mb(pid) if pid and alive else None
            rss_str = f" {rss:.1f} MB" if rss else ""
            names = [t.get("name", "?") for t in client.list_tools()] if alive else []
            lines.append(f"  {name}: {'UP' if alive else 'DOWN'}{rss_str} — {names}")
        lines.append(f"  Total: {len(self._tool_to_server)} tool(s)")
        return "\n".join(lines)


# ── singleton ───────────────────────────────────────────────────────
_manager: MCPManager | None = None

def get_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
