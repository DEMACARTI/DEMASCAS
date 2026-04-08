"""
DEMASCAS — mcp/servers/example_sqlite.py
MCP Server: Local SQLite search — bare-metal Python.

Target RSS: ~10-15 MB.  Zero external deps beyond stdlib.

OPTIMIZATIONS:
  ▸ No persistent DB connection — open per-call, close instantly.
    SQLite in :memory: mode adds ~0 overhead for read-only file access.
  ▸ Row cap: 20 rows max.  Cell value truncated to 200 chars.
    Keeps response payload small → less RAM in the LLM context window.
  ▸ sys.stdout.buffer.write() — binary I/O, skips Python's TextIOWrapper
    encoding overhead.
  ▸ stderr → devnull (not a log file) — no extra fd, no disk I/O.
  ▸ Compact JSON (no whitespace) — smaller pipe payloads.

Run standalone:
  SQLITE_DB_PATH=~/test.db python3 -m mcp.servers.example_sqlite
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.path.expanduser(os.environ.get("SQLITE_DB_PATH", "~/Documents/my_data.db"))
_MAX_ROWS = 20       # Hard cap — never send more than this to the LLM
_MAX_CELL = 200      # Truncate any cell value longer than this

SERVER_INFO = {"name": "demascas-sqlite", "version": "1.0.0"}
CAPABILITIES = {"tools": {}}

# Compact separators — reused for every response
_JSEP = (",", ":")

# ---------------------------------------------------------------------------
# Tool schemas (MCP format)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "search_sqlite",
        "description": (
            "Run a read-only SQL SELECT on a local SQLite database. "
            "Returns up to 20 rows.  No INSERT/UPDATE/DELETE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SQL SELECT query (e.g. \"SELECT * FROM notes LIMIT 10\").",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_sqlite_tables",
        "description": "Lists tables and column schemas in the SQLite database.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _open_db():
    """Read-only connection.  Returns (conn, err_str)."""
    if not os.path.isfile(DB_PATH):
        return None, f"DB not found: {DB_PATH}"
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        return conn, None
    except Exception as e:
        return None, str(e)


def _trunc(val: Any) -> str:
    s = str(val)
    return s[:_MAX_CELL] + "…" if len(s) > _MAX_CELL else s


def _text(t: str) -> list[dict]:
    return [{"type": "text", "text": t}]


def handle_search(args: dict) -> list[dict]:
    q = (args.get("query") or "").strip()
    if not q:
        return _text("Error: empty query.")
    if not q.lstrip().upper().startswith("SELECT"):
        return _text("Error: only SELECT allowed.")

    conn, err = _open_db()
    if err:
        return _text(f"Error: {err}")
    try:
        cur = conn.execute(q)
        rows = cur.fetchmany(_MAX_ROWS + 1)       # +1 to detect truncation
        if not rows:
            return _text("No results.")
        cols = [d[0] for d in cur.description]
        lines = [" | ".join(cols)]
        for r in rows[:_MAX_ROWS]:
            lines.append(" | ".join(_trunc(v) for v in r))
        if len(rows) > _MAX_ROWS:
            lines.append(f"(showing {_MAX_ROWS} of more rows)")
        return _text("\n".join(lines))
    except Exception as e:
        return _text(f"SQL error: {e}")
    finally:
        conn.close()


def handle_tables(args: dict) -> list[dict]:
    conn, err = _open_db()
    if err:
        return _text(f"Error: {err}")
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        if not tables:
            return _text("No tables.")
        lines = []
        for t in tables:
            cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            col_str = ", ".join(f"{c[1]}:{c[2] or '?'}" for c in cols)
            lines.append(f"{t} ({cnt} rows) — {col_str}")
        return _text("\n".join(lines))
    except Exception as e:
        return _text(f"Error: {e}")
    finally:
        conn.close()


_HANDLERS = {"search_sqlite": handle_search, "list_sqlite_tables": handle_tables}

# ---------------------------------------------------------------------------
# JSON-RPC I/O — binary stdout, compact JSON
# ---------------------------------------------------------------------------
_stdout = sys.stdout.buffer   # bypass TextIOWrapper


def _send(msg: dict) -> None:
    _stdout.write(json.dumps(msg, separators=_JSEP).encode() + b"\n")
    _stdout.flush()


def _ok(rid: Any, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _err(rid: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------

def _dispatch(msg: dict) -> None:
    rid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "initialize":
        _ok(rid, {"protocolVersion": "2024-11-05", "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        _ok(rid, {"tools": TOOLS})
    elif method == "tools/call":
        h = _HANDLERS.get(params.get("name", ""))
        if h is None:
            _err(rid, -32601, f"Unknown tool: {params.get('name')}")
        else:
            try:
                _ok(rid, {"content": h(params.get("arguments", {}))})
            except Exception as e:
                _ok(rid, {"content": _text(f"Error: {e}"), "isError": True})
    elif method == "ping":
        _ok(rid, {})
    elif rid is not None:
        _err(rid, -32601, f"Unknown method: {method}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    # Silence stderr entirely — no log file fd, no disk I/O
    sys.stderr = open(os.devnull, "w")

    for raw in sys.stdin.buffer:             # binary read — no TextIOWrapper
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _dispatch(msg)
        except Exception as e:
            rid = msg.get("id") if isinstance(msg, dict) else None
            if rid is not None:
                _err(rid, -32603, str(e))


if __name__ == "__main__":
    main()
