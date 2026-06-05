"""
WorkflowAgent - record, save, replay, and manage multi-step workflows.

USER COMMANDS:
  "record a workflow called [name]"  -> RECORD mode
  "stop recording"                   -> save current recording
  "run the [name] workflow"          -> REPLAY mode
  "what workflows do you know"       -> LIST mode
  "add a step to [name] workflow"    -> EDIT mode (future)

WORKFLOW JSON FORMAT (saved via save_memory):
{
  "type": "workflow",
  "name": "workflow_name",
  "description": "what this does",
  "created_at": "ISO timestamp",
  "step_count": N,
  "steps": [
    {"step": 1, "tool": "open_application",
     "args": {"app_name": "Slack"}, "description": "Open Slack"},
    {"step": 2, "tool": "press_keyboard_shortcut",
     "args": {"keys": "cmd+n"}, "description": "New message"}
  ]
}

VARIABLE SUBSTITUTION in replay:
  {date}       -> today's date (YYYY-MM-DD)
  {time}       -> current time (HH:MM)
  {clipboard}  -> current clipboard contents
  {filename}   -> last active filename from context
"""

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .base_agent import AgentBase, AgentResult, StepRecord

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

RECORD_KEYWORDS = [
    "record a workflow",
    "record workflow",
    "create a workflow",
    "save this workflow",
    "start recording",
]
REPLAY_KEYWORDS = [
    "run the",
    "run workflow",
    "replay",
    "execute workflow",
    "do the",
    "play the",
]
LIST_KEYWORDS = [
    "what workflows",
    "list workflows",
    "show workflows",
    "what do you know how to do",
]

LIST_STUDENT_WORKFLOWS_KEYWORDS = [
    "list student workflows",
    "show student workflows",
    "what student workflows",
    "show built in workflows",
    "list built in workflows",
    "show builtin workflows",
]

STUDENT_PACK_KEYWORDS = [
    "student productivity pack",
    "student workflow pack",
    "install student pack",
    "set up student workflows",
]

STUDENT_RUN_PATTERNS = {
    "class_notes_capture": [
        "class notes",
        "capture notes",
        "lecture notes",
        "start notes session",
    ],
    "assignment_planner": [
        "assignment planner",
        "plan my assignments",
        "assignment schedule",
        "homework planner",
    ],
    "revision_timer": [
        "revision timer",
        "study timer",
        "pomodoro",
        "focus timer",
    ],
    "research_citation_capture": [
        "capture citation",
        "research citation",
        "save source link",
        "citation notes",
    ],
    "daily_student_startup": [
        "daily student startup",
        "start my study day",
        "study routine",
        "open my study routine",
        "start study routine",
        "student startup",
        "start school workflow",
        "start daily startup",
    ],
}

BUILTIN_STUDENT_WORKFLOWS = {
    "class_notes_capture": {
        "description": "Open Notes and start a dated class-note entry.",
        "steps": [
            {"step": 1, "tool": "open_application", "args": {"app_name": "Notes"}, "description": "Open Notes"},
            {"step": 2, "tool": "press_keyboard_shortcut", "args": {"keys": "command+n"}, "description": "Create a new note"},
            {"step": 3, "tool": "type_text", "args": {"text": "Class Notes - {date} {time}"}, "description": "Add note title"},
            {"step": 4, "tool": "press_keyboard_shortcut", "args": {"keys": "return"}, "description": "Move to next line"},
            {"step": 5, "tool": "type_text", "args": {"text": "Topic:\nKey points:\nQuestions:\nAction items:"}, "description": "Insert notes template"},
        ],
    },
    "assignment_planner": {
        "description": "Open Reminders and create a dated assignment checklist.",
        "steps": [
            {"step": 1, "tool": "open_application", "args": {"app_name": "Reminders"}, "description": "Open Reminders"},
            {"step": 2, "tool": "press_keyboard_shortcut", "args": {"keys": "command+n"}, "description": "Create new reminder"},
            {"step": 3, "tool": "type_text", "args": {"text": "Assignments for {date} - add tasks and due dates"}, "description": "Create planner heading"},
            {"step": 4, "tool": "press_keyboard_shortcut", "args": {"keys": "return"}, "description": "Save reminder"},
        ],
    },
    "revision_timer": {
        "description": "Start a focused revision timer in Clock and set up a study cycle.",
        "steps": [
            {"step": 1, "tool": "open_application", "args": {"app_name": "Clock"}, "description": "Open Clock app"},
            {"step": 2, "tool": "press_keyboard_shortcut", "args": {"keys": "command+2"}, "description": "Switch to Timer tab"},
            {"step": 3, "tool": "type_text", "args": {"text": "50:00"}, "description": "Set timer to 50 minutes"},
            {"step": 4, "tool": "press_keyboard_shortcut", "args": {"keys": "return"}, "description": "Start revision timer"},
        ],
    },
    "research_citation_capture": {
        "description": "Capture source URL from browser into Notes citation log.",
        "steps": [
            {"step": 1, "tool": "open_application", "args": {"app_name": "Brave Browser"}, "description": "Focus Brave Browser"},
            {"step": 2, "tool": "press_keyboard_shortcut", "args": {"keys": "command+l"}, "description": "Focus address bar"},
            {"step": 3, "tool": "press_keyboard_shortcut", "args": {"keys": "command+c"}, "description": "Copy current URL"},
            {"step": 4, "tool": "open_application", "args": {"app_name": "Notes"}, "description": "Open Notes"},
            {"step": 5, "tool": "press_keyboard_shortcut", "args": {"keys": "command+n"}, "description": "Create citation note"},
            {"step": 6, "tool": "type_text", "args": {"text": "Citation {date} {time}: {clipboard}"}, "description": "Paste citation line"},
        ],
    },
    "daily_student_startup": {
        "description": "Open daily study essentials: Calendar, Bennett LMS, Notes, and a focus timer.",
        "steps": [
            {"step": 1, "tool": "open_application", "args": {"app_name": "Calendar"}, "description": "Open Calendar"},
            {"step": 2, "tool": "open_url_and_wait", "args": {"url": "https://lms.bennett.edu.in", "browser": "Brave Browser", "wait_seconds": 3}, "description": "Open Bennett LMS in Brave"},
            {"step": 3, "tool": "open_application", "args": {"app_name": "Notes"}, "description": "Open Notes"},
            {"step": 4, "tool": "press_keyboard_shortcut", "args": {"keys": "command+n"}, "description": "Create startup note"},
            {"step": 5, "tool": "type_text", "args": {"text": "Study Plan {date}:\n1) Classes\n2) Assignments\n3) Revision goals"}, "description": "Insert daily plan template"},
            {"step": 6, "tool": "open_application", "args": {"app_name": "Clock"}, "description": "Open Clock"},
            {"step": 7, "tool": "press_keyboard_shortcut", "args": {"keys": "command+2"}, "description": "Switch to Timer"},
            {"step": 8, "tool": "type_text", "args": {"text": "50:00"}, "description": "Set focus timer"},
            {"step": 9, "tool": "press_keyboard_shortcut", "args": {"keys": "return"}, "description": "Start timer"},
        ],
    },
}


class WorkflowAgent(AgentBase):
    domain_name = "workflow"
    tool_names = [
        "open_application",
        "activate_application",
        "quit_application",
        "press_keyboard_shortcut",
        "click_ui_element",
        "type_text",
        "type_text_into_element",
        "find_and_click",
        "scroll_direction",
        "open_url",
        "open_url_and_wait",
        "find_file",
        "open_file",
        "browser_navigate",
        "browser_click_text",
        "browser_type",
        "get_clipboard",
        "set_clipboard",
        "save_memory",
        "retrieve_memories",
    ]

    @property
    def system_prompt(self) -> str:
        return """You are DEMASCAS's Workflow Agent. You record and replay multi-step task sequences.

RECORD MODE - when asked to record a workflow:
1. Acknowledge: "Recording workflow '[name]'. Tell me the steps one by one."
2. After each step instruction from the user, add it to the workflow.
3. When user says "stop recording" or "done": save via save_memory and confirm.

REPLAY MODE - when asked to run a workflow:
1. retrieve_memories(query="workflow: [name]") to load it.
2. Parse the steps array from the JSON.
3. Execute each step with the appropriate tool.
4. After each step: log it but don't speak until all steps complete.
5. At the end: "Workflow '[name]' complete. N steps executed."

LIST MODE - when asked what workflows exist:
1. retrieve_memories(query="type:workflow")
2. Extract workflow names and step counts.
3. Speak: "I know N workflows: [names]."

VARIABLE SUBSTITUTION before each step execution:
  Replace {date} with today's date, {clipboard} with get_clipboard result,
  {time} with current time. Apply to args strings before calling tool.

RULES:
1. Never replay a workflow without first loading it from memory.
2. If a step fails during replay: stop, report which step failed, ask to continue.
3. Workflow names are case-insensitive in lookup.
4. Always confirm before saving: "Saving workflow '[name]' with N steps. Confirm?"
"""

    def run(self, goal: str, context: dict) -> AgentResult:
        context = context if isinstance(context, dict) else {}
        g = goal.lower()

        if any(kw in g for kw in LIST_STUDENT_WORKFLOWS_KEYWORDS):
            return self._list_student_workflows()

        if any(kw in g for kw in STUDENT_PACK_KEYWORDS):
            return self._install_student_pack()

        for workflow_name, patterns in STUDENT_RUN_PATTERNS.items():
            if any(p in g for p in patterns):
                return self._run_builtin_student_workflow(workflow_name, context)

        if any(kw in g for kw in LIST_KEYWORDS):
            return self._list_workflows(context)

        if any(kw in g for kw in REPLAY_KEYWORDS):
            name = self._extract_workflow_name(goal, REPLAY_KEYWORDS)
            if name:
                return self._replay(name, context)

        if any(kw in g for kw in RECORD_KEYWORDS):
            name = self._extract_workflow_name(goal, RECORD_KEYWORDS)
            return AgentResult(
                True,
                f"Ready to record workflow '{name or 'new'}'. Tell me the first step.",
                0,
                [],
                data={"recording": True, "workflow_name": name or "untitled"},
            )

        return self._execute(goal, context)

    def _list_student_workflows(self) -> AgentResult:
        lines = []
        for name, spec in BUILTIN_STUDENT_WORKFLOWS.items():
            lines.append(f"{name}: {spec.get('description', '')}")
        joined = "; ".join(lines)
        return AgentResult(
            True,
            f"Built-in student workflows: {joined}",
            0,
            [],
            data={"builtin_workflows": list(BUILTIN_STUDENT_WORKFLOWS.keys())},
        )

    def _install_student_pack(self) -> AgentResult:
        tools = []
        installed = []
        for name, spec in BUILTIN_STUDENT_WORKFLOWS.items():
            workflow_obj = {
                "type": "workflow",
                "pack": "student_productivity_v1",
                "name": name,
                "description": spec["description"],
                "created_at": datetime.now(IST).isoformat(),
                "step_count": len(spec["steps"]),
                "steps": spec["steps"],
            }
            payload = "WORKFLOW_JSON: " + json.dumps(workflow_obj, ensure_ascii=True)
            save_res = self._run_tool("save_memory", {"text": payload})
            tools.append("save_memory")
            if not self._is_tool_error(save_res):
                installed.append(name)

        if not installed:
            return AgentResult(
                False,
                "I couldn't install the student workflow pack right now.",
                len(tools),
                tools,
                error="student_pack_install_failed",
            )

        pretty = ", ".join(installed)
        return AgentResult(
            True,
            f"Installed student workflows: {pretty}. You can now say commands like 'start class notes' or 'start revision timer'.",
            len(tools),
            tools,
            data={"installed": installed},
        )

    def _run_builtin_student_workflow(self, workflow_name: str, context: dict) -> AgentResult:
        context = context if isinstance(context, dict) else {}
        spec = BUILTIN_STUDENT_WORKFLOWS.get(workflow_name)
        if not spec:
            return AgentResult(False, "That student workflow is unavailable.", 0, [], error="missing_workflow")

        executed = []
        now = datetime.now(IST)
        variables = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "filename": context.get("active_file", ""),
            "clipboard": "",
        }

        try:
            for step in spec["steps"]:
                tool = step.get("tool", "")
                if tool not in self.tool_names:
                    return AgentResult(
                        False,
                        f"Workflow step uses unavailable tool '{tool}'.",
                        len(executed),
                        executed,
                        error="tool_unavailable",
                    )

                raw_args = step.get("args", {})
                if not isinstance(raw_args, dict):
                    raw_args = {}

                args = self._substitute(raw_args, variables)
                if "{clipboard}" in str(raw_args) and not variables["clipboard"]:
                    clip = self._run_tool("get_clipboard", {})
                    if not self._is_tool_error(clip):
                        variables["clipboard"] = clip.strip()
                        args = self._substitute(raw_args, variables)

                r = self._run_tool(tool, args)
                executed.append(tool)
                if self._is_tool_error(r):
                    return AgentResult(
                        False,
                        f"Workflow '{workflow_name}' failed at step {step.get('step')}: {step.get('description')}",
                        len(executed),
                        executed,
                        error=r,
                    )
        except Exception as e:
            return AgentResult(
                False,
                f"Workflow '{workflow_name}' hit an internal error before completion.",
                len(executed),
                executed,
                error=str(e),
            )

        return AgentResult(
            True,
            f"Workflow '{workflow_name}' complete. {len(executed)} steps executed.",
            len(executed),
            executed,
        )

    def _replay(self, name: str, context: dict) -> AgentResult:
        """Load workflow from memory and execute each step."""
        result = self._run_tool("retrieve_memories", {"query": f"workflow {name}"})
        if self._is_error(result) or not result.strip():
            return AgentResult(
                False,
                f"I don't have a workflow called '{name}'. Say 'record a workflow called {name}' to create it.",
                1,
                ["retrieve_memories"],
            )

        try:
            workflow = self._extract_workflow_json(result)
            steps = workflow.get("steps", [])
        except Exception as e:
            return AgentResult(False, f"Couldn't parse workflow '{name}': {e}", 1, ["retrieve_memories"])

        if not steps:
            return AgentResult(False, f"Workflow '{name}' has no steps.", 1, ["retrieve_memories"])

        executed = []
        now = datetime.now(IST)
        variables = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "filename": context.get("active_file", ""),
            "clipboard": "",
        }

        for step in steps:
            tool = step.get("tool", "")
            args = step.get("args", {})
            desc = step.get("description", tool)

            if tool not in self.tool_names:
                return AgentResult(
                    False,
                    f"Step {step['step']} uses tool '{tool}' which isn't available. Stopping at step {step['step']}.",
                    len(executed),
                    executed,
                )

            args = self._substitute(args, variables)

            if "{clipboard}" in str(args) and not variables["clipboard"]:
                variables["clipboard"] = self._run_tool("get_clipboard", {})
                args = self._substitute(args, variables)

            r = self._run_tool(tool, args)
            executed.append(tool)

            if self._is_tool_error(r):
                return AgentResult(
                    False,
                    f"Workflow '{name}' failed at step {step['step']}: {desc}. Error: {r[:80]}",
                    len(executed),
                    executed,
                )

        return AgentResult(
            True,
            f"Workflow '{name}' complete. {len(executed)} steps executed.",
            len(executed),
            executed,
        )

    def _extract_workflow_json(self, text: str) -> dict:
        marker = "WORKFLOW_JSON:"
        src = text or ""
        idx = src.find(marker)
        if idx >= 0:
            src = src[idx + len(marker):].lstrip()

        depth = 0
        start = -1
        for i, ch in enumerate(src):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        candidate = src[start:i + 1]
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and obj.get("type") == "workflow":
                            return obj
        raise ValueError("No workflow JSON found")

    def _list_workflows(self, context: dict) -> AgentResult:
        result = self._run_tool("retrieve_memories", {"query": "type workflow"})
        if self._is_error(result) or not result.strip():
            return AgentResult(
                True,
                "I don't have any saved workflows yet. Say 'record a workflow called [name]' to create one.",
                1,
                ["retrieve_memories"],
            )
        names = re.findall(r'"name":\s*"([^"]+)"', result)
        builtin_names = set(BUILTIN_STUDENT_WORKFLOWS.keys())
        user_names = [n for n in names if n not in builtin_names]
        if not user_names:
            return AgentResult(
                True,
                "No user-recorded workflows found. To see built-ins, say 'list student workflows'.",
                1,
                ["retrieve_memories"],
            )
        name_list = ", ".join(user_names[:8])
        return AgentResult(
            True,
            f"I know {len(user_names)} user workflow{'s' if len(user_names) != 1 else ''}: {name_list}.",
            1,
            ["retrieve_memories"],
        )

    def _extract_workflow_name(self, goal: str, keywords: list) -> str:
        for kw in sorted(keywords, key=len, reverse=True):
            pattern = re.escape(kw) + r"\s+(?:called\s+|named\s+)?([A-Za-z0-9 _-]+?)(?:\s*$|\?|\.)"
            m = re.search(pattern, goal, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    def _substitute(self, args: dict, variables: dict) -> dict:
        """Replace {variable} tokens in string arg values."""
        result = {}
        for k, v in args.items():
            if isinstance(v, str):
                for var, val in variables.items():
                    v = v.replace(f"{{{var}}}", val)
            result[k] = v
        return result
