"""
AppBuilderAgent - generates and saves scripts, tools, and automations.

WHAT IT BUILDS (all written to ~/Desktop by default):
  - Shell scripts (.sh) for system automation
  - Python scripts using only stdlib + packages in requirements.txt
  - AppleScript (.scpt) for macOS UI automation
  - Single-file HTML/CSS/JS tools (opened in browser)
  - Launchd plists for scheduled tasks

RAM CONSTRAINT - code generation in chunks:
  Each LLM call generates <= 512 tokens of code.
  For scripts >100 lines: generate section by section, concatenate.
  NEVER load a second model. NEVER install packages at runtime.

SAFETY CONSTRAINTS (absolute, never override):
  - Only write to ~/Desktop, ~/Documents, or paths user explicitly names
  - Never generate code that deletes user data
  - Never make network requests to unknown/hardcoded endpoints
  - Never modify system files outside user home directory
  - Always open the generated file for user review before executing it
"""

import json
import logging
import os
import re
from datetime import datetime

import pytz

from .base_agent import AgentBase, AgentResult, StepRecord

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

BUILD_KEYWORDS = [
    "build a script",
    "create a script",
    "write a script",
    "build an app",
    "create a tool",
    "make a shortcut",
    "build me a",
    "create me a",
    "write me a",
]
EXPLAIN_KEYWORDS = ["explain what", "what does", "describe the script"]
SCHEDULE_KEYWORDS = [
    "schedule",
    "set up to run",
    "run every",
    "run daily",
    "run on startup",
]

ALLOWED_PACKAGES = {
    "flask",
    "requests",
    "chromadb",
    "ddgs",
    "beautifulsoup4",
    "pypdf2",
    "numpy",
    "ollama",
    "pytz",
    "mlx",
    "mlx_lm",
}

SCRIPT_TYPES = {
    "shell": {"ext": "sh", "header": "#!/bin/bash\nset -e\n"},
    "python": {"ext": "py", "header": "#!/usr/bin/env python3\n"},
    "applescript": {"ext": "scpt", "header": ""},
    "html": {"ext": "html", "header": "<!DOCTYPE html>\n"},
}


class AppBuilderAgent(AgentBase):
    domain_name = "app_builder"
    tool_names = [
        "create_file",
        "find_file",
        "open_file",
        "list_files_in_folder",
        "delete_file",
        "open_application",
        "activate_application",
        "press_keyboard_shortcut",
        "type_text",
        "get_clipboard",
        "set_clipboard",
        "open_url_and_wait",
        "smart_search",
        "web_search",
    ]

    @property
    def system_prompt(self) -> str:
        return """You are DEMASCAS's App Builder. You generate and save scripts and automation tools.

SCRIPT TYPES you can build:
  - Shell (.sh): for system commands, file operations, scheduled tasks
  - Python (.py): using only stdlib or these packages: flask, requests, chromadb,
    ddgs, beautifulsoup4, pypdf2, numpy, pytz
  - AppleScript (.scpt): for macOS UI automation
  - HTML (.html): single-file web tools, no external dependencies

BUILD PROCESS:
1. Classify what type of script is needed from the user's description.
2. Generate the complete script content (keep it minimal and working).
3. Determine filename: ~/Desktop/[descriptive_name].[ext]
4. Call create_file(file_path=..., content=...) to save it.
5. Call open_file(file_path=...) to show the user.
6. End: {"done": true, "say": "I've built [name] and opened it for review."}

GENERATION RULES:
1. Generate minimal working code. No excessive comments or boilerplate.
2. Python: never import packages not in the allowed list above.
3. Shell: always start with #!/bin/bash and set -e.
4. HTML: single file only, no <script src="external"> dependencies.
5. AppleScript: test the smallest action first, build up.
6. Code must fit in one create_file call. If >200 lines: split into functions.
7. NEVER generate code that deletes user data or modifies system files.
8. NEVER hardcode paths like /Users/dakshrathore - use $HOME or ~.
9. Always write to ~/Desktop unless user specifies elsewhere.
10. After create_file succeeds: open_file to show user, then done."""

    def _extra_rules(self) -> str:
        return (
            "11. For 'backup' scripts: use rsync or cp to ~/Desktop/Backup_$(date +%Y%m%d).\n"
            "12. For 'disk usage' scripts: use df -h and du -sh ~/Desktop ~/Documents.\n"
            "13. For 'scheduled' tasks: generate a shell script first, then a launchd plist.\n"
            "14. Script content goes in the 'content' arg of create_file, not in a separate step."
        )

    def run(self, goal: str, context: dict) -> AgentResult:
        g = goal.lower()

        if any(kw in g for kw in EXPLAIN_KEYWORDS):
            return self._explain_script(goal, context)

        if any(kw in g for kw in SCHEDULE_KEYWORDS):
            return self._schedule_flow(goal, context)

        return self._execute(goal, context)

    def _explain_script(self, goal: str, context: dict) -> AgentResult:
        """Find and read a script file, then explain it."""
        words = goal.lower().split()
        ext_words = [w for w in words if any(e in w for e in [".sh", ".py", ".scpt", ".html"])]
        partial = ext_words[0] if ext_words else "script"
        ext = re.search(r"\.(sh|py|scpt|html)", partial)
        ext_str = ext.group(1) if ext else ""

        r = self._run_tool(
            "find_file",
            {
                "folder": "~/Desktop",
                "partial_name": partial.replace(f".{ext_str}", "") if ext_str else partial,
                "extension": ext_str,
            },
        )
        if self._is_error(r):
            return AgentResult(False, "I couldn't find that script on your Desktop.", 1, ["find_file"])

        try:
            data = json.loads(r)
            matches = data.get("matches", [])
            if not matches:
                return AgentResult(False, "No matching script found.", 1, ["find_file"])
            path = matches[0]["path"]
            self._run_tool("open_file", {"file_path": path})
            return AgentResult(
                True,
                f"Opened {matches[0]['name']} for you to review. Ask me to explain specific parts.",
                2,
                ["find_file", "open_file"],
            )
        except Exception:
            return AgentResult(False, "Couldn't open the script.", 1, ["find_file"])

    def _schedule_flow(self, goal: str, context: dict) -> AgentResult:
        """Build script + launchd plist for scheduled execution."""
        ctx = {
            **context,
            "task_hint": (
                "The user wants a scheduled task. "
                "Step 1: create_file for the shell script at ~/Desktop/[name].sh. "
                "Step 2: create_file for the launchd plist at "
                "~/Desktop/com.demascas.[name].plist with the correct interval. "
                "Step 3: open_file to show the plist. "
                "Step 4: done. Include instructions in the summary for how to install the plist."
            ),
        }
        return self._execute(goal, ctx)
