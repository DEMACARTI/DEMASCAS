"""
DEMASCAS — mcp/client.py
Pure-Python MCP Client — JSON-RPC 2.0 over stdio.  Zero dependencies.

BARE-METAL OPTIMIZATIONS (M1 Air / 8 GB unified RAM):
  ▸ select.select() for read timeouts — no thread spawning, no GC pressure.
    macOS maps this to kqueue: zero CPU while waiting.
  ▸ stderr → /dev/null (not PIPE) — prevents hidden RAM buffering.
  ▸ No os.environ.copy() when env overrides are empty (common case).
  ▸ Compact JSON serialization (no whitespace) — fewer bytes over pipe.
  ▸ __slots__ on MCPClient — eliminates per-instance __dict__ (saves ~200 B/obj).
  ▸ Startup poll 50 ms (not 300 ms) — shaves 250 ms off every boot.
  ▸ Default timeout 5 s — fail fast, never block the voice loop.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from typing import Any

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 — bytes-direct (skip intermediate str objects)
# ---------------------------------------------------------------------------
_NL = b"\n"
# compact separators: no spaces → smaller payloads over pipe
_JSEP = (",", ":")


def _req_bytes(method: str, params: dict | None, req_id: int) -> bytes:
    m: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        m["params"] = params
    return json.dumps(m, separators=_JSEP).encode() + _NL


def _notif_bytes(method: str, params: dict | None = None) -> bytes:
    m: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        m["params"] = params
    return json.dumps(m, separators=_JSEP).encode() + _NL


# ---------------------------------------------------------------------------
# MCP Client
# ---------------------------------------------------------------------------

class MCPClient:
    """
    One persistent stdio connection to one MCP server process.

    Memory cost ≈ server process RSS + two pipe fds + this object (~400 B).
    No background threads.  No asyncio.  No buffered stderr.
    """

    __slots__ = (
        "name", "command", "args", "env", "timeout",
        "_process", "_req_id", "_tools_cache", "_alive",
    )

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 5.0,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.timeout = timeout

        self._process: subprocess.Popen | None = None
        self._req_id: int = 0
        self._tools_cache: list[dict] | None = None
        self._alive: bool = False

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> bool:
        """Boot server subprocess + MCP initialize handshake."""
        if self._alive and self._process and self._process.poll() is None:
            return True

        # Env: only copy os.environ when there are actual overrides.
        # Common case (no overrides) → env=None → inherits parent env (zero alloc).
        merged_env: dict[str, str] | None = None
        if self.env:
            merged_env = os.environ.copy()
            for k, v in self.env.items():
                merged_env[k] = os.path.expanduser(v)

        try:
            self._process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,   # no hidden RAM buffer
                env=merged_env,
                start_new_session=True,
            )
        except FileNotFoundError:
            print(f"[MCP:{self.name}] Command not found: {self.command}")
            return False
        except Exception as e:
            print(f"[MCP:{self.name}] Start failed: {e}")
            return False

        # 50 ms poll — just enough for process init, not a ms more
        time.sleep(0.05)
        if self._process.poll() is not None:
            print(f"[MCP:{self.name}] Exited immediately (rc={self._process.returncode}).")
            self._process = None
            return False

        # MCP handshake
        try:
            r = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "DEMASCAS", "version": "1.0.0"},
            })
            if r is None:
                self.stop()
                return False

            self._write(_notif_bytes("notifications/initialized"))

            info = r.get("serverInfo", {})
            print(f"[MCP:{self.name}] OK: {info.get('name', self.name)} v{info.get('version', '?')}")
            self._alive = True
            return True
        except Exception as e:
            print(f"[MCP:{self.name}] Handshake failed: {e}")
            self.stop()
            return False

    def stop(self) -> None:
        """Kill server — release every byte of memory."""
        self._alive = False
        self._tools_cache = None
        proc = self._process
        if proc is None:
            return
        self._process = None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        except Exception:
            pass

    def is_alive(self) -> bool:
        if not self._process:
            return False
        if self._process.poll() is not None:
            self._alive = False
            return False
        return self._alive

    def get_pid(self) -> int | None:
        """Return server PID (for RSS measurement)."""
        return self._process.pid if self._process else None

    # ── MCP protocol ────────────────────────────────────────────────

    def list_tools(self) -> list[dict]:
        """Get tool schemas.  Cached after first call — zero IPC on repeat."""
        if self._tools_cache is not None:
            return self._tools_cache
        r = self._send_request("tools/list", {})
        if r is None:
            return []
        tools = r.get("tools", [])
        self._tools_cache = tools
        return tools

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Invoke a tool, return text result."""
        r = self._send_request("tools/call", {"name": tool_name, "arguments": arguments})
        if r is None:
            return f"[-] MCP:{self.name} no response."
        content = r.get("content", [])
        if isinstance(content, list):
            parts = [
                it.get("text", "")
                for it in content
                if isinstance(it, dict) and it.get("type") == "text"
            ]
            return "\n".join(parts) if parts else str(r)
        return str(content)

    def invalidate_cache(self) -> None:
        self._tools_cache = None

    # ── raw I/O ─────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _write(self, data: bytes) -> bool:
        proc = self._process
        if not proc or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            self._alive = False
            return False

    def _send_request(self, method: str, params: dict) -> dict | None:
        """Send request, block until matching response (or timeout)."""
        proc = self._process
        if not proc or proc.poll() is not None:
            return None
        req_id = self._next_id()
        if not self._write(_req_bytes(method, params, req_id)):
            return None
        return self._read_response(req_id)

    def _read_response(self, expected_id: int) -> dict | None:
        """
        Read line-delimited JSON-RPC using select() for timeout.
        select() on macOS = kqueue: zero CPU, zero threads, zero allocs
        while waiting.  The only allocation is the readline buffer itself.
        """
        proc = self._process
        if not proc or not proc.stdout:
            return None
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + self.timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break

            raw = proc.stdout.readline()
            if not raw:                    # EOF — server died
                self._alive = False
                return None

            line = raw.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "id" not in msg:            # server notification — skip
                continue

            if msg.get("id") == expected_id:
                if "error" in msg:
                    err = msg["error"]
                    print(f"[MCP:{self.name}] RPC error: {err.get('message', err)}")
                    return None
                return msg.get("result", {})
            # mismatched id — discard, keep reading

        print(f"[MCP:{self.name}] Timeout (id={expected_id})")
        return None
