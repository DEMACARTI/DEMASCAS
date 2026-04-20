"""
DEMASCAS — mcp/config.py
MCP Server Configuration — bare-metal safe.

HARD RULES FOR M1 AIR (8 GB unified RAM):
  ▸ Max 3 servers — enforced. Every process eats unified memory that
    Qwen and Whisper need.
  ▸ Per-server RSS budget via "max_rss_mb" — manager kills violators.
  ▸ Node.js (npx/node) commands are BLOCKED at boot by the manager.
    Use Python or Rust servers only.
  ▸ All servers boot once at startup — no cold starts.
  ▸ Core OS tools (click, type, vision) NEVER go into MCP.

Config fields per server:
  "name"         — Label (logs only)
  "command"      — Executable ("python3", "./my_server", etc.)
  "args"         — CLI arguments list
  "env"          — Extra env vars (merged with parent env)
  "enabled"      — false to skip
  "max_rss_mb"   — RSS ceiling in MB (default 100). Server killed if exceeded.
  "timeout"      — IPC timeout in seconds (default 5)
  "description"  — Human note (ignored by code)
"""

from __future__ import annotations

import json
import os
from typing import Any

_CONFIG_DIR = os.path.expanduser("~/.demascas")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "mcp_servers.json")

# Hard ceiling — never change this without checking total system RAM.
MAX_SERVERS = 3

# ---------------------------------------------------------------------------
# Defaults — all disabled.  User edits ~/.demascas/mcp_servers.json to enable.
# ---------------------------------------------------------------------------
DEFAULT_SERVERS: list[dict[str, Any]] = [
    # Python SQLite server (~15 MB RSS)
    {
        "name": "sqlite",
        "command": "python3",
        "args": ["-m", "mcp.servers.example_sqlite"],
        "env": {"SQLITE_DB_PATH": "~/Documents/my_data.db"},
        "enabled": False,
        "max_rss_mb": 50,
        "timeout": 5,
        "description": "Local SQLite search — Python, ~15 MB.",
    },
    # BLOCKED EXAMPLE — shows why Node.js is rejected:
    # {
    #     "name": "github",
    #     "command": "npx",            ← BLOCKED by manager (Node.js)
    #     "args": ["-y", "@modelcontextprotocol/server-github"],
    #     "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."},
    #     "enabled": false,
    #     "max_rss_mb": 150,           ← Would eat 150 MB from Qwen's VRAM
    #     "description": "DON'T USE — rewrite in Python/Rust to stay lean."
    # },
]


def load_server_configs() -> list[dict[str, Any]]:
    """
    Load enabled server configs.  Hard-caps at MAX_SERVERS.

    Priority: ~/.demascas/mcp_servers.json > DEFAULT_SERVERS.
    """
    servers = DEFAULT_SERVERS

    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r") as f:
                user = json.load(f)
            if isinstance(user, list):
                servers = user
        except (json.JSONDecodeError, OSError) as e:
            print(f"[MCP CFG] Bad config: {e} — using defaults.")

    enabled = [s for s in servers if s.get("enabled", True)]

    if len(enabled) > MAX_SERVERS:
        print(f"[MCP CFG] {len(enabled)} servers enabled, cap is {MAX_SERVERS}. Truncating.")
        enabled = enabled[:MAX_SERVERS]

    return enabled


def save_default_config() -> None:
    """Write template to ~/.demascas/mcp_servers.json if it doesn't exist."""
    if os.path.isfile(_CONFIG_FILE):
        return
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_SERVERS, f, indent=2)
    print(f"[MCP CFG] Template written to {_CONFIG_FILE}")
