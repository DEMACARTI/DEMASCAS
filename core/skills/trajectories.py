"""
DEMASCAS — core/skills/trajectories.py
Experience log: every command DEMASCAS handles becomes a trajectory —
what you asked, which tools ran (with arguments and outcomes), the LLM
turns that chose them, whether it worked, and your feedback.

Used by
  * induce.py   — turn successful multi-step runs into reusable skills
  * finetune/   — verified successes become LoRA training data
Stored as JSONL in ~/.demascas/trajectories/YYYY-MM.jsonl (append-only).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

TRAJ_DIR = os.path.join(os.path.expanduser("~"), ".demascas", "trajectories")

# Bookkeeping reads the daemon does on its own — not part of what the user asked.
_IGNORED_TOOLS = {"retrieve_memories", "get_active_window_tree", "list_open_windows"}

_lock = threading.Lock()
_buffer: list[dict] = []          # tool calls since the last finished command
_turns: list[dict] = []           # agent LLM turns since the last finished command
_last: dict | None = None         # most recent trajectory (for feedback)


def note_tool_call(tool: str, args: dict, result: str, ok: bool) -> None:
    """Called by the tool server for every executed tool."""
    if tool in _IGNORED_TOOLS:
        return
    with _lock:
        _buffer.append({"tool": tool, "args": args, "result": (result or "")[:240], "ok": bool(ok)})
        del _buffer[:-40]          # bound memory


def note_llm_turn(messages: list, output: str) -> None:
    """Agents (running inside the tool server) record each decision."""
    with _lock:
        _turns.append({"messages": messages, "output": output})
        del _turns[:-6]


def finish(command: str, path: str, success: bool, reply: str = "",
           llm_turns: list | None = None, skill_id: str = "") -> dict:
    """Close the current command: persist a trajectory and return it."""
    global _last
    with _lock:
        calls = list(_buffer)
        _buffer.clear()
        agent_turns = list(_turns)
        _turns.clear()
    traj = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "command": command,
        "path": path,
        "skill_id": skill_id,
        "tool_calls": calls,
        "success": bool(success),
        "reply": (reply or "")[:300],
        "llm_turns": llm_turns or agent_turns,
        "feedback": None,          # "positive" | "negative" | None (implicit)
    }
    _append(traj)
    _last = traj
    return traj


def last() -> dict | None:
    return _last


def set_feedback(positive: bool) -> dict | None:
    """Attach the user's verdict to the most recent trajectory."""
    t = _last
    if t is None:
        return None
    t["feedback"] = "positive" if positive else "negative"
    _append({"id": t["id"], "feedback": t["feedback"], "ts": time.time(), "type": "feedback"})
    return t


def _append(rec: dict) -> None:
    try:
        os.makedirs(TRAJ_DIR, exist_ok=True)
        path = os.path.join(TRAJ_DIR, time.strftime("%Y-%m") + ".jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[TRAJ] could not write trajectory: {e}")


def load_all() -> list[dict]:
    """All trajectories with feedback records folded in."""
    trajs: dict[str, dict] = {}
    if not os.path.isdir(TRAJ_DIR):
        return []
    for fn in sorted(os.listdir(TRAJ_DIR)):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(TRAJ_DIR, fn)) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "feedback":
                    if r.get("id") in trajs:
                        trajs[r["id"]]["feedback"] = r.get("feedback")
                elif r.get("id"):
                    trajs[r["id"]] = r
    return list(trajs.values())
