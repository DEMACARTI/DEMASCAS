"""
AppLearnerAgent - teaches DEMASCAS how to use any macOS app it hasn't seen.

WHAT IT DOES:
  User says "learn how to use [AppName]" -> agent opens the app,
  walks its full accessibility tree, opens every menu to record actions,
  saves a structured profile to ChromaDB, speaks a summary.

  On future commands like "how do I [action] in [AppName]", the agent
  retrieves the saved profile and answers from memory without re-exploring.

HOW THE EXPLORATION WORKS (step by step):
  1. open_application(app_name)
  2. wait_for_app_ready(app_name, timeout=5)
  3. get_active_window_tree() -> parse menu bar items from result
  4. For each menu item found (File, Edit, View, etc.):
     a. click_ui_element(element_name=menu_name)
     b. get_active_window_tree() -> record submenu items
     c. press_keyboard_shortcut(keys="escape") -> close menu
  5. right_click_element on main canvas area -> record context menu
  6. press_keyboard_shortcut("cmd+,") -> check if preferences exist
  7. Compile profile dict (see format below)
  8. save_memory(text=json.dumps(profile))
  9. Return spoken summary

MEMORY FORMAT (saved as JSON string via save_memory):
{
  "type": "app_profile",
  "app": "AppName",
  "learned_at": "ISO timestamp",
  "menus": {"File": ["New", "Open", "Save", "Export"], "Edit": [...]},
  "shortcuts": {"new": "cmd+n", "save": "cmd+s", "preferences": "cmd+,"},
  "key_elements": ["button: New Document", "sidebar: Projects"],
  "has_preferences": true,
  "workflows": [
    "To create new document: Cmd+N or File > New",
    "To save: Cmd+S or File > Save"
  ]
}
"""

import json
import logging
import re
from datetime import datetime

import pytz

from .base_agent import AgentBase, AgentResult, StepRecord

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

LEARN_KEYWORDS = [
    "learn how to use",
    "learn the app",
    "explore",
    "map out",
    "figure out how to use",
    "teach yourself",
]
RECALL_KEYWORDS = [
    "what can you do in",
    "how do i use",
    "how do i",
    "what are the shortcuts for",
    "what did you learn about",
]


class AppLearnerAgent(AgentBase):
    domain_name = "app_learner"
    tool_names = [
        "open_application",
        "activate_application",
        "wait_for_app_ready",
        "get_active_window_tree",
        "list_open_windows",
        "click_ui_element",
        "right_click_element",
        "find_and_click",
        "press_keyboard_shortcut",
        "wait_for_element",
        "save_memory",
        "retrieve_memories",
        "get_clipboard",
        "set_clipboard",
        "vision_execute",
    ]

    @property
    def system_prompt(self) -> str:
        return """You are DEMASCAS's App Learner. You explore macOS apps and build knowledge profiles.

EXPLORATION RULES:
1. Always open_application first, then wait_for_app_ready.
2. Use get_active_window_tree to see available UI elements and menus.
3. Click each menu item (File, Edit, View, etc.) to record its contents.
4. After opening each menu, press escape to close it before opening the next.
5. Try cmd+, to find preferences. Note if they exist.
6. Use right_click_element on the main content area to find context menus.
7. NEVER perform destructive actions: no delete, no format, no send, no purchase.
8. After exploration, call save_memory with a JSON profile of what you found.
9. End with a spoken summary of key capabilities discovered.

RECALL RULES (when user asks "how do I X in AppName"):
1. Call retrieve_memories(query="app_profile AppName") first.
2. If profile found: answer from the profile. No need to re-explore.
3. If not found: say "I haven't learned that app yet. Say 'learn [app]' to teach me."
"""

    def _extra_rules(self) -> str:
        return (
            "10. Parse menu items from get_active_window_tree output: "
            "look for [menu bar item] or [menu item] tags in the tree.\n"
            "11. save_memory arg must be a JSON string with type='app_profile'.\n"
            "12. Limit exploration to 8 menus maximum - stop and save after that."
        )

    def run(self, goal: str, context: dict) -> AgentResult:
        g = goal.lower()

        if any(kw in g for kw in RECALL_KEYWORDS):
            app = self._extract_app_name(goal)
            if not app:
                return AgentResult(False, "Which app are you asking about?", 0, [])
            result = self._run_tool("retrieve_memories", {"query": f"app_profile {app}"})
            if self._is_error(result) or not result.strip():
                return AgentResult(
                    True,
                    f"I haven't learned {app} yet. Say 'learn {app}' and I'll explore it.",
                    1,
                    ["retrieve_memories"],
                )
            return self._summarize_profile(result, app)

        app = self._extract_app_name(goal)
        if not app:
            return AgentResult(
                False,
                "Which app should I learn? Say 'learn how to use [app name]'.",
                0,
                [],
            )

        return self._explore(app, goal, context)

    def _explore(self, app: str, goal: str, context: dict) -> AgentResult:
        """Full exploration flow."""
        ctx = {
            **context,
            "task_hint": f"Explore {app} methodically. Open menus, record items, save profile.",
        }
        return self._execute(goal, ctx)

    def _summarize_profile(self, raw_memory: str, app: str) -> AgentResult:
        """Parse saved profile and speak a natural summary."""
        try:
            m = re.search(r"\{.*\}", raw_memory, re.DOTALL)
            if m:
                profile = json.loads(m.group(0))
                menus = list(profile.get("menus", {}).keys())
                wflows = profile.get("workflows", [])
                shortcuts = profile.get("shortcuts", {})
                menu_str = ", ".join(menus[:5]) if menus else "none found"
                wf_str = wflows[0] if wflows else ""
                sc_count = len(shortcuts)
                summary = (
                    f"I know {app}. It has menus: {menu_str}. "
                    f"I found {sc_count} shortcuts. {wf_str}"
                )
                return AgentResult(True, summary, 1, ["retrieve_memories"])
        except Exception:
            pass
        return AgentResult(
            True,
            f"I have notes on {app} but couldn't parse them cleanly.",
            1,
            ["retrieve_memories"],
        )

    def _extract_app_name(self, goal: str) -> str:
        """Extract app name from 'learn how to use X' or 'how do I use X'."""
        patterns = [
            r"learn(?:\s+how\s+to\s+use|\s+the\s+app)?\s+([A-Za-z0-9 ]+?)(?:\s*$|\?|\.)",
            r"explore\s+([A-Za-z0-9 ]+?)(?:\s*$|\?|\.)",
            r"(?:what can you do in|how do i use)\s+([A-Za-z0-9 ]+?)(?:\s*$|\?|\.)",
        ]
        for pat in patterns:
            m = re.search(pat, goal, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                if name and len(name) > 1:
                    return name
        return ""

    def _on_step(self, rec: StepRecord) -> None:
        if rec.tool == "save_memory" and rec.ok:
            logger.info("[app_learner] Profile saved to memory.")
