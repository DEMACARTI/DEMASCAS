"""
DEMASCAS — core/orchestrator.py
Semantic Plan-Mode Orchestrator — "Think then Implement".

The LLM calls architect_plan as a TOOL when it detects a complex,
multi-step goal.  architect_plan calls the MLX server with a
Supervisor prompt + current screen state and returns a JSON plan
that the ReAct loop then executes step-by-step.

Architecture:
  1. LLM (PATH 3 ReAct) receives user command
  2. LLM decides this is complex → calls architect_plan(goal)
  3. architect_plan hits MLX with Supervisor prompt + screen context
  4. Returns JSON plan: {"steps": [...], "tool_hint": "..."}
  5. LLM reads the plan and executes each step using real tools

RAM BUDGET: ~0 MB extra — uses requests (already loaded) + existing
  MLX server on port 8081.  No Node.js, no subprocesses.
"""

from __future__ import annotations

import json
import os

import requests

from core.screen_context import get_screen_context_block, get_tree_only_context


# ── Configuration ────────────────────────────────────────────────────

_MLX_URL = "http://127.0.0.1:8081/v1/chat/completions"
_MLX_MODEL = os.getenv("DEMASCAS_MLX_TEXT_MODEL", "mlx-community/Phi-4-mini-instruct-4bit")
_REQUEST_TIMEOUT = 30  # seconds


# ── Supervisor prompt ────────────────────────────────────────────────

_SUPERVISOR_PROMPT = (
    "You are a macOS task planner. Given a high-level GOAL and the "
    "current SCREEN STATE, produce a step-by-step execution plan.\n\n"
    "RULES:\n"
    "1. Each step must be a single, concrete action (e.g. 'Open Brave Browser', "
    "'Navigate to gmail.com', 'Click Compose button').\n"
    "2. Use ONLY actions that map to real macOS automation tools: "
    "open apps, open URLs, click elements, type text, press shortcuts, "
    "read screen, search web, compose drafts.\n"
    "3. Be specific — use exact app names, URLs, and element labels "
    "visible in the screen state when available.\n"
    "4. Output ONLY valid JSON with this schema:\n"
    '   {"steps": ["step 1", "step 2", ...], "tool_hint": "primary_tool_name"}\n'
    "5. Maximum 7 steps. If the task needs more, output the first 7.\n"
    "6. tool_hint is the most likely first tool needed (e.g. 'open_url', "
    "'open_application', 'browser_click_text').\n"
    "7. If the goal is simple (1 step), output a single-step plan.\n"
)


def architect_plan(goal: str) -> str:
    """
    Break a high-level goal into a concrete step-by-step plan.

    Called as a TOOL by the ReAct loop when the LLM detects a complex
    multi-step task.  Returns a JSON string with the plan.

    Args:
        goal: The user's high-level request (e.g. "Send an email to Aman").

    Returns:
        JSON string: {"steps": [...], "tool_hint": "..."} on success,
        or {"error": "..."} on failure.
    """
    screen_ctx = _get_screen_context()

    messages = [
        {"role": "system", "content": _SUPERVISOR_PROMPT},
        {"role": "user", "content": (
            f"GOAL: {goal}\n\n"
            f"SCREEN STATE:\n{screen_ctx if screen_ctx else '(not available)'}"
        )},
    ]

    body = {
        "model": _MLX_MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": 400,
        "temperature": 0.3,
    }

    try:
        resp = requests.post(_MLX_URL, json=body, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        return json.dumps({"error": f"Planner call failed: {e}"})

    # Parse the JSON plan from the LLM response
    plan = _parse_plan(text)
    return json.dumps(plan)


def _parse_plan(text: str) -> dict:
    """Extract a JSON plan from LLM output."""
    # Try direct JSON parse first
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "steps" in obj:
            steps = [str(s).strip() for s in obj["steps"] if isinstance(s, str) and s.strip()]
            return {
                "steps": steps[:7],
                "tool_hint": obj.get("tool_hint", ""),
            }
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: find JSON object in text
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            obj = json.loads(text[brace_start:brace_end + 1])
            if isinstance(obj, dict) and "steps" in obj:
                steps = [str(s).strip() for s in obj["steps"] if isinstance(s, str) and s.strip()]
                return {
                    "steps": steps[:7],
                    "tool_hint": obj.get("tool_hint", ""),
                }
        except (json.JSONDecodeError, TypeError):
            pass

    # Last resort: look for a JSON array of strings
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start >= 0 and arr_end > arr_start:
        try:
            arr = json.loads(text[arr_start:arr_end + 1])
            if isinstance(arr, list):
                steps = [str(s).strip() for s in arr if isinstance(s, str) and s.strip()]
                return {"steps": steps[:7], "tool_hint": ""}
        except (json.JSONDecodeError, TypeError):
            pass

    return {"error": "Could not parse plan from LLM response", "raw": text[:200]}


def _get_screen_context() -> str:
    """Get current screen context (tree-only for speed)."""
    try:
        return get_tree_only_context()
    except Exception:
        return ""
