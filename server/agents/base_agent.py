"""
base_agent.py - AgentBase class for DEMASCAS domain agents.

Design principles:
- One shared Qwen 3B (MLX) - agents take turns, never run in parallel
- Goal + step memory lives in the agent, NOT in the LLM context window
- Each LLM call gets a fresh, minimal context reconstructed from agent state
- Silent execution with a final summary spoken by the daemon
- Max 20 steps per task (vs 5 in the old ReAct loop)
- Hard error budget: 4 consecutive failures -> abort with explanation
"""

import json
import ast
import time
import logging
import re
import requests
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)

from core.config import LLM_CONFIGURED, LLM_MODEL, LLM_CHAT_URL, TOOL_SERVER_URL as TOOL_SERVER

MAX_STEPS = 20
MAX_ERRORS = 4
LLM_TIMEOUT = 30
TOOL_TIMEOUT = 30
# Wall-clock budget for one agent run.  The C++ daemon waits up to
# AGENT_TIMEOUT_SEC (config.hpp, 100 s) for /agent; the agent must stop
# BEFORE that, otherwise it keeps clicking/typing after the daemon has
# already told the user it failed and moved on.
AGENT_TIME_BUDGET = 80
TOOL_PREDICT = 256
LLM_TEMPERATURE = 0.1
HISTORY_WINDOW = 6

ERROR_PREFIXES = (
    "error:", "failed:", "timeout:", "[-]", "exception", "traceback",
    "could not", "can't get", "execution error",
)

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class StepRecord:
    step_num: int
    tool: str
    args: dict
    result: str
    success: bool
    timestamp: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        """Backward-compatible alias for success used by some agents."""
        return self.success

    def compact(self) -> str:
        status = "OK" if self.success else "FAIL"
        result_preview = self.result[:120].replace("\n", " ") if self.result else ""
        short_args = {
            k: (v[:40] + "..." if isinstance(v, str) and len(v) > 40 else v)
            for k, v in self.args.items()
        }
        return f"[{self.step_num}] {self.tool}({json.dumps(short_args)}) -> {status}: {result_preview}"


@dataclass
class AgentResult:
    success: bool
    summary: str
    steps_taken: int
    tool_calls: list[str]
    error: Optional[str] = None
    data: Optional[dict] = None


class AgentBase(ABC):
    domain_name: str = "base"
    tool_names: list[str] = []
    NAV_TOOLS = {
        "browser_navigate_and_wait",
        "browser_navigate",
        "open_url_and_wait",
        "open_url",
        "browser_reload",
        "browser_back",
    }

    def __init__(self):
        self._goal: str = ""
        self._steps: list[StepRecord] = []
        self._step_count: int = 0
        self._error_streak: int = 0
        self._recovery_attempted: bool = False
        self._start_time: float = 0.0
        self._tool_schemas: Optional[list] = None
        self._active_context: dict[str, Any] = {}

    @abstractmethod
    def run(self, goal: str, context: dict) -> AgentResult:
        ...

    def _execute(self, goal: str, context: dict) -> AgentResult:
        """
        Core ReAct step loop. Subclasses call this from run().
        """
        self._goal = goal
        self._steps = []
        self._step_count = 0
        self._error_streak = 0
        self._recovery_attempted = False
        self._start_time = time.time()
        self._active_context = context if isinstance(context, dict) else {}
        tool_calls_made: list[str] = []

        if not LLM_CONFIGURED:
            return AgentResult(
                success=False,
                summary="I need a language model for that, and none is set up yet. "
                        "Run python main.py setup to plug one in.",
                steps_taken=0,
                tool_calls=[],
                error="llm_not_configured",
            )

        self._tool_schemas = self._fetch_tool_schemas()
        if not self._tool_schemas:
            return AgentResult(
                success=False,
                summary="I couldn't reach the tool server. Please check that it's running.",
                steps_taken=0,
                tool_calls=[],
                error="tool_server_unavailable",
            )

        logger.info("[🤖 AGENT:%s] goal='%s'", self.domain_name.upper(), goal[:60])

        for step_num in range(1, MAX_STEPS + 1):
            self._step_count = step_num
            if self._time_left() <= 5:
                return self._abort("Ran out of time before finishing.")
            history_block = self._build_history_block()
            context_block = self._build_context_block()

            action = self._call_llm(goal, history_block, context_block)
            if action is None:
                self._error_streak += 1
                if self._error_streak >= MAX_ERRORS:
                    recovered = self._attempt_recovery(goal, "LLM stopped producing valid actions.")
                    if recovered is not None:
                        return recovered
                    return self._abort("LLM stopped producing valid actions.")
                continue

            if action.get("done"):
                summary = action.get("say", "Done.")
                print(f"[DONE] {self.domain_name} step={step_num} summary={summary}")
                logger.info("[🤖 DONE] %s step=%d summary='%s'", self.domain_name, step_num, summary[:60])
                return AgentResult(
                    success=True,
                    summary=summary,
                    steps_taken=step_num,
                    tool_calls=tool_calls_made,
                )

            tool_name = action.get("tool", "")
            tool_args = action.get("args", {})
            if not isinstance(tool_args, dict):
                tool_args = {}

            if not tool_name:
                self._error_streak += 1
                continue

            if tool_name not in self.tool_names:
                self._record_step(
                    step_num,
                    tool_name,
                    tool_args,
                    f"Tool '{tool_name}' not available to {self.domain_name} agent.",
                    success=False,
                )
                self._error_streak += 1
                if self._error_streak >= MAX_ERRORS:
                    recovered = self._attempt_recovery(goal, "Repeated out-of-scope tool calls.")
                    if recovered is not None:
                        return recovered
                    return self._abort("Repeated out-of-scope tool calls.")
                continue

            if self._is_duplicate(tool_name, tool_args):
                self._record_step(step_num, tool_name, tool_args, "Duplicate call blocked.", success=False)
                self._error_streak += 1
                if self._error_streak >= MAX_ERRORS:
                    recovered = self._attempt_recovery(goal, "Agent stuck in a loop.")
                    if recovered is not None:
                        return recovered
                    return self._abort("Agent stuck in a loop.")
                continue

            print(f"[STEP] {self.domain_name} #{step_num} {tool_name}({json.dumps(tool_args)[:120]})")
            logger.info("[🤖 STEP %d] %s(%s)", step_num, tool_name, json.dumps(tool_args)[:80])
            result = self._call_tool(tool_name, tool_args)

            # A destructive tool asked for the user's spoken approval.  Stop
            # here: the daemon asks yes/no and the server runs the action only
            # if approved.  (Continuing could make the model claim it's done.)
            pending = self._pending_confirmation(result)
            if pending is not None:
                tool_calls_made.append(tool_name)
                return AgentResult(
                    success=True,
                    summary=pending.get("confirm_speech", "Should I go ahead?"),
                    steps_taken=step_num,
                    tool_calls=tool_calls_made,
                    data={"awaiting_confirmation": pending.get("pending_tool", tool_name)},
                )

            is_error = self._is_tool_error(result)
            status = "✅" if not is_error else "❌"
            print(f"[RESULT] {self.domain_name} #{step_num} {tool_name} {status} {result[:180].replace(chr(10), ' ')}")
            logger.info("[🤖 RESULT] %s %s", status, result[:100].replace("\n", " "))
            tool_calls_made.append(tool_name)
            record = self._record_step(step_num, tool_name, tool_args, result, success=not is_error)
            self._on_step(record)
            self._on_step_complete(record)

            if is_error:
                self._error_streak += 1
                if self._error_streak >= MAX_ERRORS:
                    reason = f"Too many tool failures. Last: {result[:120]}"
                    recovered = self._attempt_recovery(goal, reason)
                    if recovered is not None:
                        return recovered
                    return self._abort(reason)
            else:
                self._error_streak = 0

        return self._abort(f"Task exceeded {MAX_STEPS} steps without completion.")

    def _call_llm(self, goal: str, history_block: str, context_block: str) -> Optional[dict]:
        """
        Build prompt and request one JSON action from the shared MLX endpoint.
        """
        now_ist = datetime.now(IST)
        now_str = now_ist.strftime("%Y-%m-%d %H:%M %Z")
        tools_block = self._format_tool_schemas()

        rules = [
            "1. You are a domain-specific executor. Return JSON only.",
            "2. Output exactly one object: either {'tool':'name','args':{...}} or {'done': true, 'say': '...'}.",
            "3. Never include markdown fences, commentary, or multiple actions.",
            "4. Use only listed tools. If unavailable, finish with done and explain briefly.",
            "5. Use tilde paths (~/Downloads etc.) on macOS; avoid hardcoded /Users paths.",
            "6. Keep each step minimal and deterministic.",
            "7. If blocked by login/paywall/permission, stop with done and ask user action.",
            "8. On completion, return a short TTS-friendly summary.",
        ]

        default_rules = self._rules().strip()
        extra = self._extra_rules().strip()
        rules_block = "\n".join(rules)
        if default_rules:
            rules_block = f"{rules_block}\n{default_rules}"
        if extra:
            rules_block = f"{rules_block}\n{extra}"

        # Order matters for speed: the MLX server reuses its KV cache for the
        # longest shared prompt PREFIX.  Stable text (role, tools, rules) goes
        # first; things that change every step (history, context, clock) go
        # last.  The clock used to sit near the top WITH SECONDS, so every
        # step re-processed the entire prompt.
        system_prompt = (
            f"{self.system_prompt}\n\n"
            f"Available tools (scoped):\n{tools_block}\n\n"
            f"Execution rules:\n{rules_block}\n\n"
            f"Recent steps (last 6):\n{history_block}\n\n"
            f"Active context:\n{context_block}\n\n"
            f"Current IST time: {now_str}"
        )

        payload = {
            "model": LLM_MODEL,
            "stream": False,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": TOOL_PREDICT,
            "messages": [
                {"role": "system", "content": system_prompt},
                # The final line matters: without it the model writes a paragraph
                # before the JSON (measured: 2.93 s → 0.85 s per step on Qwen2.5-3B).
                {"role": "user", "content": f"Complete this task: {goal}\n"
                                            "Reply with ONLY the JSON object — no explanation, no markdown."},
            ],
        }

        try:
            resp = requests.post(LLM_CHAT_URL, json=payload,
                                 timeout=max(3.0, min(LLM_TIMEOUT, self._time_left())))
            if resp.status_code != 200:
                logger.warning("Agent LLM call failed: %s %s", resp.status_code, resp.text[:200])
                return None
            body = resp.json()
            raw = ""
            choices = body.get("choices", [])
            if choices:
                raw = choices[0].get("message", {}).get("content", "")
            if not raw:
                return None
            return self._parse_action(raw)
        except Exception as e:
            logger.exception("Agent LLM exception: %s", e)
            return None

    def _parse_action(self, raw: str) -> Optional[dict]:
        """
        Strip markdown fences, parse direct JSON first, then scan for balanced objects.
        """
        if not raw:
            return None

        def _parse_candidate(candidate: str) -> Optional[dict]:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and ("tool" in obj or "done" in obj):
                    return obj
            except Exception:
                pass
            try:
                obj = ast.literal_eval(candidate)
                if isinstance(obj, dict) and ("tool" in obj or "done" in obj):
                    return obj
            except Exception:
                pass
            return None

        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2:
                lines = lines[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                text = "\n".join(lines).strip()

        parsed = _parse_candidate(text)
        if parsed is not None:
            return parsed

        depth = 0
        start = -1
        for idx, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = idx
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        candidate = text[start:idx + 1]
                        parsed = _parse_candidate(candidate)
                        if parsed is not None:
                            return parsed

        return None

    def _call_tool(self, name: str, args: dict) -> str:
        """POST {name, arguments: args} to TOOL_SERVER/tool. Return result string."""
        try:
            payload = {"name": name, "arguments": args}
            resp = requests.post(f"{TOOL_SERVER}/tool", json=payload,
                                 timeout=max(3.0, min(TOOL_TIMEOUT, self._time_left())))
            if resp.status_code != 200:
                return f"error: tool server HTTP {resp.status_code}"
            body = resp.json()
            if "error" in body:
                return f"error: {body.get('error', 'unknown error')}"
            return str(body.get("result", ""))
        except requests.Timeout:
            return "timeout: tool call exceeded limit"
        except Exception as e:
            return f"exception: {e}"

    def _run_tool(self, name: str, args: dict) -> str:
        """Backward-compatible alias used by some domain agents."""
        return self._call_tool(name, args)

    def _time_left(self) -> float:
        """Seconds remaining in this run's wall-clock budget."""
        if not self._start_time:
            return float(AGENT_TIME_BUDGET)
        return AGENT_TIME_BUDGET - (time.time() - self._start_time)

    def _fetch_tool_schemas(self) -> Optional[list]:
        """
        GET /tools and keep only this agent's whitelisted tools, so the LLM
        sees each tool's real description and parameter names.
        (Previously this asked the keyword router about the domain name
        — e.g. "comms" — which matched nothing and fell back to arg-less
        stubs, so the model had to guess argument names.)
        """
        try:
            resp = requests.get(f"{TOOL_SERVER}/tools", timeout=TOOL_TIMEOUT)
            if resp.status_code != 200:
                return None
            schemas = resp.json()
            if not isinstance(schemas, list):
                return None
            allowed = set(self.tool_names)
            filtered: list[dict[str, Any]] = []
            for item in schemas:
                fn = item.get("function", {}) if isinstance(item, dict) else {}
                name = fn.get("name")
                if name in allowed:
                    filtered.append(item)
            if not filtered:
                # Fallback to static whitelist names if router returns none.
                for name in self.tool_names:
                    filtered.append({
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": "Domain-scoped tool",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    })
            return filtered
        except Exception as e:
            logger.exception("Failed to fetch tool schemas: %s", e)
            return None

    def _format_tool_schemas(self) -> str:
        """Compressed format: show only required params and short description."""
        if not self._tool_schemas:
            return "(no tools available)"

        lines: list[str] = []
        for item in self._tool_schemas:
            fn = item.get("function", {}) if isinstance(item, dict) else {}
            name = fn.get("name", "unknown")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            required = params.get("required", []) if isinstance(params, dict) else []

            pparts: list[str] = []
            if isinstance(props, dict):
                # Required params first, then optional ones (marked "?") —
                # tools like gmail_compose_draft have only optional params.
                ordered = list(required) + [n for n in props if n not in required]
                for p_name in ordered:
                    p_meta = props.get(p_name, {})
                    p_type = p_meta.get("type", "any") if isinstance(p_meta, dict) else "any"
                    opt = "" if p_name in required else "?"
                    pparts.append(f"{p_name}{opt}: {p_type}")

            ptext = ", ".join(pparts) if pparts else "no args"
            lines.append(f"- {name}({ptext}) - {desc.splitlines()[0][:80]}".strip())

        return "\n".join(lines)

    def _record_step(self, step_num, tool, args, result, success) -> StepRecord:
        record = StepRecord(step_num, tool, args, result, success)
        self._steps.append(record)
        return record

    def _build_history_block(self) -> str:
        """Return last 6 steps as compact summaries."""
        if not self._steps:
            return "(none)"
        return "\n".join(s.compact() for s in self._steps[-HISTORY_WINDOW:])

    def _build_context_block(self) -> str:
        """Return compact context fields for the current task."""
        ctx = self._active_context if isinstance(self._active_context, dict) else {}
        active_app = str(ctx.get("active_app", ""))
        active_url = str(ctx.get("active_url", ""))
        active_file = str(ctx.get("active_file", ""))
        last_result = str(ctx.get("last_result", ""))[:300]
        prev = str(ctx.get("last_command", ""))[:200]
        prev_reply = str(ctx.get("last_reply", ""))[:200]
        return (
            # Lets follow-ups like "show that on another site" resolve "that".
            f"previous_request: {prev or '(none)'}\n"
            f"previous_reply: {prev_reply or '(none)'}\n"
            f"active_app: {active_app or '(unknown)'}\n"
            f"active_url: {active_url or '(unknown)'}\n"
            f"active_file: {active_file or '(unknown)'}\n"
            f"last_tool_result: {last_result or '(none)'}"
        )

    def _is_duplicate(self, tool: str, args: dict) -> bool:
        """
        Prevent loops with two strategies:

        1. NAV-LOCK: Once any nav tool succeeds, block ALL nav tools
           for the rest of the task. Navigation is one-shot.
           The LLM must move to reading/interacting after nav succeeds.

        2. WIDE WINDOW: For non-nav tools, check last 4 steps (not 2)
           for exact tool+args match. Catches the LLM inserting filler
           calls between repeated identical calls.
        """
        if tool in self.NAV_TOOLS:
            # Block if ANY nav tool succeeded earlier in this task
            for s in self._steps:
                if s.tool in self.NAV_TOOLS and s.ok:
                    return True  # nav already succeeded - lock it out
        else:
            # Non-nav: check last 4 steps for exact match
            for s in self._steps[-4:]:
                if s.tool == tool and s.args == args:
                    return True
        return False

    @property
    def _nav_succeeded(self) -> bool:
        """True once any navigation tool has already succeeded in this task."""
        for s in self._steps:
            if s.tool in self.NAV_TOOLS and s.success:
                return True
        return False

    def _is_dup(self, tool: str, args: dict) -> bool:
        """Backward-compatible alias used in older prompts/tests."""
        return self._is_duplicate(tool, args)

    def _is_tool_error(self, result: str) -> bool:
        """True if a tool result represents a failure.

        Handles plain strings ("[-] ...", "Failed: ...") and JSON results
        like {"success": false, ...} returned by browser/file tools."""
        if not result:
            return True
        text = str(result).lstrip()
        if text.lower().startswith(ERROR_PREFIXES):
            return True
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except Exception:
                return False
            if isinstance(obj, dict):
                if obj.get("success") is False or obj.get("error"):
                    return True
                msg = obj.get("message")
                if isinstance(msg, str) and msg.lstrip().lower().startswith(ERROR_PREFIXES):
                    return True
        return False

    @staticmethod
    def _pending_confirmation(result: str) -> Optional[dict]:
        """Return the pending-confirmation payload if `result` is one."""
        if not result or "pending_confirmation" not in result:
            return None
        try:
            obj = json.loads(result)
        except Exception:
            return None
        if isinstance(obj, dict) and obj.get("status") == "pending_confirmation":
            return obj
        return None

    def _is_error(self, result: str) -> bool:
        """Backward-compatible alias used by some domain agents."""
        return self._is_tool_error(result)

    def _abort(self, reason: str) -> AgentResult:
        logger.info("[🤖 ABORT] %s after %d steps: %s", self.domain_name, len(self._steps), reason[:60])
        # Internal reasons ("Agent stuck in a loop") stay in the log; the user
        # hears something useful.
        done = [s.tool for s in self._steps if s.ok]
        spoken = ("I got partway but couldn't finish that. Could you say it a bit differently?"
                  if done else "I couldn't do that one. Could you rephrase it?")
        return AgentResult(
            success=False,
            summary=spoken,
            steps_taken=len(self._steps),
            tool_calls=[s.tool for s in self._steps],
            error=reason,
        )

    def _attempt_recovery(self, goal: str, reason: str) -> Optional[AgentResult]:
        """
        Try one alternative strategy before aborting.
        Called when error_streak >= MAX_ERRORS.
        Returns AgentResult if recovery succeeds, None if it fails.
        """
        logger.info("[🤖 RECOVER] %s trying recovery after: %s", self.domain_name, reason[:60])

        failed_tools = [s.tool for s in self._steps if not s.ok]
        if not failed_tools:
            return None

        most_failed = max(set(failed_tools), key=failed_tools.count)

        recoveries = self._recovery_strategies()
        strategy = recoveries.get(most_failed)

        if not strategy:
            # (Previously a raw screen dump was written into the user's
            # long-term memory here, which then polluted later prompts.)
            return None

        alt_tool, alt_args = strategy
        if alt_tool not in self.tool_names:
            return None

        logger.info("[🤖 RECOVER] trying %s instead of %s", alt_tool, most_failed)
        r = self._run_tool(alt_tool, alt_args)

        if not self._is_error(r):
            if "save_memory" in self.tool_names:
                self._run_tool(
                    "save_memory",
                    {
                        "text": (
                            f"Recovery: in {self.domain_name}, when {most_failed} fails, "
                            f"use {alt_tool} instead. Goal pattern: '{goal[:50]}'"
                        )
                    },
                )
            return AgentResult(
                success=True,
                summary="Done.",
                steps_taken=len(self._steps) + 1,
                tool_calls=[s.tool for s in self._steps] + [alt_tool],
            )

        return None

    def _recovery_strategies(self) -> dict:
        """
        Map of failed_tool -> (alternative_tool, alternative_args).
        Subclasses override this to add domain-specific strategies.
        Base class provides cross-domain fallbacks.
        """
        return {
            "click_ui_element": ("find_and_click", {"search_text": self._failed_click_target()}),
            "browser_navigate_and_wait": ("open_url", {"url": self._last_nav_url()}),
            "browser_navigate": ("open_url", {"url": self._last_nav_url()}),
        }

    def _failed_click_target(self) -> str:
        """Element name from the most recent failed click, else the goal."""
        for s in reversed(self._steps):
            if s.tool == "click_ui_element" and not s.ok:
                name = s.args.get("element_name") or s.args.get("element")
                if name:
                    return str(name)
        return self._goal[:30]

    def _last_nav_url(self) -> str:
        """Extract URL from the last navigation attempt."""
        for s in reversed(self._steps):
            if "url" in s.args:
                return s.args["url"]
        return "about:blank"

    def _on_step(self, rec: StepRecord) -> None:
        """Compatibility hook for agents that implement _on_step(rec)."""
        pass

    def _on_step_complete(self, record: StepRecord) -> None:
        """Hook called after each tool execution. Override per domain."""
        pass

    def _rules(self) -> str:
        """Additional rules injected into system prompt. Override per domain."""
        return (
            "9. After ANY navigation succeeds (open_url_and_wait, browser_navigate_and_wait, "
            "open_url, browser_navigate), you MUST NOT call any navigation tool again. "
            "Move immediately to browser_get_dom_summary or browser_get_page_text."
        )

    def _extra_rules(self) -> str:
        """Additional numbered rules appended to system prompt. Override per domain."""
        return ""

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        ...
