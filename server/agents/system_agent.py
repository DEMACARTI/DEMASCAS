"""
system_agent.py - App control, UI automation, keyboard shortcuts, screen reading.
"""

import re
from .base_agent import AgentBase, AgentResult, StepRecord

APP_ALIASES = {
    "chrome": "Google Chrome", "firefox": "Firefox", "safari": "Safari",
    "brave": "Brave Browser", "terminal": "Terminal",
    "vscode": "Visual Studio Code", "code": "Visual Studio Code",
    "powerpoint": "Microsoft PowerPoint", "power point": "Microsoft PowerPoint",
    "microsoft powerpoint": "Microsoft PowerPoint",
    "finder": "Finder", "spotify": "Spotify", "slack": "Slack",
    "zoom": "Zoom", "notes": "Notes", "calendar": "Calendar",
    "mail": "Mail", "photos": "Photos", "preview": "Preview",
}


class SystemAgent(AgentBase):
    domain_name = "system"
    tool_names = [
        "open_application", "quit_application", "activate_application",
        "list_running_applications", "wait_for_app_ready",
        "click_ui_element", "type_text", "type_text_into_element", "scroll_direction",
        "drag_element", "find_and_click", "right_click_element", "type_and_submit",
        "scroll_to_element", "wait_for_element",
        "press_keyboard_shortcut",
        "get_active_window_tree", "list_open_windows",
        "get_clipboard", "set_clipboard",
        "vision_execute",
    ]

    @property
    def system_prompt(self) -> str:
        return (
            "You are the DEMASCAS System Agent for macOS app and UI control.\n"
            "Rules:\n"
            "1. Prefer press_keyboard_shortcut over click_ui_element when shortcut exists.\n"
            "2. Before clicking UI elements, use get_active_window_tree to verify they exist.\n"
            "3. Use activate_application before interacting with app UI.\n"
            "4. NEVER quit Finder.\n"
            "5. For typing into a field, use type_text_into_element.\n"
            "6. For volume/brightness, prefer press_keyboard_shortcut (F1/F2 and F10/F11/F12).\n"
            "7. If element lookup fails, fallback to find_and_click or vision_execute.\n"
            "8. Keyboard shortcut format examples: cmd+c, cmd+shift+4, f11, volume_up."
        )

    def run(self, goal, context):
        goal_lower = (goal or "").lower().strip()

        if self._looks_like_new_powerpoint_task(goal_lower):
            return self._new_powerpoint_presentation_flow()

        app = self._parse_app_open(goal_lower)
        if app:
            return self._app_open_flow(app, context)

        app = self._extract_app_close(goal_lower)
        if app:
            if app.lower() == "finder":
                return AgentResult(False, "I won't quit Finder.", 0, [], error="forbidden_app")
            return self._app_close_flow(app, context)

        if re.search(r"what('?s| is)?\s+in.*clipboard|read.*clipboard|show.*clipboard", goal_lower):
            return self._clipboard_read_flow(context)

        if re.search(r"what.?s on.*screen|what do you see|describe.*screen", goal_lower):
            return self._screen_read_flow(context)

        return self._execute(goal, context)

    def _looks_like_new_powerpoint_task(self, goal_lower: str) -> bool:
        has_powerpoint = any(
            k in goal_lower
            for k in ["powerpoint", "power point", "microsoft powerpoint", "point app", "ppt"]
        )
        has_create = any(k in goal_lower for k in ["create", "new", "blank"])
        has_doc = any(k in goal_lower for k in ["presentation", "slides", "ppt"])
        return has_powerpoint and has_create and has_doc

    def _new_powerpoint_presentation_flow(self) -> AgentResult:
        tools: list[str] = []

        open_res = self._call_tool("open_application", {"app_name": "Microsoft PowerPoint"})
        tools.append("open_application")
        if self._is_tool_error(open_res):
            return AgentResult(
                False,
                "I couldn't open Microsoft PowerPoint.",
                len(tools),
                tools,
                error=open_res,
            )

        wait_res = self._call_tool("wait_for_app_ready", {"app_name": "Microsoft PowerPoint", "timeout": 12})
        tools.append("wait_for_app_ready")
        if self._is_tool_error(wait_res):
            # Fallback: app may be open but not frontmost/fully focused yet.
            act_res = self._call_tool("activate_application", {"app_name": "Microsoft PowerPoint"})
            tools.append("activate_application")
            if not self._is_tool_error(act_res):
                wait_res_2 = self._call_tool("wait_for_app_ready", {"app_name": "Microsoft PowerPoint", "timeout": 8})
                tools.append("wait_for_app_ready")
                if self._is_tool_error(wait_res_2):
                    return AgentResult(
                        False,
                        "PowerPoint opened, but it wasn't ready in time.",
                        len(tools),
                        tools,
                        error=wait_res_2,
                    )
            else:
                return AgentResult(
                    False,
                    "PowerPoint opened, but it wasn't ready in time.",
                    len(tools),
                    tools,
                    error=wait_res,
                )

        new_res = self._call_tool("press_keyboard_shortcut", {"keys": "command+n"})
        tools.append("press_keyboard_shortcut")
        if self._is_tool_error(new_res):
            return AgentResult(
                False,
                "PowerPoint opened, but I couldn't create the blank presentation.",
                len(tools),
                tools,
                error=new_res,
            )

        self._active_context["active_app"] = "Microsoft PowerPoint"
        return AgentResult(
            True,
            "Microsoft PowerPoint is open and a new blank presentation is ready.",
            len(tools),
            tools,
        )

    def _app_open_flow(self, app_name, context):
        resolved = APP_ALIASES.get(app_name.lower(), app_name)
        result = self._call_tool("open_application", {"app_name": resolved})
        if self._is_tool_error(result):
            return AgentResult(False, f"I couldn't open {resolved}.", 1, ["open_application"], error=result)
        self._active_context["active_app"] = resolved
        return AgentResult(True, f"Opening {resolved}.", 1, ["open_application"])

    def _app_close_flow(self, app_name, context):
        resolved = APP_ALIASES.get(app_name.lower(), app_name)
        result = self._call_tool("quit_application", {"app_name": resolved})
        if self._is_tool_error(result):
            return AgentResult(False, f"I couldn't close {resolved}.", 1, ["quit_application"], error=result)
        return AgentResult(True, f"Closed {resolved}.", 1, ["quit_application"])

    def _clipboard_read_flow(self, context):
        result = self._call_tool("get_clipboard", {})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't read the clipboard.", 1, ["get_clipboard"], error=result)
        text = (result or "").strip()
        if not text:
            text = "Clipboard is empty."
        else:
            text = text[:200]
        return AgentResult(True, text, 1, ["get_clipboard"])

    def _screen_read_flow(self, context):
        result = self._call_tool("get_active_window_tree", {})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't read the screen state.", 1, ["get_active_window_tree"], error=result)

        lines = result.splitlines()
        active_app = "current app"
        for ln in lines:
            if "ACTIVE APP" in ln.upper():
                active_app = ln.split(":", 1)[-1].strip() if ":" in ln else ln.strip()
                break

        button_count = sum(1 for ln in lines if "[button]" in ln.lower())
        field_count = sum(1 for ln in lines if "[text field]" in ln.lower())

        summary = (
            f"You're in {active_app}. I can see about {button_count} buttons "
            f"and {field_count} text fields on screen."
        )
        return AgentResult(True, summary, 1, ["get_active_window_tree"])

    def _parse_app_open(self, g: str) -> str:
        m = re.match(r"^(?:open|launch|start)\s+(.+)$", g)
        if not m:
            return ""

        name = m.group(1).strip()

        # Reject if it looks like a file
        if re.search(r"\.(pdf|docx|xlsx|pptx|txt|html|com|org|io|app)$", name):
            return ""

        # Reject if it looks like a web destination
        web_words = (
            "page", "website", "site", "the ", "for ", "wikipedia",
            "google", "reddit", "github", "url", "link",
        )
        lower_name = name.lower()
        if any(w in lower_name for w in web_words):
            return ""

        # Reject long multi-word strings that are likely web/search queries
        if len(name.split()) > 3:
            return ""

        return name

    def _extract_app_close(self, goal_lower):
        m = re.match(r"^(quit|close|exit|kill)\s+(.+)$", goal_lower)
        if not m:
            return ""
        return m.group(2).strip()

    def _on_step_complete(self, record):
        if record.success and record.tool in {"open_application", "activate_application"}:
            app_name = record.args.get("app_name") if isinstance(record.args, dict) else None
            if app_name:
                self._active_context["active_app"] = str(app_name)
