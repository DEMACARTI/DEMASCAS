"""
file_agent.py - Local file management.
"""

import ast
import json
import re
from .base_agent import AgentBase, AgentResult, StepRecord

DEFAULT_SEARCH_FOLDERS = ["~/Downloads", "~/Documents", "~/Desktop"]


class FileAgent(AgentBase):
    domain_name = "file"
    tool_names = [
        "open_application",
        "find_file", "open_file", "list_files_in_folder", "create_file", "delete_file",
        "summarize_latest_pdf", "summarize_selected_pdf",
        "get_active_window_tree", "get_clipboard", "set_clipboard",
        "press_keyboard_shortcut",
    ]

    @property
    def system_prompt(self) -> str:
        return (
            "You are the DEMASCAS File Agent for macOS local file operations.\n"
            "Rules:\n"
            "1. ALWAYS call find_file FIRST before open_file. Never guess a path.\n"
            "2. Use tilde paths: ~/Downloads, ~/Documents, ~/Desktop. NEVER /Users/<n>/...\n"
            "3. If no matches, tell the user. Do not create files and do not open browser.\n"
            "4. One match: extract path and call open_file.\n"
            "5. Multiple matches: finish with done and ask which file the user wants.\n"
            "6. If user wants to read PDF content, summarize_latest_pdf is usually faster.\n"
            "7. If no folder hint, search ~/Downloads first.\n"
            "8. partial_name must be specific, not generic terms.\n"
            "9. extension should be without dot (pdf, docx, xlsx).\n"
            "10. NEVER delete anything unless the goal explicitly confirms deletion."
        )

    def run(self, goal, context):
        g = (goal or "").lower()
        open_folder = re.search(r"\b(open|show|go to)\b", g) and re.search(
            r"\b(downloads?|documents?|desktop)\b", g
        ) and "folder" in g
        direct_open = re.search(r"\b(open|show|view|read|launch)\b", g) and re.search(
            r"\b(pdf|docx|doc|word|excel|xlsx|pptx|slides|presentation|file|document)\b", g
        )
        list_files = re.search(r"\b(list|show|what files|what's in|whats in)\b", g)

        summarize = re.search(r"\b(summari[sz]e|summary)\b", g)

        if open_folder:
            return self._open_known_folder_flow(g)
        if summarize:
            return self._summarize_flow(goal, context)
        if direct_open:
            return self._direct_open_flow(goal, context)
        if list_files:
            return self._list_files_flow(goal, context)
        return self._execute(goal, context)

    def _open_known_folder_flow(self, goal_lower: str) -> AgentResult:
        folder_name = "Downloads"
        shortcut = "command+option+l"

        if "documents" in goal_lower:
            folder_name = "Documents"
            shortcut = "command+shift+o"
        elif "desktop" in goal_lower:
            folder_name = "Desktop"
            shortcut = "command+shift+d"

        open_finder = self._call_tool("open_application", {"app_name": "Finder"})
        if self._is_tool_error(open_finder):
            return AgentResult(False, "I couldn't open Finder.", 1, ["open_application"], error=open_finder)

        jump_folder = self._call_tool("press_keyboard_shortcut", {"keys": shortcut})
        if self._is_tool_error(jump_folder):
            return AgentResult(
                False,
                f"I opened Finder, but couldn't open {folder_name}.",
                2,
                ["open_application", "press_keyboard_shortcut"],
                error=jump_folder,
            )

        return AgentResult(
            True,
            f"Opened {folder_name} folder.",
            2,
            ["open_application", "press_keyboard_shortcut"],
        )

    def _direct_open_flow(self, goal, context):
        partial_name, extension, folder = self._extract_file_params(goal)
        search_folders = [folder] if folder else list(DEFAULT_SEARCH_FOLDERS)

        tried_tools = []
        for f in search_folders:
            args = {"partial_name": partial_name, "extension": extension, "folder": f}
            result = self._call_tool("find_file", args)
            tried_tools.append("find_file")
            if self._is_tool_error(result):
                continue
            return self._handle_find_result(result, partial_name, extension, goal, context)

        ext_name = extension or "file"
        label = partial_name or "your request"
        return AgentResult(False, f"I couldn't find any {ext_name} matching '{label}'.", len(tried_tools), tried_tools)

    def _handle_find_result(self, result, partial_name, extension, goal, context):
        data = None
        try:
            data = json.loads(result)
        except Exception:
            try:
                data = ast.literal_eval(result)
            except Exception:
                data = None

        if not isinstance(data, dict):
            return AgentResult(False, "I couldn't understand the file search result.", 1, ["find_file"], error="parse_error")

        count = int(data.get("count", 0))
        matches = data.get("matches", []) if isinstance(data.get("matches", []), list) else []

        if count == 0 or not matches:
            ext_name = extension or "file"
            label = partial_name or "your request"
            return AgentResult(False, f"I couldn't find any {ext_name} matching '{label}'.", 1, ["find_file"])

        if count == 1:
            path = matches[0].get("path", "") if isinstance(matches[0], dict) else ""
            if not path:
                return AgentResult(False, "I found a file but couldn't read its path.", 1, ["find_file"], error="missing_path")
            open_res = self._call_tool("open_file", {"file_path": path})
            if self._is_tool_error(open_res):
                return AgentResult(False, "I found the file but couldn't open it.", 2, ["find_file", "open_file"], error=open_res)
            name = matches[0].get("name", "the file") if isinstance(matches[0], dict) else "the file"
            return AgentResult(True, f"Opening {name}.", 2, ["find_file", "open_file"])

        names = []
        for m in matches[:8]:
            if isinstance(m, dict):
                names.append(m.get("name", "unknown"))
        return AgentResult(
            success=True,
            summary=f"I found {count} files: {', '.join(names)}. Which one?",
            steps_taken=1,
            tool_calls=["find_file"],
            data={"pending_file_choices": matches[:8]},
        )

    def _summarize_flow(self, goal, context):
        """Summarize a named PDF, the PDF selected in Finder, or the latest one."""
        g = (goal or "").lower()
        if re.search(r"\b(selected|highlighted)\b", g):
            res = self._call_tool("summarize_selected_pdf", {})
            if self._is_tool_error(res):
                return AgentResult(False, "I couldn't summarize the selected file.", 1,
                                   ["summarize_selected_pdf"], error=res)
            return AgentResult(True, res[:600], 1, ["summarize_selected_pdf"])

        partial_name, _ext, folder = self._extract_file_params(goal)
        partial_name = re.sub(r"\b(summari[sz]e|summary|of|latest|last|newest|recent|most)\b", " ",
                              partial_name or "")
        partial_name = re.sub(r"\s+", " ", partial_name).strip()

        if partial_name:
            for f in ([folder] if folder else list(DEFAULT_SEARCH_FOLDERS)):
                found = self._call_tool("find_file", {"partial_name": partial_name,
                                                      "extension": "pdf", "folder": f})
                if self._is_tool_error(found):
                    continue
                try:
                    data = json.loads(found)
                except Exception:
                    data = {}
                matches = data.get("matches", []) if isinstance(data, dict) else []
                if len(matches) == 1:
                    path = matches[0].get("path", "")
                    res = self._call_tool("summarize_selected_pdf", {"file_path": path})
                    if self._is_tool_error(res):
                        return AgentResult(False, "I found the PDF but couldn't summarize it.", 2,
                                           ["find_file", "summarize_selected_pdf"], error=res)
                    return AgentResult(True, res[:600], 2, ["find_file", "summarize_selected_pdf"])
                if len(matches) > 1:
                    return self._handle_find_result(found, partial_name, "pdf", goal, context)

        res = self._call_tool("summarize_latest_pdf", {})
        if self._is_tool_error(res):
            return AgentResult(False, "I couldn't summarize your latest PDF.", 1,
                               ["summarize_latest_pdf"], error=res)
        return AgentResult(True, res[:600], 1, ["summarize_latest_pdf"])

    def _list_files_flow(self, goal, context):
        g = (goal or "").lower()
        folder = "~/Downloads"
        if "documents" in g:
            folder = "~/Documents"
        elif "desktop" in g:
            folder = "~/Desktop"

        result = self._call_tool("list_files_in_folder", {"folder_path": folder})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't list files in that folder.", 1, ["list_files_in_folder"], error=result)

        lines = [ln.strip() for ln in result.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.lower().startswith("files in")]
        return AgentResult(True, f"I found {len(lines)} items in {folder}.", 1, ["list_files_in_folder"])

    def _extract_file_params(self, goal):
        g = (goal or "").lower()

        extension_map = {
            "pdf": "pdf",
            "word": "docx",
            "docx": "docx",
            "document": "docx",
            "excel": "xlsx",
            "spreadsheet": "xlsx",
            "xlsx": "xlsx",
            "ppt": "pptx",
            "pptx": "pptx",
            "slides": "pptx",
            "presentation": "pptx",
        }

        extension = ""
        for k, v in extension_map.items():
            if re.search(rf"\b{k}\b", g):
                extension = v
                break

        folder = ""
        if "downloads" in g:
            folder = "~/Downloads"
        elif "documents" in g:
            folder = "~/Documents"
        elif "desktop" in g:
            folder = "~/Desktop"

        cleaned = re.sub(
            r"\b(open|find|show|view|read|launch|my|the|a|file|document|documents|pdf|docx|word|excel|xlsx|pptx|presentation|slides|from|in|folder)\b",
            " ",
            g,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,_-")

        if cleaned in {"", "file", "pdf", "document", "documents"}:
            return "", extension, folder

        return cleaned, extension, folder

    def _on_step_complete(self, record):
        if record.success and record.tool == "open_file":
            m = re.search(r"((?:~|/)[^\s]+)", record.result)
            if m:
                self._active_context["active_file"] = m.group(1)
