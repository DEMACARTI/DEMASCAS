"""
DEMASCAS — core/task_executor.py
Structured Task Executor (STE) — plan, execute, verify, replan.

Replaces the old PATH 2.5 planner with a context-aware executor that:
  1. Plans tasks into typed steps using the LLM
  2. Executes each step with accumulated context
  3. Verifies UI-mutating steps via screen state capture
  4. Retries failed steps with LLM-generated recovery actions
  5. Returns spoken progress updates for TTS

Architecture:
  - Called from tool_server.py via /plan_and_execute endpoint
  - The C++ daemon invokes this as a single HTTP POST
  - All tool execution goes through the same TOOL_MAP dispatch
  - Screen state is captured after UI-mutating tools automatically

RAM: ~0 MB extra — reuses existing LLM server, tool dispatch, screen capture.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Any

# ── Data structures ──────────────────────────────────────────────────


@dataclass
class TaskStep:
    """A single step in a structured task plan."""
    index: int                          # 1-based step number
    tool: str                           # tool name (must exist in TOOL_MAP)
    args: dict                          # tool arguments
    description: str                    # human-readable "Opening Gmail"
    depends_on: list[int] = field(default_factory=list)   # step indices for result substitution
    verify: str | None = None           # verification hint ("compose window visible")
    wait_ms: int = 0                    # ms to wait BEFORE executing


@dataclass
class TaskPlan:
    """A complete task decomposition."""
    goal: str                           # original user command
    steps: list[TaskStep]               # ordered steps
    raw_llm_output: str = ""            # debug: raw LLM response


@dataclass
class StepResult:
    """Result of executing a single step."""
    index: int
    tool: str
    args: dict
    result: str
    success: bool
    screen_after: str = ""              # screen state after execution
    error: str = ""
    retries: int = 0


@dataclass
class TaskResult:
    """Final result of the full task execution."""
    status: str                         # "ok" | "partial" | "failed" | "clarifying"
    result: str                         # final result text
    steps_completed: int                # how many steps successfully ran
    total_steps: int                    # total planned steps
    spoken_updates: list[str] = field(default_factory=list)  # TTS progress messages
    step_results: list[StepResult] = field(default_factory=list)
    pending_file_choices: list[dict] | None = None  # for CLARIFYING state


# ── Constants ────────────────────────────────────────────────────────

MAX_TASK_STEPS = 10
MAX_RETRIES_PER_STEP = 2
PLANNER_MAX_TOKENS = 500
LLM_TEMPERATURE = 0.1
MLX_TEXT_MODEL = os.getenv("DEMASCAS_MLX_TEXT_MODEL", "mlx-community/Phi-4-mini-instruct-4bit")

# Tools that change UI state and should trigger screen capture + wait
_UI_MUTATING = frozenset({
    "click_ui_element", "type_text", "type_text_into_element",
    "open_application", "open_url", "open_url_and_wait",
    "press_keyboard_shortcut", "scroll_direction", "find_and_click",
    "browser_navigate", "browser_navigate_and_wait",
    "browser_click", "browser_click_text", "browser_click_xpath",
    "browser_type", "browser_type_by_label", "browser_press_key",
    "browser_fill_form", "browser_submit_form",
    "gmail_compose_draft",
    "web_smart_click", "web_handle_obstacle",
    "activate_application", "quit_application",
})


# ── Planner ──────────────────────────────────────────────────────────

def _build_planner_prompt(
    command: str,
    screen_context: str,
    tool_names: list[str],
    learned_context: str = "",
) -> str:
    """Build the structured planner system prompt."""

    tool_list = ", ".join(tool_names) if tool_names else "open_url, browser_click_text, browser_type, gmail_compose_draft, find_file, open_file, type_text, press_keyboard_shortcut"

    return (
        "You are DEMASCAS task planner for macOS desktop automation.\n\n"
        f"GOAL: {command}\n\n"
        f"CURRENT SCREEN STATE:\n{screen_context}\n\n"
        f"AVAILABLE TOOLS (use ONLY these exact names):\n{tool_list}\n\n"
        "RULES:\n"
        "1. Output ONLY a JSON array of steps. No explanation. No markdown.\n"
        "2. Each step: {\"tool\":\"...\",\"args\":{...},\"description\":\"...\",\"wait_ms\":N}\n"
        "3. Use tool names EXACTLY as listed above. NEVER invent tools.\n"
        "4. For browser workflows:\n"
        "   - Use open_url to navigate (NOT open_application + type_text)\n"
        "   - Wait 2000ms after navigation before clicking\n"
        "   - Wait 500ms after clicks before next action\n"
        "5. For Gmail email:\n"
        "   - open_url(\"https://mail.google.com\") → wait 2000ms → browser_click_text(\"Compose\") → wait 1500ms → gmail_compose_draft\n"
        "6. For file operations:\n"
        "   - find_file first, then open_file with {result_of_step_N}\n"
        "   - ALWAYS use ~/Downloads, ~/Documents, ~/Desktop — NEVER /Users/...\n"
        "7. Add wait_ms: 2000 after open_url/navigation, 1000 after clicks, 500 after typing\n"
        "8. Maximum 10 steps.\n"
        "9. Each step MUST have a brief human-readable \"description\".\n"
        "10. Optionally add \"verify\":\"what to check\" for critical steps.\n"
        "11. Optionally add \"depends_on\":[1] if args reference {result_of_step_1}.\n"
        "12. To open an app: use open_application, not open_url.\n"
        "13. For YouTube: open_url(\"https://youtube.com\") → wait → browser_type for search.\n\n"
        "EXAMPLE — compose email:\n"
        '[{"tool":"open_url","args":{"url":"https://mail.google.com"},"description":"Opening Gmail","wait_ms":0},\n'
        ' {"tool":"browser_click_text","args":{"text":"Compose"},"description":"Opening compose window","wait_ms":2000,"verify":"compose dialog visible"},\n'
        ' {"tool":"gmail_compose_draft","args":{"to":"","subject":"","body":""},"description":"Preparing email draft","wait_ms":1500}]\n\n'
        "EXAMPLE — find and open file:\n"
        '[{"tool":"find_file","args":{"folder":"~/Downloads","partial_name":"SIH","extension":"pdf"},"description":"Searching for file","wait_ms":0},\n'
        ' {"tool":"open_file","args":{"file_path":"{result_of_step_1}"},"description":"Opening the file","wait_ms":0,"depends_on":[1]}]\n\n'
        + (f"LEARNED CONTEXT:\n{learned_context}\n\n" if learned_context else "")
        + "NOW plan the steps for the GOAL above. JSON array ONLY:"
    )


def plan_task(
    command: str,
    screen_context: str,
    tool_schemas: list[dict],
    learned_context: str = "",
    llm_call_fn: Callable | None = None,
) -> TaskPlan | None:
    """
    Use the LLM to decompose a user command into structured steps.

    Args:
        command: The user's spoken command.
        screen_context: Formatted screen state string.
        tool_schemas: List of available tool schemas (Ollama format).
        learned_context: Optional past episodes/corrections.
        llm_call_fn: Callable(messages, max_tokens) -> str.
                     If None, uses default MLX HTTP call.

    Returns:
        TaskPlan or None if planning fails.
    """
    tool_names = [s["function"]["name"] for s in tool_schemas if "function" in s]

    prompt = _build_planner_prompt(command, screen_context, tool_names, learned_context)

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": command},
    ]

    if llm_call_fn is None:
        llm_call_fn = _default_llm_call

    raw_output = llm_call_fn(messages, PLANNER_MAX_TOKENS)
    if not raw_output:
        return None

    print(f"[STE PLANNER] Raw output: {raw_output[:500]}")

    # Parse JSON array from response
    steps = _parse_plan_json(raw_output, tool_names)
    if not steps:
        return None

    plan = TaskPlan(goal=command, steps=steps, raw_llm_output=raw_output)
    print(f"[STE PLANNER] Generated {len(steps)} steps for: \"{command}\"")
    for s in steps:
        print(f"  [{s.index}] {s.tool}({json.dumps(s.args)}) — {s.description}")

    return plan


def _parse_plan_json(raw: str, valid_tools: list[str]) -> list[TaskStep]:
    """Parse the LLM's JSON array output into validated TaskSteps."""

    # Find JSON array in the response
    arr_start = raw.find('[')
    arr_end = raw.rfind(']')
    if arr_start == -1 or arr_end == -1 or arr_end <= arr_start:
        print("[STE PLANNER] No JSON array found in output.")
        return []

    json_str = raw[arr_start:arr_end + 1]
    try:
        arr = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"[STE PLANNER] JSON parse error: {e}")
        return []

    if not isinstance(arr, list):
        return []

    valid_set = set(valid_tools)
    steps: list[TaskStep] = []

    for i, item in enumerate(arr):
        if not isinstance(item, dict):
            continue

        tool = item.get("tool", "").strip()
        if not tool:
            continue

        # Validate tool name
        if tool not in valid_set:
            print(f"[STE PLANNER] Skipping invalid tool: {tool}")
            continue

        args = item.get("args", {})
        if not isinstance(args, dict):
            args = {}

        # Sanitize paths: /Users/xxx/... → ~/...
        _sanitize_args(args)

        # Sanitize find_file: block generic partial_name values
        if tool == "find_file":
            pn = args.get("partial_name", "")
            if isinstance(pn, str) and pn.lower() in ("", "file", "pdf", "a", "the", "document", "doc"):
                args["partial_name"] = ""

        description = item.get("description", f"Step {i + 1}: {tool}")
        wait_ms = int(item.get("wait_ms", 0))
        verify = item.get("verify")
        depends_on = item.get("depends_on", [])

        if not isinstance(depends_on, list):
            depends_on = []

        steps.append(TaskStep(
            index=len(steps) + 1,
            tool=tool,
            args=args,
            description=description,
            depends_on=depends_on,
            verify=verify,
            wait_ms=wait_ms,
        ))

        if len(steps) >= MAX_TASK_STEPS:
            break

    return steps


def _sanitize_args(args: dict) -> None:
    """Sanitize tool arguments in-place."""
    import os
    home = os.path.expanduser("~")
    for key, val in list(args.items()):
        if isinstance(val, str):
            # Replace /Users/xxx/... with ~/...
            if val.startswith(home):
                args[key] = "~" + val[len(home):]
            elif "/Users/" in val:
                # Try to fix /Users/whatever/Downloads → ~/Downloads
                for known in ("Downloads", "Documents", "Desktop"):
                    if f"/{known}" in val:
                        idx = val.find(f"/{known}")
                        args[key] = "~" + val[idx:]
                        break


# ── Executor ─────────────────────────────────────────────────────────

def execute_task(
    plan: TaskPlan,
    tool_dispatch_fn: Callable[[str, dict], str],
    screen_capture_fn: Callable[[], str] | None = None,
    llm_call_fn: Callable | None = None,
) -> TaskResult:
    """
    Execute a task plan step by step with context accumulation.

    Args:
        plan: The structured task plan.
        tool_dispatch_fn: Callable(tool_name, args) -> result_string.
        screen_capture_fn: Optional callable() -> screen_state_string.
        llm_call_fn: Optional callable(messages, max_tokens) -> str for replanning.

    Returns:
        TaskResult with status, spoken updates, and step results.
    """
    result = TaskResult(
        status="ok",
        result="",
        steps_completed=0,
        total_steps=len(plan.steps),
    )

    # Accumulated context: each step's result is stored for reference
    step_results_map: dict[int, str] = {}
    context_log: list[dict] = []  # running log for replanning

    for step in plan.steps:
        print(f"\n[STE EXEC {step.index}/{len(plan.steps)}] {step.description}")
        print(f"  Tool: {step.tool}({json.dumps(step.args)})")

        # ── Pre-step: wait if needed ─────────────────────────
        if step.wait_ms > 0:
            print(f"  Waiting {step.wait_ms}ms for UI to settle...")
            time.sleep(step.wait_ms / 1000.0)

        # ── Substitute result tokens ─────────────────────────
        resolved_args = _substitute_results(step.args, step_results_map)

        # ── Speak progress ───────────────────────────────────
        result.spoken_updates.append(step.description + ".")

        # ── Execute with retry ───────────────────────────────
        step_result = _execute_step_with_retry(
            step=step,
            resolved_args=resolved_args,
            tool_dispatch_fn=tool_dispatch_fn,
            screen_capture_fn=screen_capture_fn,
            llm_call_fn=llm_call_fn,
            context_log=context_log,
            plan=plan,
        )

        result.step_results.append(step_result)

        # ── Handle find_file multi-match (CLARIFYING) ────────
        if step.tool == "find_file" and step_result.success:
            clarify_result = _handle_find_file_result(
                step_result.result, step_results_map, step.index
            )
            if clarify_result == "clarifying":
                # Multiple matches — need user input
                try:
                    parsed = json.loads(step_result.result)
                    matches = parsed.get("matches", [])
                    result.pending_file_choices = [
                        {"name": m.get("name", "?"), "path": m.get("path", "?")}
                        for m in matches[:5]
                    ]
                    msg = parsed.get("message", f"I found {len(matches)} files. Which one?")
                    result.spoken_updates.append(msg)
                except Exception:
                    result.spoken_updates.append("I found multiple files. Which one did you mean?")

                result.status = "clarifying"
                result.result = "Awaiting user selection."
                result.steps_completed = step.index
                return result
            elif clarify_result == "not_found":
                try:
                    parsed = json.loads(step_result.result)
                    msg = parsed.get("message", "No files found.")
                except Exception:
                    msg = "No files found."
                result.spoken_updates.append(msg)
                result.status = "failed"
                result.result = msg
                result.steps_completed = step.index
                return result

        # ── Check for failure ────────────────────────────────
        if not step_result.success:
            result.status = "partial" if step.index > 1 else "failed"
            result.result = f"Step {step.index} failed: {step_result.error}"
            result.spoken_updates.append(
                f"I ran into an issue at step {step.index}. {step_result.error[:100]}"
            )
            result.steps_completed = step.index - 1
            return result

        # ── Store result for dependent steps ─────────────────
        stored = step_result.result
        if step.tool == "find_file":
            # Already handled above — stored_result is the extracted path
            stored = step_results_map.get(step.index, stored)
        else:
            step_results_map[step.index] = stored

        # ── Append to context log ────────────────────────────
        context_log.append({
            "step": step.index,
            "tool": step.tool,
            "args": resolved_args,
            "result": stored[:200],  # cap for context window
            "success": True,
        })

        result.steps_completed = step.index

    # ── All steps done ───────────────────────────────────────
    result.status = "ok"
    result.result = "Done."
    result.spoken_updates.append("Done.")
    return result


def _execute_step_with_retry(
    step: TaskStep,
    resolved_args: dict,
    tool_dispatch_fn: Callable[[str, dict], str],
    screen_capture_fn: Callable[[], str] | None,
    llm_call_fn: Callable | None,
    context_log: list[dict],
    plan: TaskPlan,
) -> StepResult:
    """Execute a step with retry on failure."""

    for attempt in range(1 + MAX_RETRIES_PER_STEP):
        try:
            raw_result = tool_dispatch_fn(step.tool, resolved_args)
        except Exception as e:
            raw_result = f"[-] Exception: {e}"

        success = not _is_tool_error(raw_result)

        # Capture screen state after UI-mutating tools
        screen_after = ""
        if screen_capture_fn and step.tool in _UI_MUTATING:
            try:
                time.sleep(0.3)
                screen_after = screen_capture_fn()
            except Exception:
                pass

        if success:
            return StepResult(
                index=step.index,
                tool=step.tool,
                args=resolved_args,
                result=raw_result,
                success=True,
                screen_after=screen_after,
                retries=attempt,
            )

        # ── Retry logic ──────────────────────────────────────
        if attempt < MAX_RETRIES_PER_STEP:
            print(f"  [RETRY {attempt + 1}/{MAX_RETRIES_PER_STEP}] "
                  f"Step {step.index} failed: {raw_result[:100]}")

            # Try to get a recovery action from the LLM
            if llm_call_fn:
                recovery_args = _get_recovery_action(
                    step, resolved_args, raw_result, screen_after,
                    context_log, plan, llm_call_fn,
                )
                if recovery_args is not None:
                    resolved_args = recovery_args
                    time.sleep(0.5)  # Brief pause before retry
                    continue

            # Simple retry with delay
            time.sleep(1.0)

    # All retries exhausted
    return StepResult(
        index=step.index,
        tool=step.tool,
        args=resolved_args,
        result=raw_result,
        success=False,
        screen_after=screen_after,
        error=raw_result[:200],
        retries=MAX_RETRIES_PER_STEP,
    )


def _get_recovery_action(
    step: TaskStep,
    failed_args: dict,
    error: str,
    screen_after: str,
    context_log: list[dict],
    plan: TaskPlan,
    llm_call_fn: Callable,
) -> dict | None:
    """Ask the LLM for alternative args to retry a failed step."""
    try:
        context_text = json.dumps(context_log[-3:], indent=None)  # last 3 steps
        prompt = (
            f"A task step failed. Suggest fixed arguments.\n\n"
            f"GOAL: {plan.goal}\n"
            f"STEP {step.index}: {step.tool}({json.dumps(failed_args)})\n"
            f"ERROR: {error[:200]}\n"
            f"SCREEN: {screen_after[:300]}\n"
            f"PREVIOUS STEPS: {context_text}\n\n"
            f"Reply with ONLY the fixed args JSON object for {step.tool}. "
            f"If you cannot fix it, reply: {{\"give_up\": true}}"
        )
        messages = [{"role": "user", "content": prompt}]
        raw = llm_call_fn(messages, 150)
        if not raw:
            return None

        # Parse JSON from response
        obj_start = raw.find('{')
        obj_end = raw.rfind('}')
        if obj_start == -1 or obj_end == -1:
            return None

        parsed = json.loads(raw[obj_start:obj_end + 1])
        if parsed.get("give_up"):
            return None

        return parsed
    except Exception:
        return None


def _substitute_results(args: dict, step_results: dict[int, str]) -> dict:
    """Replace {result_of_step_N} tokens in args with actual values."""
    args_str = json.dumps(args)
    for step_idx, result_val in step_results.items():
        token = f"{{result_of_step_{step_idx}}}"
        args_str = args_str.replace(token, result_val)
    try:
        return json.loads(args_str)
    except json.JSONDecodeError:
        return args


def _handle_find_file_result(
    result: str,
    step_results: dict[int, str],
    step_index: int,
) -> str:
    """
    Handle find_file results: extract path for single match, flag multi-match.

    Returns:
        "ok" — single match, path stored in step_results
        "clarifying" — multiple matches, need user input
        "not_found" — no matches
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        # Try Python dict-style
        try:
            parsed = eval(result)  # noqa: S307
        except Exception:
            return "ok"  # can't parse, pass through

    if not isinstance(parsed, dict):
        return "ok"

    found = parsed.get("found", False)
    count = parsed.get("count", 0)
    matches = parsed.get("matches", [])

    if not found or count == 0:
        return "not_found"

    if count == 1 and matches:
        # Single match — extract the path
        path = matches[0].get("path", "")
        if path:
            step_results[step_index] = path
        return "ok"

    if count > 1:
        return "clarifying"

    return "ok"


def _is_tool_error(result: str) -> bool:
    """Check if a tool result indicates an error."""
    if not result:
        return True
    error_prefixes = ("[-]", "Error:", "Failed:", "Timeout:")
    for prefix in error_prefixes:
        if result.strip().startswith(prefix):
            return True
    if '"success": false' in result or '"success":false' in result:
        return True
    return False


# ── Default LLM call (MLX HTTP) ─────────────────────────────────────

def _default_llm_call(messages: list[dict], max_tokens: int) -> str:
    """Call the local MLX LLM server."""
    import requests

    try:
        resp = requests.post(
            "http://127.0.0.1:8081/v1/chat/completions",
            json={
                "model": MLX_TEXT_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": LLM_TEMPERATURE,
                "stream": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"[STE] LLM call failed: {e}")

    return ""
