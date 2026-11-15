#!/usr/bin/env python3
"""
DEMASCAS — server/tool_server.py
Unified Tool Gateway — serves both native + MCP tools over HTTP.

The C++ daemon hits this single endpoint; routing to native in-process
Python functions or to MCP subprocess servers is invisible to the caller.

Endpoints:
  POST /tool       — Execute a named tool  {"name": "...", "arguments": {...}}
  GET  /tools      — Return ALL tool schemas (native + MCP, Ollama format)
  GET  /health     — Readiness check  {"status": "ok", "native": N, "mcp": M}
  GET  /mcp/status — MCP-specific server status (RSS, tool list per server)

Start:
  python server/tool_server.py
"""

import atexit
import inspect
import json
import sys
import os
import logging
import re
import secrets
import signal
import subprocess
import threading
import logging as _logging
_logging.getLogger("primp").setLevel(_logging.WARNING)   # web-search request spam
import time

# Ensure the DEMASCAS package root is on the path so we can import
# actuators/, sensors/, core/, and mcp/ directly.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from flask import Flask, request, jsonify

# ── Import all native tool functions ─────────────────────────────────

from actuators.file_system import (
    summarize_latest_pdf, list_files_in_folder, create_file, delete_file,
    find_file, open_file, summarize_selected_pdf,
)
from actuators.network import search_web, read_webpage
from actuators.system_control import (
    click_ui_element,
    open_application,
    open_url,
    open_url_and_wait,
    press_keyboard_shortcut,
    type_text,
    type_text_into_element,
    quit_application,
    activate_application,
    list_running_applications,
    scroll_direction,
    drag_element,
    wait_for_element,
    wait_for_app_ready,
    get_clipboard,
    set_clipboard,
    find_and_click,
    right_click_element,
    type_and_submit,
    switch_tab,
    scroll_to_element,
)
from core.memory import save_memory, retrieve_memories
from sensors.vision import get_active_window_tree, list_open_windows, vision_execute
from core.toolbox import TOOLS as NATIVE_TOOL_SCHEMAS
from server.tool_router import select_tools_for_query
from actuators.communication import (
    read_current_email,
    compose_email,
    smart_reply,
    confirm_and_execute,
    extract_contact,
    web_research_and_report,
)
from actuators.smart_search import smart_search, get_weather, get_news
from actuators.system_commands import run_shell_command, get_system_info, set_default_browser
from core.preferences import set_preference, remove_preference, get_all_preferences, set_humor_level
from core.learning import save_preference as learning_save_preference
from core.orchestrator import architect_plan

from actuators.browser import (
    browser_execute_js,
    browser_get_url, browser_get_title,
    browser_navigate, browser_navigate_and_wait,
    browser_new_tab, browser_close_tab,
    browser_list_tabs, browser_switch_to_tab,
    browser_back, browser_forward, browser_reload,
    browser_get_page_text, browser_get_page_html,
    browser_get_links, browser_get_inputs,
    browser_click, browser_click_text, browser_click_xpath,
    browser_type, browser_type_by_label, browser_press_key,
    browser_scroll, browser_scroll_to_element as browser_scroll_to_el,
    browser_fill_form, browser_select_option,
    browser_check_checkbox, browser_submit_form,
    browser_wait_for_element as browser_wait_el,
    browser_wait_for_page_load, browser_element_exists,
    browser_get_dom_summary,
    gmail_compose_draft, gmail_read_email,
)
from actuators.web_agent import (
    web_analyze_page, web_scan_for_obstacles, web_handle_obstacle,
    web_search, web_read_article, web_research,
    web_execute_goal, web_smart_click,
    web_youtube_search, web_youtube_play_first,
    web_ecommerce_search,
)
from core.screen_context import get_screen_context_block, get_screen_digest
from actuators.system_control import is_risky_shortcut
from actuators.app_script import (
    run_app_script, is_destructive as _script_is_destructive, click_menu_item, is_risky_menu,
)
from core.appknowledge.learner import learn_app, guide_app
from actuators.messaging import deliver_chat_message
from core.web.crawler import (
    research_web, scrape_webpage, watch_topic, unwatch_topic, web_knowledge, forget_web,
)
from actuators.documents import (
    create_presentation, edit_presentation, create_document, create_spreadsheet,
)
from core.utils import resolve_safe_path

logger = logging.getLogger(__name__)

# Agent system
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(__file__))
try:
    from agents import run_agent, should_use_agent, route_command, get_dispatcher
    _AGENTS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Agent system unavailable: {e}")
    _AGENTS_AVAILABLE = False


# ── Safe click wrapper (3-tier fallback) ─────────────────────────────

def click_ui_element_safe(element_name: str) -> str:
    """
    Safe wrapper around click_ui_element with a fast-first fallback chain:
      1. click_ui_element directly (one accessibility-tree pass for the name
         and all its aliases) — the common case costs a single traversal.
      2. If not found: the element may still be appearing → wait up to 2 s,
         then click once more.
      3. If still not found and the VLM server is running → vision click.
      4. Otherwise a structured error the LLM can act on.

    (Previously step 2's wait ran BEFORE every click, adding a full
    1-2 s tree traversal even when the element was already on screen.)
    """
    try:
        click_result = click_ui_element(element_name)
        if click_result.startswith("Success"):
            return click_result
        print(f"[SAFE-CLICK] First attempt failed: {click_result}")
        if "Timeout" not in click_result:
            wait_result = wait_for_element(element_name, timeout=2)
            if "Success" in wait_result:
                click_result = click_ui_element(element_name)
                if click_result.startswith("Success"):
                    return click_result
    except Exception as e:
        print(f"[SAFE-CLICK] Accessibility click error: {e}")

    # Vision-based click via VLM (skipped instantly when the VLM is off)
    try:
        vision_result = vision_execute(f"Click the element labeled '{element_name}'")
        if not vision_result.startswith("[-]"):
            return vision_result
        print(f"[SAFE-CLICK] VLM vision unavailable/failed: {vision_result}")
    except Exception as e:
        print(f"[SAFE-CLICK] Vision error: {e}")

    return (
        f"[-] Could not click '{element_name}'. "
        "The element was not found in the accessibility tree"
        " (and screen vision is unavailable or could not find it). "
        "Try: (1) read the screen with get_active_window_tree to find the exact element name, "
        "or (2) use press_keyboard_shortcut for the equivalent action."
    )

# ── Import MCP manager (lazy — zero cost if no servers configured) ───

from mcp.manager import get_manager, MCPManager

# ── Native tool dispatch map (mirrors core/agent.py TOOL_MAP) ────────

TOOL_MAP = {
    "click_ui_element":          click_ui_element_safe,
    "type_text":                 type_text,
    "type_text_into_element":    type_text_into_element,
    "open_application":          open_application,
    "press_keyboard_shortcut":   press_keyboard_shortcut,
    "summarize_latest_pdf":      summarize_latest_pdf,
    "summarize_selected_pdf":    summarize_selected_pdf,
    "get_active_window_tree":    get_active_window_tree,
    "quit_application":          quit_application,
    "activate_application":      activate_application,
    "list_running_applications": list_running_applications,
    "list_files_in_folder":      list_files_in_folder,
    "create_file":               create_file,
    "delete_file":               delete_file,
    "find_file":                 find_file,
    "open_file":                 open_file,
    "search_web":                search_web,
    "read_webpage":              read_webpage,
    "save_memory":               save_memory,
    "retrieve_memories":         retrieve_memories,
    "scroll_direction":          scroll_direction,
    "drag_element":              drag_element,
    "list_open_windows":         list_open_windows,
    "open_url":                  open_url,
    "open_url_and_wait":         open_url_and_wait,
    # ── New agentic tools ────────────────────────────────────────
    "wait_for_element":          wait_for_element,
    "wait_for_app_ready":        wait_for_app_ready,
    "get_clipboard":             get_clipboard,
    "set_clipboard":             set_clipboard,
    
    # ── System and Intelligence Tools ────────────────────────────
    "run_shell_command":         run_shell_command,
    "get_system_info":           get_system_info,
    "set_default_browser":       set_default_browser,
    "set_preference":            set_preference,
    "remove_preference":         remove_preference,
    "set_humor_level":           set_humor_level,
    "save_user_preference":      learning_save_preference,
    # ── Plan-Mode Orchestrator ───────────────────────────────────
    "architect_plan":            architect_plan,
    
    "find_and_click":            find_and_click,
    "right_click_element":       right_click_element,
    "type_and_submit":           type_and_submit,
    "switch_tab":                switch_tab,
    "scroll_to_element":         scroll_to_element,
    # ── Vision tool ──────────────────────────────────────────────
    "vision_execute":            vision_execute,
    # ── Communication tools (JARVIS-grade) ───────────────────────
    "read_current_email":        read_current_email,
    "compose_email":             compose_email,
    "smart_reply":               smart_reply,
    "confirm_and_execute":       confirm_and_execute,
    "extract_contact":           extract_contact,
    "web_research_and_report":   web_research_and_report,
    # ── Smart search & realtime ──────────────────────────────────
    "smart_search":              smart_search,
    "get_weather":               get_weather,
    "get_news":                  get_news,
    # ── Browser automation (Brave via JS injection) ──────────────
    "browser_execute_js":        browser_execute_js,
    "browser_get_url":           browser_get_url,
    "browser_get_title":         browser_get_title,
    "browser_navigate":          browser_navigate,
    "browser_navigate_and_wait": browser_navigate_and_wait,
    "browser_new_tab":           browser_new_tab,
    "browser_close_tab":         browser_close_tab,
    "browser_list_tabs":         browser_list_tabs,
    "browser_switch_to_tab":     browser_switch_to_tab,
    "browser_back":              browser_back,
    "browser_forward":           browser_forward,
    "browser_reload":            browser_reload,
    "browser_get_page_text":     browser_get_page_text,
    "browser_get_page_html":     browser_get_page_html,
    "browser_get_links":         browser_get_links,
    "browser_get_inputs":        browser_get_inputs,
    "browser_click":             browser_click,
    "browser_click_text":        browser_click_text,
    "browser_click_xpath":       browser_click_xpath,
    "browser_type":              browser_type,
    "browser_type_by_label":     browser_type_by_label,
    "browser_press_key":         browser_press_key,
    "browser_scroll":            browser_scroll,
    "browser_scroll_to_element": browser_scroll_to_el,
    "browser_fill_form":         browser_fill_form,
    "browser_select_option":     browser_select_option,
    "browser_check_checkbox":    browser_check_checkbox,
    "browser_submit_form":       browser_submit_form,
    "browser_wait_for_element":  browser_wait_el,
    "browser_wait_for_page_load": browser_wait_for_page_load,
    "browser_element_exists":    browser_element_exists,
    "browser_get_dom_summary":   browser_get_dom_summary,
    "gmail_compose_draft":       gmail_compose_draft,
    "gmail_read_email":          gmail_read_email,
    # ── Web agent (autonomous browsing) ──────────────────────────
    "web_analyze_page":          web_analyze_page,
    "web_scan_for_obstacles":    web_scan_for_obstacles,
    "web_handle_obstacle":       web_handle_obstacle,
    "web_search":                web_search,
    "web_read_article":          web_read_article,
    "web_research":              web_research,
    "web_execute_goal":          web_execute_goal,
    "web_smart_click":           web_smart_click,
    "web_youtube_search":        web_youtube_search,
    "web_youtube_play_first":    web_youtube_play_first,
    "web_ecommerce_search":      web_ecommerce_search,
    # ── Documents (built as files, opened in the user's apps) ────────
    "create_presentation":       create_presentation,
    "edit_presentation":         edit_presentation,
    "create_document":           create_document,
    "create_spreadsheet":        create_spreadsheet,
    # ── App scripting (the app's own dictionary, guarded) ────────────
    "run_app_script":            run_app_script,
    "click_menu_item":           click_menu_item,
    "learn_app":                 learn_app,
    "guide_app":                 guide_app,
    # ── Chat messages (read back, send only on a spoken yes) ─────────
    "deliver_chat_message":      deliver_chat_message,
    # ── Web knowledge (scraped, stored, reused in every task) ────────
    "research_web":              research_web,
    "scrape_webpage":            scrape_webpage,
    "watch_topic":               watch_topic,
    "unwatch_topic":             unwatch_topic,
    "web_knowledge":             web_knowledge,
    "forget_web":                forget_web,
}

# ── Result helpers ───────────────────────────────────────────────────
# Tools return either plain strings or dicts like {"success": bool, ...}.
# Results are serialised as JSON (never Python repr) so every consumer —
# the C++ daemon, agents, the learning engine — can parse them reliably.

_ERROR_PREFIXES = ("[-]", "failed", "error", "timeout", "could not", "refused")


def _serialize_result(obj) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (dict, list)):
        try:
            return json.dumps(obj, ensure_ascii=False, default=str)
        except Exception:
            pass
    return str(obj)


def _result_failed(obj) -> bool:
    """True if a raw tool return value represents a failure."""
    if obj is None:
        return True
    if isinstance(obj, dict):
        if obj.get("success") is False:
            return True
        if obj.get("error"):
            return True
        msg = obj.get("message")
        return isinstance(msg, str) and msg.lstrip().lower().startswith(_ERROR_PREFIXES)
    if isinstance(obj, str):
        return obj.lstrip().lower().startswith(_ERROR_PREFIXES)
    return False


def _parse_result(obj):
    """Return a dict/list for a raw result or a JSON string, else None."""
    if isinstance(obj, (dict, list)):
        return obj
    if isinstance(obj, str) and obj[:1] in ("{", "["):
        try:
            return json.loads(obj)
        except Exception:
            return None
    return None


# ── Human confirmation for destructive actions ───────────────────────
# Destructive tools never run straight from an LLM/agent call.  Instead a
# pending action is stored server-side under a one-time token and the
# daemon asks the user.  Only POST /confirm with that token executes it,
# so a model can't "confirm" on its own by passing extra arguments.

CONFIRM_REQUIRED_TOOLS = frozenset({"delete_file", "deliver_chat_message"})
_CONFIRM_TTL_SEC = 120
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()


def _create_pending(tool: str, args: dict, speech: str) -> dict:
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _PENDING_LOCK:
        for t in [t for t, p in _PENDING.items() if now - p["created"] > _CONFIRM_TTL_SEC]:
            _PENDING.pop(t, None)
        _PENDING[token] = {"tool": tool, "args": dict(args or {}), "created": now}
    return {
        "status": "pending_confirmation",
        "confirm_speech": speech,
        "pending_tool": tool,
        "pending_args": dict(args or {}),
        "confirm_token": token,
        "message": "Waiting for the user to confirm. Do not call this tool again.",
    }


def _describe_action(tool: str, args: dict) -> str:
    if tool == "delete_file":
        path = str(args.get("file_path", "that file"))
        return f"Move {os.path.basename(path.rstrip('/')) or path} to the Trash?"
    if tool == "deliver_chat_message":
        return f"Send '{args.get('text', '')}' to {args.get('contact', 'them')} on {args.get('app', 'the app')}?"
    if tool == "press_keyboard_shortcut":
        if is_risky_shortcut(str(args.get("keys", ""))):
            return f"Press {args.get('keys')}? That can close, quit or delete something."
        return f"Press {args.get('keys')}? That would send what's typed."
    if tool == "click_menu_item":
        return f"In {args.get('app_name', 'the app')}, click {args.get('menu_path', 'that menu item')}?"
    if tool == "run_app_script":
        first = str(args.get("script", "")).strip().splitlines()[0][:80] if args.get("script") else ""
        return f"In {args.get('app_name', 'the app')}, run \"{first}\"? It changes or closes something."
    return f"Run {tool.replace('_', ' ')}?"


def _confirm_and_execute_tool(action_description: str, tool_name: str,
                              tool_args: str = "{}") -> dict:
    """Server-side wrapper for the confirm_and_execute tool."""
    try:
        args = json.loads(tool_args) if isinstance(tool_args, str) else dict(tool_args or {})
    except Exception:
        args = {}
    canon, args = _normalize_tool_call(tool_name or "", args)
    if canon not in TOOL_MAP or canon == "confirm_and_execute":
        return {"success": False, "message": f"[-] Unknown tool to confirm: {tool_name}"}
    return _create_pending(canon, args, action_description or _describe_action(canon, args))


TOOL_MAP["confirm_and_execute"] = _confirm_and_execute_tool

_NATIVE_NAMES = frozenset(TOOL_MAP.keys())

# Tools that drive macOS through AppleScript / Accessibility / screenshots.
# They cannot work when the server runs in a Linux container.
_MACOS_ONLY_MODULES = frozenset({
    "actuators.system_control", "actuators.browser", "actuators.web_agent",
    "sensors.vision", "actuators.communication",
})
_MACOS_ONLY_TOOLS = frozenset({
    "click_ui_element", "open_file", "delete_file", "summarize_selected_pdf",
    "get_system_info", "set_default_browser",
})


def _is_macos_only(name: str, func) -> bool:
    return name in _MACOS_ONLY_TOOLS or getattr(func, "__module__", "") in _MACOS_ONLY_MODULES


def _get_tool_args(func, raw_args: dict) -> dict:
    """Map raw JSON arguments onto the tool's signature with light coercion.
    Raises TypeError naming any missing required argument."""
    sig = inspect.signature(func)
    cleaned = {}
    missing = []
    for param_name, param in sig.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param_name in raw_args and raw_args[param_name] is not None:
            val = raw_args[param_name]
            ann = param.annotation
            default = param.default
            if ann is bool or isinstance(default, bool):
                if isinstance(val, str):
                    val = val.strip().lower() in ("true", "1", "yes")
            elif (ann is int or (isinstance(default, int) and not isinstance(default, bool))) \
                    and isinstance(val, (str, float)):
                try:
                    val = int(float(val))
                except ValueError:
                    pass
            elif (ann is float or isinstance(default, float)) and isinstance(val, (str, int)):
                try:
                    val = float(val)
                except ValueError:
                    pass
            cleaned[param_name] = val
        elif param.default is inspect.Parameter.empty:
            missing.append(param_name)
    if missing:
        raise TypeError(f"missing required argument(s): {', '.join(missing)}")
    return cleaned


def _would_send_in_chat_app(name: str, args: dict) -> bool:
    """Enter / submit while a chat app is in front sends a message to real
    people — outside the read-back flow it needs a spoken yes."""
    keys = str(args.get("keys", "")).lower().replace(" ", "")
    sends = name == "type_and_submit" or (
        name == "press_keyboard_shortcut"
        and (re.search(r"(^|\+|,)(return|enter)$", keys) or keys in ("cmd+shift+d", "command+shift+d")))
    if not sends:
        return False
    try:
        from core.axui import frontmost_app
        from actuators.messaging import is_chat_app
        app = frontmost_app()
        # Mail apps too: ⌘↩ / ⌘⇧D send an email (a live session pressed ⌘↩ in Mail).
        return is_chat_app(app) or app in ("Mail", "Microsoft Outlook", "Outlook", "Spark", "Airmail")
    except Exception:
        return False


def _send_chat_message_tool(app: str = "", contact: str = "", text: str = "") -> dict:
    """Open the chat, verify it, type the exact text, then ask before sending."""
    from actuators.messaging import prepare_chat_message, is_chat_app, CHAT_APPS
    if not app:
        active = str(ACTIVE_CONTEXT.get("active_app", ""))
        running = [a for a in CHAT_APPS if _app_running(a)]
        app = active if is_chat_app(active) else (running[0] if len(running) == 1 else "")
        if not app:
            return {"success": False, "message": f"Which app should I use to message {contact or 'them'}?"}
    if not text:
        return {"success": False, "message": f"What should I say to {contact or 'them'}?"}
    prep = prepare_chat_message(app, contact, text)
    if not prep.get("success"):
        return prep
    args = {"app": prep["app"], "contact": prep["contact"], "text": prep["text"]}
    return _create_pending("deliver_chat_message", args, _describe_action("deliver_chat_message", args))


def _message_contact_tool(request: str) -> dict:
    """Skill entry point: understand "send hi to Hatem on Discord" and run
    the read-back-then-send flow."""
    from actuators.messaging import parse_request
    p = parse_request(request)
    if not p["contact"]:
        return {"success": False, "message": "Who should I message?"}
    return _send_chat_message_tool(p["app"], p["contact"], p["text"])


def _app_running(app: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", app], capture_output=True, timeout=2).returncode == 0
    except Exception:
        return False


TOOL_MAP["send_chat_message"] = _send_chat_message_tool
TOOL_MAP["message_contact"] = _message_contact_tool


def _dispatch_native(name: str, args: dict, *, confirmed: bool = False) -> tuple[str, bool, object]:
    """Run a native tool.  Returns (serialized_result, failed, raw_result).
    Destructive tools return a pending-confirmation request unless
    `confirmed` is True (only set by the /confirm endpoint)."""
    func = TOOL_MAP.get(name)
    if func is None:
        return f"[-] Unknown tool: {name}", True, None

    if sys.platform != "darwin" and _is_macos_only(name, func):
        msg = (f"[-] {name} needs macOS (AppleScript/Accessibility) and is not "
               "available when the tool server runs in Docker/Linux.")
        return msg, True, None

    needs_yes = name in CONFIRM_REQUIRED_TOOLS or _would_send_in_chat_app(name, args) or (
        name == "press_keyboard_shortcut" and is_risky_shortcut(str(args.get("keys", "")))) or (
        name == "run_app_script" and _script_is_destructive(str(args.get("script", "")))) or (
        name == "click_menu_item" and is_risky_menu(str(args.get("menu_path", "")))) or (
        name in ("find_and_click", "click_ui_element")
        and is_risky_menu(str(args.get("search_text") or args.get("element_name") or "")))
    if needs_yes and not confirmed:
        pending = _create_pending(name, args, _describe_action(name, args))
        return _serialize_result(pending), False, pending

    try:
        safe_args = _get_tool_args(func, args)
        raw = func(**safe_args)
    except TypeError as e:
        return f"[-] Bad arguments for {name}: {e}", True, None
    except Exception as e:
        return f"[-] Tool error ({name}): {e}", True, None

    return _serialize_result(raw), _result_failed(raw), raw

# ── Active Context (cross-turn persistent state) ─────────────────────
# Replaces the old SESSION_STATE with richer fields that track what the
# user is doing across turns.  Consumed by /context endpoint and by the
# C++ daemon's planner for continuation detection.

import datetime as _dt
import pathlib as _pathlib

_HOME = str(_pathlib.Path.home())

ACTIVE_CONTEXT = {
    # What's in focus right now
    "active_app": None,
    "active_folder": None,
    "active_file": None,
    "active_url": None,

    # Last tool execution details (for result passing between turns)
    "last_tool": None,
    "last_tool_args": None,
    "last_tool_result": None,
    "last_action_summary": None,
    "last_failed_action": None,

    # Pending state — enables continuation commands
    "pending_goal": None,
    "pending_clarification": None,   # e.g. "Which file did you mean?"
    "pending_file_choices": None,     # list of {name, path} from find_file multi-match
    "awaiting_user_choice": False,    # if True, next turn is a continuation

    # Previous exchange — lets agents resolve follow-ups ("show that elsewhere")
    "last_command": None,
    "last_reply": None,

    # Pre-resolved real paths
    "home_dir": _HOME,
    "downloads_dir": str(_pathlib.Path.home() / "Downloads"),
    "documents_dir": str(_pathlib.Path.home() / "Documents"),
    "desktop_dir": str(_pathlib.Path.home() / "Desktop"),

    # Bookkeeping
    "session_start": _dt.datetime.now().isoformat(),
    "turn_count": 0,
    "last_updated": None,
}


# Read-only bookkeeping tools the daemon calls on its own every turn —
# they must not overwrite "what the user last did".
_CONTEXT_SILENT_TOOLS = frozenset({
    "retrieve_memories", "get_active_window_tree", "list_open_windows",
})


def _update_context(tool_name: str, args: dict, result, success: bool):
    """Auto-update ACTIVE_CONTEXT after every tool call.
    Tracks app/folder/file/url, detects multi-match file results,
    and sets pending state for continuation detection.

    `result` may be the raw tool return value (dict/str) or its JSON text."""
    if tool_name in _CONTEXT_SILENT_TOOLS:
        return

    now = _dt.datetime.now().isoformat()
    ACTIVE_CONTEXT["last_updated"] = now
    ACTIVE_CONTEXT["turn_count"] += 1

    # Always record what happened
    ACTIVE_CONTEXT["last_tool"] = tool_name
    ACTIVE_CONTEXT["last_tool_args"] = args

    result_text = _serialize_result(result)[:500]
    if not success or _result_failed(result):
        ACTIVE_CONTEXT["last_failed_action"] = f"{tool_name}({args})"
        ACTIVE_CONTEXT["last_tool_result"] = result_text
        return

    ACTIVE_CONTEXT["last_tool_result"] = result_text
    ACTIVE_CONTEXT["last_action_summary"] = f"{tool_name}({args})"

    # ── App tracking ──
    if tool_name in ("open_application", "activate_application"):
        ACTIVE_CONTEXT["active_app"] = args.get("app_name", None)
    elif tool_name == "quit_application":
        if ACTIVE_CONTEXT["active_app"] == args.get("app_name"):
            ACTIVE_CONTEXT["active_app"] = None
    elif tool_name == "open_url" or tool_name == "open_url_and_wait":
        browser_name = str(args.get("browser", "") or "").strip()
        if not browser_name:
            try:
                prefs = get_all_preferences()
                browser_name = str(prefs.get("default_browser", "") or "").strip()
            except Exception:
                browser_name = ""
        ACTIVE_CONTEXT["active_app"] = browser_name or ACTIVE_CONTEXT.get("active_app") or "Browser"
        ACTIVE_CONTEXT["active_url"] = args.get("url", None)

    # ── Browser tool tracking ──
    elif tool_name in ("browser_navigate", "browser_navigate_and_wait", "browser_new_tab"):
        ACTIVE_CONTEXT["active_app"] = "Brave Browser"
        ACTIVE_CONTEXT["active_url"] = args.get("url", ACTIVE_CONTEXT.get("active_url"))
    elif tool_name.startswith("browser_") or tool_name.startswith("gmail_"):
        ACTIVE_CONTEXT["active_app"] = "Brave Browser"
    elif tool_name in ("web_search", "web_read_article", "web_analyze_page",
                       "web_youtube_search", "web_ecommerce_search"):
        ACTIVE_CONTEXT["active_app"] = "Brave Browser"
        url_arg = args.get("url", args.get("query", ""))
        if url_arg and url_arg.startswith("http"):
            ACTIVE_CONTEXT["active_url"] = url_arg

    # ── Keyboard shortcuts → folder tracking ──
    elif tool_name == "press_keyboard_shortcut":
        keys = args.get("keys", "")
        if keys == "command+option+l":
            ACTIVE_CONTEXT["active_folder"] = ACTIVE_CONTEXT["downloads_dir"]
            ACTIVE_CONTEXT["active_app"] = "Finder"
        elif keys == "command+shift+o":
            ACTIVE_CONTEXT["active_folder"] = ACTIVE_CONTEXT["documents_dir"]
            ACTIVE_CONTEXT["active_app"] = "Finder"
        elif keys == "command+shift+d":
            ACTIVE_CONTEXT["active_folder"] = ACTIVE_CONTEXT["desktop_dir"]
            ACTIVE_CONTEXT["active_app"] = "Finder"

    # ── find_file → detect multi-match for continuation ──
    elif tool_name == "find_file":
        try:
            parsed = _parse_result(result)
            if isinstance(parsed, dict):
                matches = parsed.get("matches", [])
                count = parsed.get("count", len(matches))
                folder = args.get("folder", "")
                if folder:
                    ACTIVE_CONTEXT["active_folder"] = str(resolve_safe_path(folder))

                if count == 1 and matches:
                    # Single match — store the file
                    m = matches[0]
                    ACTIVE_CONTEXT["active_file"] = m.get("path", m.get("name", ""))
                    ACTIVE_CONTEXT["awaiting_user_choice"] = False
                    ACTIVE_CONTEXT["pending_file_choices"] = None
                elif count > 1:
                    # Multi-match — set up continuation state
                    ACTIVE_CONTEXT["pending_file_choices"] = [
                        {"name": m.get("name", "?"), "path": m.get("path", "?")}
                        for m in matches[:5]
                    ]
                    choice_names = [m.get("name", "?") for m in matches[:5]]
                    ACTIVE_CONTEXT["pending_clarification"] = (
                        f"Found {count} files: {', '.join(choice_names)}. Which one?"
                    )
                    ACTIVE_CONTEXT["awaiting_user_choice"] = True
                else:
                    ACTIVE_CONTEXT["awaiting_user_choice"] = False
        except Exception:
            pass

    # ── open_file → clear pending, set active file ──
    elif tool_name == "open_file":
        fpath = args.get("file_path", "")
        ACTIVE_CONTEXT["active_file"] = fpath
        ACTIVE_CONTEXT["last_action_summary"] = f"opened file {fpath}"
        ACTIVE_CONTEXT["awaiting_user_choice"] = False
        ACTIVE_CONTEXT["pending_file_choices"] = None
        ACTIVE_CONTEXT["pending_clarification"] = None

    # ── search_web / smart_search → store URL context ──
    elif tool_name in ("search_web", "read_webpage", "smart_search"):
        q = args.get("query", args.get("url", ""))
        ACTIVE_CONTEXT["last_action_summary"] = f"searched: {q}"


def _context_block() -> str:
    """Format ACTIVE_CONTEXT as a compact text block for LLM injection.
    Includes pre-resolved home directory paths to prevent username hallucination."""
    parts = [
        "CURRENT CONTEXT:",
        f"- Home directory: {ACTIVE_CONTEXT['home_dir']}",
        f"- Downloads: {ACTIVE_CONTEXT['downloads_dir']}",
        f"- Documents: {ACTIVE_CONTEXT['documents_dir']}",
        f"- Desktop: {ACTIVE_CONTEXT['desktop_dir']}",
        "- ALWAYS use ~/Downloads, ~/Documents, ~/Desktop for file paths. NEVER construct paths with a username.",
    ]
    if ACTIVE_CONTEXT["active_app"]:
        parts.append(f"- Active app: {ACTIVE_CONTEXT['active_app']}")
    if ACTIVE_CONTEXT["active_folder"]:
        parts.append(f"- Active folder: {ACTIVE_CONTEXT['active_folder']}")
    if ACTIVE_CONTEXT["active_file"]:
        parts.append(f"- Active file: {ACTIVE_CONTEXT['active_file']}")
    if ACTIVE_CONTEXT["active_url"]:
        parts.append(f"- Active URL: {ACTIVE_CONTEXT['active_url']}")
    if ACTIVE_CONTEXT["last_action_summary"]:
        parts.append(f"- Last action: {ACTIVE_CONTEXT['last_action_summary']}")
    if ACTIVE_CONTEXT["last_failed_action"]:
        parts.append(f"- Last failed: {ACTIVE_CONTEXT['last_failed_action']}")
    if ACTIVE_CONTEXT["pending_goal"]:
        parts.append(f"- Pending goal: {ACTIVE_CONTEXT['pending_goal']}")
    if ACTIVE_CONTEXT["awaiting_user_choice"] and ACTIVE_CONTEXT["pending_clarification"]:
        parts.append(f"- AWAITING USER CHOICE: {ACTIVE_CONTEXT['pending_clarification']}")
    if ACTIVE_CONTEXT["pending_file_choices"]:
        names = [c["name"] for c in ACTIVE_CONTEXT["pending_file_choices"][:3]]
        parts.append(f"- File choices: {', '.join(names)}")
    return "\n".join(parts)


_TLDS = r"(?:com|org|net|edu|gov|io|ai|co|in|uk|us|dev|app|me|info|ac)"
# A spoken/ASR-mangled domain: "lms . bennett . edu . in", "google dot com".
_SPOKEN_DOMAIN_RE = re.compile(
    rf"\b[a-z0-9-]+(?:\s*\.\s*[a-z0-9-]+)*\s*\.\s*{_TLDS}\b", re.IGNORECASE
)


def _normalize_user_command(command: str) -> str:
    """Lightweight autocorrection for noisy speech-to-text command text.

    Only touches explicit ASR artifacts (spoken domains, whole-utterance
    repeats).  Case is preserved so names, email subjects and file names
    reach the tools exactly as spoken."""
    text = str(command or "").strip()
    if not text:
        return text

    original = text

    # "double u" style spelling → letters ("double t" → "tt")
    text = re.sub(r"\bdouble\s+([a-z])\b", lambda m: m.group(1) * 2, text, flags=re.IGNORECASE)
    # "google dot com" → "google.com"
    text = re.sub(
        rf"(?<=[a-z0-9])\s+dot\s+(?=(?:{_TLDS}\b|[a-z0-9-]+\s+dot\s))", ".",
        text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\badu\b(?=\s*\.)", "edu", text, flags=re.IGNORECASE)
    # Fix common TLD truncations after speech recognition ("edu . n")
    text = re.sub(r"\bedu\s*\.\s*(?:n|i)\b", "edu.in", text, flags=re.IGNORECASE)

    # Collapse spaces around dots ONLY inside something that looks like a
    # domain.  A sentence break ("Safari. Then …") is left untouched.
    def _join_domain(m: re.Match) -> str:
        span = m.group(0)
        if re.search(r"\.\s+[A-Z]", span):   # ". Capital" = new sentence
            return span
        return re.sub(r"\s*\.\s*", ".", span)

    text = _SPOKEN_DOMAIN_RE.sub(_join_domain, text)

    # Canonical LMS domain correction for common recognition variants
    low = text.lower()
    if "lms" in low and any(tok in low for tok in (
        "bennit", "benit", "bennet", "benned", "bewewd", "penit", "paneth", "bennett"
    )):
        text = re.sub(r"lms\.[a-z0-9.-]+", "lms.bennett.edu.in", text, flags=re.IGNORECASE)

    # ASR sometimes emits the whole utterance twice:
    # "start my study routine start my study routine" → once.
    # Only an exact whole-utterance repeat is collapsed, never inner text.
    m = re.fullmatch(r"(.{5,200}?)[\s,.]+\1[.!?]?", text, flags=re.IGNORECASE)
    if m:
        text = m.group(1)

    # Study routine should map clearly to workflow behavior
    if re.search(r"\bstudy routine\b", text, re.IGNORECASE) and "workflow" not in text.lower():
        text = text + " workflow"

    # Names the user taught / apps DEMASCAS learned ("this cord" → "Discord"),
    # and DEMASCAS's own name (heard as "Damascus").
    try:
        from core.vocabulary import apply_corrections
        text = apply_corrections(text)
    except Exception:
        pass

    fixed = text.strip()
    if fixed != original:
        print(f"[AUTOCORRECT] '{original}' -> '{fixed}'")
    return fixed

# ── Tool alias normalization — tolerate common LLM hallucinated names ──

TOOL_ALIASES = {
    "click": "click_ui_element",
    "click_element": "click_ui_element",
    "click_ui": "click_ui_element",
    "close_button": "click_ui_element",
    "type": "type_text",
    "type_text_in": "type_text_into_element",
    "find_element": "find_and_click",
    "switch_application": "activate_application",
    "activate_app": "activate_application",
    "open_app": "open_application",
    "press_key": "press_keyboard_shortcut",
    "press_key_shortcut": "press_keyboard_shortcut",
    "send_shortcut": "press_keyboard_shortcut",
    "open_website": "open_url",
    "go_to_url": "open_url",
    # ── Communication aliases ────────────────────────────────────
    "read_email": "read_current_email",
    "get_email": "read_current_email",
    "write_email": "compose_email",
    "draft_email": "compose_email",
    "send_email": "compose_email",
    "reply": "smart_reply",
    "reply_email": "smart_reply",
    "respond": "smart_reply",
    "confirm": "confirm_and_execute",
    "research": "web_research_and_report",
    "web_research": "web_research_and_report",
    "get_contact": "extract_contact",
    "search_file": "find_file",
    "search_files": "find_file",
    "locate_file": "find_file",
    "find_files": "find_file",
    # ── Browser aliases ──────────────────────────────────────────
    "navigate": "browser_navigate",
    "go_to": "browser_navigate",
    "browse": "browser_navigate",
    "open_tab": "browser_new_tab",
    "new_tab": "browser_new_tab",
    "close_tab": "browser_close_tab",
    "page_text": "browser_get_page_text",
    "get_page": "browser_get_page_text",
    "click_link": "browser_click_text",
    "click_button": "browser_click_text",
    "fill_form": "browser_fill_form",
    "dom_summary": "browser_get_dom_summary",
    "compose_draft": "gmail_compose_draft",
    "gmail_draft": "gmail_compose_draft",
    # ── Web agent aliases ────────────────────────────────────────
    "analyze_page": "web_analyze_page",
    "scan_obstacles": "web_scan_for_obstacles",
    "handle_obstacle": "web_handle_obstacle",
    "search_google": "web_search",
    "google": "web_search",
    "read_article": "web_read_article",
    "youtube_search": "web_youtube_search",
    "play_youtube": "web_youtube_play_first",
    "smart_click": "web_smart_click",
}


def _normalize_tool_call(name: str, args: dict) -> tuple[str, dict]:
    """Map alias tool names/arguments into canonical native tool calls."""
    name = (name or "").strip()
    # Real tool names always win — an alias must never hijack a real tool
    # (e.g. "web_research" used to be rewritten to web_research_and_report).
    canonical = name if name in TOOL_MAP else TOOL_ALIASES.get(name.lower(), name)
    normalized = dict(args or {})

    # Generic noisy fields sometimes emitted by the model
    normalized.pop("window_title", None)
    normalized.pop("args", None)

    # Generic aliases must never rename a parameter the tool really has
    # (e.g. "key" → "keys" stole set_preference's `key` argument).
    target = TOOL_MAP.get(canonical)
    try:
        real_params = set(inspect.signature(target).parameters) if target else set()
    except (TypeError, ValueError):
        real_params = set()
    stash = {k: normalized.pop(k) for k in list(normalized) if k in real_params}

    # Common argument aliases
    if "app" in normalized and "app_name" not in normalized:
        normalized["app_name"] = normalized.pop("app")
    if "application" in normalized and "app_name" not in normalized:
        normalized["app_name"] = normalized.pop("application")
    if "shortcut" in normalized and "keys" not in normalized:
        normalized["keys"] = normalized.pop("shortcut")
    if "key" in normalized and "keys" not in normalized:
        normalized["keys"] = normalized.pop("key")

    if canonical in {"click_ui_element", "right_click_element", "scroll_to_element"}:
        if "element" in normalized and "element_name" not in normalized:
            normalized["element_name"] = normalized.pop("element")
        if "target" in normalized and "element_name" not in normalized:
            normalized["element_name"] = normalized.pop("target")
        if "button" in normalized and "element_name" not in normalized:
            normalized["element_name"] = normalized.pop("button")

    if canonical == "find_and_click":
        if "element_name" in normalized and "search_text" not in normalized:
            normalized["search_text"] = normalized.pop("element_name")
        if "element" in normalized and "search_text" not in normalized:
            normalized["search_text"] = normalized.pop("element")
        if "target" in normalized and "search_text" not in normalized:
            normalized["search_text"] = normalized.pop("target")

    if canonical == "type_text_into_element":
        if "element" in normalized and "element_name" not in normalized:
            normalized["element_name"] = normalized.pop("element")
        if "text" in normalized and "text_input" not in normalized:
            normalized["text_input"] = normalized.pop("text")

    if canonical in {"type_text", "type_and_submit"}:
        if "query" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("query")
        if "content" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("content")
        if "message" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("message")

    if canonical in {"open_url", "open_url_and_wait"}:
        if "website" in normalized and "url" not in normalized:
            normalized["url"] = normalized.pop("website")
        if "link" in normalized and "url" not in normalized:
            normalized["url"] = normalized.pop("link")
        if "app_name" in normalized and "browser" not in normalized:
            normalized["browser"] = normalized.pop("app_name")
        if "app" in normalized and "browser" not in normalized:
            normalized["browser"] = normalized.pop("app")

    if canonical == "compose_email":
        key_lookup = {k.lower(): k for k in list(normalized.keys())}

        def _move_aliases(target_key: str, aliases: tuple[str, ...]) -> None:
            if target_key in normalized:
                return
            for alias in aliases:
                src_key = key_lookup.get(alias.lower())
                if src_key and src_key in normalized:
                    normalized[target_key] = normalized.pop(src_key)
                    break

        _move_aliases("recipient", ("to", "email", "to_address", "address", "toemail"))
        _move_aliases("subject", ("title",))
        _move_aliases("key_points", ("body", "message", "content", "text", "instructions"))
        _move_aliases("tone", ("style",))

    # ── Browser tool argument normalization ──
    if canonical in ("browser_navigate", "browser_navigate_and_wait", "browser_new_tab"):
        if "website" in normalized and "url" not in normalized:
            normalized["url"] = normalized.pop("website")
        if "link" in normalized and "url" not in normalized:
            normalized["url"] = normalized.pop("link")

    if canonical in ("browser_click", "browser_wait_for_element", "browser_element_exists",
                     "browser_scroll_to_element"):
        for alias in ("element", "target", "element_name", "name", "description",
                       "button", "label"):
            if alias in normalized and "selector" not in normalized:
                normalized["selector"] = normalized.pop(alias)
                break

    if canonical == "browser_click_text":
        for alias in ("element", "label", "button", "element_name", "name",
                       "selector", "description", "target"):
            if alias in normalized and "text" not in normalized:
                normalized["text"] = normalized.pop(alias)
                break

    if canonical == "browser_type":
        for alias in ("element", "element_name", "target", "field", "input_selector"):
            if alias in normalized and "selector" not in normalized:
                normalized["selector"] = normalized.pop(alias)
                break
        if "value" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("value")
        if "input" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("input")
        if "content" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("content")

    if canonical == "browser_type_by_label":
        for alias in ("label", "field", "element_name", "name", "element"):
            if alias in normalized and "label_or_placeholder" not in normalized:
                normalized["label_or_placeholder"] = normalized.pop(alias)
                break
        if "value" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("value")
        if "content" in normalized and "text" not in normalized:
            normalized["text"] = normalized.pop("content")

    if canonical in ("web_search", "web_youtube_search"):
        if "search" in normalized and "query" not in normalized:
            normalized["query"] = normalized.pop("search")
        if "term" in normalized and "query" not in normalized:
            normalized["query"] = normalized.pop("term")

    if canonical == "web_smart_click":
        for alias in ("element", "target", "text", "name", "element_name",
                       "selector", "button", "label", "description"):
            if alias in normalized and "element_description" not in normalized:
                normalized["element_description"] = normalized.pop(alias)
                break

    if canonical == "save_memory":
        for alias in ("memory", "content", "fact", "info", "note", "value", "message"):
            if alias in normalized and "text" not in normalized and "text" not in stash:
                normalized["text"] = normalized.pop(alias)
                break

    if canonical == "set_preference":
        for alias in ("name", "preference", "setting"):
            if alias in normalized and "key" not in normalized and "key" not in stash:
                normalized["key"] = normalized.pop(alias)
                break

    if canonical == "gmail_compose_draft":
        # Accept common aliases for recipient
        for alias in ("recipient", "email", "to_address", "address"):
            if alias in normalized and "to" not in normalized:
                normalized["to"] = normalized.pop(alias)
                break
        if "message" in normalized and "body" not in normalized:
            normalized["body"] = normalized.pop("message")
        if "content" in normalized and "body" not in normalized:
            normalized["body"] = normalized.pop("content")

    normalized.update(stash)   # real parameters always win
    return canonical, normalized

# ── UI-mutating tools — auto-capture screen state after execution ────

UI_MUTATING_TOOLS = frozenset({
    "click_ui_element", "type_text", "type_text_into_element",
    "open_application", "open_url", "open_url_and_wait",
    "press_keyboard_shortcut",
    "scroll_direction", "drag_element", "find_and_click",
    "right_click_element", "type_and_submit", "switch_tab",
    "scroll_to_element", "quit_application", "activate_application",
    "vision_execute",
    # Browser tools that change page state
    "browser_navigate", "browser_navigate_and_wait",
    "browser_new_tab", "browser_close_tab",
    "browser_back", "browser_forward", "browser_reload",
    "browser_click", "browser_click_text", "browser_click_xpath",
    "browser_type", "browser_type_by_label", "browser_press_key",
    "browser_scroll", "browser_scroll_to_element",
    "browser_fill_form", "browser_select_option",
    "browser_check_checkbox", "browser_submit_form",
    "gmail_compose_draft",
    "web_execute_goal", "web_smart_click", "web_handle_obstacle",
    "web_youtube_search", "web_youtube_play_first",
    "web_ecommerce_search",
})

# ── Flask app ────────────────────────────────────────────────────────

app = Flask(__name__)

# ── Request guard ────────────────────────────────────────────────────
# The tool server can click, type, delete files and run commands, so it
# must only be reachable by the local daemon and agents — never by a web
# page.  Browsers always send an Origin (or Sec-Fetch-Site) header on
# cross-site requests; libcurl and python-requests never do.  The Host
# check defeats DNS-rebinding attacks.

_DEFAULT_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _allowed_hosts() -> set[str]:
    extra = os.getenv("DEMASCAS_ALLOWED_HOSTS", "")
    return _DEFAULT_ALLOWED_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


_USER_ACTIVITY_PATHS = ("/skill/match", "/agent", "/tool", "/execute", "/feedback")


@app.before_request
def _note_user_activity():
    """A command is arriving → background fine-tuning must yield NOW."""
    if request.path.startswith(_USER_ACTIVITY_PATHS):
        try:
            from core.finetune.scheduler import note_activity
            note_activity()
        except Exception:
            pass
        try:
            from core.web.crawler import note_activity as web_activity
            web_activity()               # background web research yields too
        except Exception:
            pass


@app.before_request
def _guard_request():
    host = (request.host or "").lower()
    hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if hostname not in _allowed_hosts():
        return jsonify({"error": f"Host '{host}' not allowed"}), 403
    if request.headers.get("Origin") or request.headers.get("Sec-Fetch-Site"):
        return jsonify({"error": "Browser requests are not allowed"}), 403
    return None

# ── MCP singleton — booted once at server start ─────────────────────

_mcp: MCPManager | None = None


def _server_host() -> str:
    return os.getenv("DEMASCAS_HOST", "127.0.0.1")


def _server_port() -> int:
    return int(os.getenv("DEMASCAS_PORT", "5001"))


def _should_boot_mcp() -> bool:
    return os.getenv("DEMASCAS_SKIP_MCP_BOOT", "").strip().lower() not in {"1", "true", "yes", "on"}


def _boot_mcp() -> MCPManager:
    """Boot MCP servers.  Called once at startup.  Non-fatal on failure."""
    global _mcp
    _mcp = get_manager()
    try:
        n = _mcp.boot_all()
        if n > 0:
            print(_mcp.status_summary())
    except Exception as e:
        print(f"[!] MCP boot failed (non-fatal): {e}")
    return _mcp


def _shutdown_mcp() -> None:
    """Registered via atexit — reclaims every MCP subprocess."""
    if _mcp is not None:
        try:
            _mcp.shutdown_all()
        except Exception:
            pass


# ── Unified tool list (native + MCP, no name collisions) ────────────

def _all_tool_schemas() -> list[dict]:
    """
    Merge NATIVE_TOOL_SCHEMAS + MCP Ollama schemas.
    Native wins on name collision (same rule as mcp/bridge.py).
    """
    if _mcp is None:
        return NATIVE_TOOL_SCHEMAS

    mcp_tools = _mcp.get_all_tools_ollama()
    if not mcp_tools:
        return NATIVE_TOOL_SCHEMAS

    # Filter collisions — native always wins (Rule 1: fast path stays native)
    filtered = [t for t in mcp_tools if t["function"]["name"] not in _NATIVE_NAMES]
    return NATIVE_TOOL_SCHEMAS + filtered


# ── Endpoints ────────────────────────────────────────────────────────

@app.route("/tool", methods=["POST"])
def execute_tool():
    """Execute a named tool — routes to native or MCP automatically.
    For UI-mutating tools, also captures screen state after execution.

    ALWAYS returns HTTP 200 with JSON — the C++ bridge expects this.
    Error info goes into the 'result' field with [-] prefix so the
    LLM's ReAct loop can detect and recover."""
    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"result": "[-] Missing 'name' in request body."}), 200

    name = data["name"]
    args = data.get("arguments", {})
    if not isinstance(args, dict):
        args = {}
    name, args = _normalize_tool_call(name, args)

    # Priority 1: native (in-process, ~0 µs overhead)
    if name in TOOL_MAP:
        result, failed, raw = _run_and_track(name, args)
        response = {"result": result, "success": not failed}

        # Screen state after UI actions costs a full accessibility-tree read
        # (~1-2 s), so it is only done when the caller asks for it (the
        # daemon's ReAct loop does, for actions whose follow-up needs element
        # names).  Agents never ask, so their UI steps stay fast.
        if (data.get("include_screen") and name in UI_MUTATING_TOOLS
                and not failed and sys.platform == "darwin"):
            try:
                time.sleep(0.3)  # let UI settle
                screen = get_active_window_tree()
                if screen and len(screen) < 5000:  # don't bloat response
                    response["screen_state"] = screen
            except Exception:
                pass  # non-fatal

        return jsonify(response), 200

    # Priority 2: MCP (JSON-RPC over stdio, ~5-10 ms)
    if _mcp is not None and _mcp.is_mcp_tool(name):
        try:
            result = _serialize_result(_mcp.call_tool(name, args))
            return jsonify({"result": result, "success": not _result_failed(result)}), 200
        except Exception as e:
            return jsonify({"result": f"[-] MCP tool error ({name}): {e}", "success": False}), 200

    return jsonify({"result": f"[-] Unknown tool: {name}. Use get_active_window_tree to read the screen.",
                    "success": False}), 200


def _run_and_track(name: str, args: dict, *, confirmed: bool = False) -> tuple[str, bool, object]:
    """Dispatch a native tool, then update active context + auto-learning."""
    result, failed, raw = _dispatch_native(name, args, confirmed=confirmed)
    _update_context(name, args, raw if raw is not None else result, success=not failed)
    if name == "open_application" and (failed or str(result).startswith("Failed")):
        _LAST_UNRESOLVED.update(name=str(args.get("app_name", "")), ts=time.time())
    try:
        from core.skills.trajectories import note_tool_call
        note_tool_call(name, args, result, not failed)
    except Exception:
        pass
    if not failed:
        try:
            from core.knowledge import auto_save_from_tool_result
            auto_save_from_tool_result(name, args, raw if raw is not None else result, success=True)
        except Exception:
            pass
    return result, failed, raw


# ── Skills: learned procedures (fast path before agents) ─────────────

_PENDING_SKILL: dict = {}       # skill waiting for a missing parameter
_LAST_UNRESOLVED: dict = {}     # last app name that couldn't be opened (for "I meant X")


@app.route("/vocabulary", methods=["GET"])
def vocabulary_endpoint():
    """Words the speech recogniser should expect (taught words, learned apps)."""
    from core.vocabulary import terms
    return jsonify({"terms": terms()}), 200


def _skill_dispatch(name: str, args: dict):
    """Dispatcher used by skills.  "__command__" steps (taught skills) run a
    whole sub-command: another skill if one matches, else the agents."""
    if name == "__command__":
        sub = str(args.get("command", ""))
        from core.skills import runner
        m = runner.match(sub)
        if m and not m.missing and m.skill.id != "__teach__":
            r = runner.run(m.skill, m.params, _skill_dispatch)
            return r.summary, not r.success, {"message": r.summary}
        if _AGENTS_AVAILABLE:
            ar = run_agent(sub, dict(ACTIVE_CONTEXT))
            return ar.summary, not ar.success, {"message": ar.summary}
        return f"[-] Can't run: {sub}", True, None
    canon, cargs = _normalize_tool_call(name, args or {})
    return _run_and_track(canon, cargs)


@app.route("/skill/match", methods=["POST"])
def skill_match_endpoint():
    """Find a learned/built-in skill for a command (and fill its params).
    Response: {matched, skill_id, name, params, ack, ask, done, summary}"""
    data = request.get_json(silent=True) or {}
    raw_command = str(data.get("command", "")).strip()
    command = _normalize_user_command(raw_command)
    if not command:
        return jsonify({"matched": False}), 200
    from core.skills import induce, runner

    # "When I say X, do Y and Z" → learn it right now (raw text: the
    # autocorrector's rewrites must not leak into the taught steps).
    if induce.parse_teaching(raw_command):
        sk = induce.teach(raw_command)
        if sk:
            n = len(sk.steps)
            return jsonify({"matched": True, "done": True, "skill_id": sk.id, "name": sk.name,
                            "summary": f"Got it. When you say {sk.name}, I'll do those "
                                       f"{n} thing{'s' if n != 1 else ''}."}), 200

    # "It's spelled DISCORD" / "I meant Discord" → learn the word now and
    # map the last misheard name to it.  If the command also asks for
    # something ("open Discord, spell it DISCORD"), carry on with that.
    note = ""
    from core import vocabulary
    taught = vocabulary.parse_teaching(raw_command)
    if taught:
        vocabulary.add_term(taught)
        heard = _LAST_UNRESOLVED.get("name", "")
        if heard and time.time() - _LAST_UNRESOLVED.get("ts", 0) < 600:
            vocabulary.add_correction(heard, taught)
            _LAST_UNRESOLVED.clear()
        note = f"Got it — {taught}. I'll remember that."
        command = vocabulary.apply_corrections(command)
        rest = re.sub(r"(?i)[,;]?\s*(?:and\s+)?(?:please\s+)?(?:spell(?:ed|t)?\b.*|it'?s\s+(?:spelled|spelt|called)\b.*|"
                      r"i\s+meant\b.*|(?:and\s+)?i\s+want\s+you\s+to\s+remember\b.*|remember\s+(?:it|that|this)\b.*)$",
                      "", command).strip(" ,.")
        if len(rest.split()) < 2:     # nothing else asked
            return jsonify({"matched": True, "done": True, "skill_id": "vocabulary",
                            "name": "Learn a word", "summary": note, "command": command}), 200
        command = rest

    m = runner.match(command)

    # Answer to a question we asked for a missing parameter?
    if m is None and _PENDING_SKILL and time.time() - _PENDING_SKILL.get("ts", 0) < 60:
        pend = dict(_PENDING_SKILL)
        _PENDING_SKILL.clear()
        from core.skills.store import get_store
        sk = get_store().skills.get(pend["skill_id"])
        if sk:
            value = re.sub(r"^(?:it'?s\s+|about\s+|on\s+|for\s+|the\s+topic\s+is\s+)", "",
                           command, flags=re.I).strip(" .")
            params = dict(pend["params"])
            params[pend["missing"][0]] = value
            missing = [k for k in pend["missing"][1:] if k not in params]
            m = runner.SkillMatch(sk, 1.0, params, missing)
    if m is None:
        # No skill: say where the command should go (chat vs action) so the
        # daemon never answers an action request with small talk.
        try:
            from core.routing import route
            r = route(command)
        except Exception:
            r = ""
        # What DEMASCAS has read on the web about this (≈1 ms, in memory) —
        # handed to whichever path answers, so the chat model stops guessing.
        try:
            from core.web.store import context_block
            knowledge = context_block(command, max_chars=800, k=3)
        except Exception:
            knowledge = ""
        ask = ""
        if r == "unclear":
            from core.routing import clarify
            ask = clarify(command) or "Sorry, I didn't catch that. Could you say it again?"
        return jsonify({"matched": False, "route": r, "command": command, "note": note,
                        "knowledge": knowledge, "ask": ask}), 200

    if m.missing:
        _PENDING_SKILL.update(skill_id=m.skill.id, params=m.params, missing=m.missing, ts=time.time())
        spec = m.skill.params[m.missing[0]]
        ask = spec.get("ask") or f"Sure — {spec.get('description', m.missing[0])}?"
        return jsonify({"matched": True, "skill_id": m.skill.id, "name": m.skill.name,
                        "ask": ask, "command": command}), 200
    ack = runner.say_fmt(m.skill.ack, m.params) if m.skill.ack else ""
    return jsonify({"matched": True, "skill_id": m.skill.id, "name": m.skill.name,
                    "score": round(m.score, 3), "params": m.params, "ack": ack,
                    "command": command}), 200


@app.route("/skill/run", methods=["POST"])
def skill_run_endpoint():
    data = request.get_json(silent=True) or {}
    from core.skills import runner
    from core.skills.store import get_store
    sk = get_store().skills.get(str(data.get("skill_id", "")))
    if sk is None:
        return jsonify({"success": False, "summary": "I don't know that skill."}), 200
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    r = runner.run(sk, params, _skill_dispatch)
    out = {"success": r.success, "summary": r.summary, "tool_calls": r.tool_calls, "ms": round(r.ms)}
    if r.pending:
        out.update(confirm_token=r.pending.get("confirm_token", ""),
                   confirm_speech=r.pending.get("confirm_speech", ""),
                   pending_tool=r.pending.get("pending_tool", ""))
    return jsonify(out), 200


@app.route("/feedback", methods=["POST"])
def feedback_endpoint():
    """User judged the previous result ("good job" / "that's wrong")."""
    data = request.get_json(silent=True) or {}
    positive = bool(data.get("positive"))
    from core.skills import trajectories, runner
    t = trajectories.set_feedback(positive)
    if t and positive and not t.get("skill_id"):
        # Praise is the strongest signal: learn this run now (skipping the
        # two-sightings rule), if it's safe skill material.
        try:
            from core.skills import induce
            induce.induce_from_trajectory(t, set(TOOL_MAP), praised=True)
        except Exception as e:
            print(f"[LEARN] praise-induction skipped: {e}")
    if t and t.get("skill_id"):
        if positive:
            from core.skills.store import get_store
            sk = get_store().skills.get(t["skill_id"])
            if sk:
                sk.stats["verified_reuses"] = sk.stats.get("verified_reuses", 0) + 1
                get_store().save(sk)
        else:
            runner.penalize(t["skill_id"])
    return jsonify({"status": "ok", "applied_to": (t or {}).get("command", "")}), 200


@app.route("/confirm", methods=["POST"])
def confirm_endpoint():
    """Resolve a pending confirmation created by a destructive tool.
    Body: {"token": "...", "approve": true|false}"""
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", ""))
    approve = bool(data.get("approve", False))
    with _PENDING_LOCK:
        pending = _PENDING.pop(token, None)
    if pending is None or time.time() - pending["created"] > _CONFIRM_TTL_SEC:
        return jsonify({"result": "[-] That confirmation has expired.", "success": False}), 200
    if not approve:
        if pending["tool"] == "deliver_chat_message":
            # "No" to a chat message: don't leave the typed draft behind.
            from actuators.messaging import discard_chat_draft
            r = discard_chat_draft(**{k: pending["args"].get(k, "") for k in ("app", "contact", "text")})
            return jsonify({"result": r["message"], "success": True}), 200
        return jsonify({"result": "Cancelled.", "success": True}), 200
    result, failed, _ = _run_and_track(pending["tool"], pending["args"], confirmed=True)
    return jsonify({"result": result, "success": not failed}), 200


@app.route("/tools", methods=["GET"])
def get_tool_schemas():
    """Return ALL tool schemas (native + MCP) in Ollama format."""
    return jsonify(_all_tool_schemas())


# The tools nearly every command needs — listed in the CACHED part of the
# daemon's tool prompt.  Fixed order: any change here invalidates the cache.
CORE_TOOLS = ("open_application", "quit_application", "open_url", "search_web",
              "press_keyboard_shortcut", "type_text", "find_and_click", "get_active_window_tree",
              "click_menu_item", "find_file", "open_file", "save_memory", "set_preference",
              "retrieve_memories", "research_web")


@app.route("/tools_core", methods=["GET"])
def tools_core_endpoint():
    by_name = {s["function"]["name"]: s for s in _all_tool_schemas() if "function" in s}
    return jsonify([by_name[n] for n in CORE_TOOLS if n in by_name])


@app.route("/tools_for_query", methods=["POST"])
def get_filtered_tools():
    """Return only the most relevant tool schemas for a given query.
    Body: {"query": "click the save button", "max_tools": 8}
    Returns: filtered list of Ollama-format tool schemas (3-8 tools).
    """
    data = request.get_json(silent=True)
    if not data or "query" not in data:
        return jsonify(_all_tool_schemas())  # fallback to all

    query = _normalize_user_command(data["query"])
    max_tools = data.get("max_tools", 8)
    all_schemas = _all_tool_schemas()
    filtered = select_tools_for_query(
        query, all_schemas, max_tools=max_tools,
        active_app=ACTIVE_CONTEXT.get("active_app"),
        active_url=ACTIVE_CONTEXT.get("active_url"),
    )

    print(f"[ROUTER] Query: '{query}' → {len(filtered)} tools "
          f"(from {len(all_schemas)} total)")
    return jsonify(filtered)


@app.route("/health", methods=["GET"])
def health_check():
    """Readiness probe — includes native + MCP counts and LLM status."""
    from core.config import LLM
    mcp_count = len(_mcp.get_mcp_tool_names()) if _mcp else 0
    return jsonify({
        "status": "ok",
        "native": len(TOOL_MAP),
        "mcp": mcp_count,
        "tools": len(TOOL_MAP) + mcp_count,
        "llm": {"configured": LLM.configured, "model": LLM.model, "runtime": LLM.runtime},
    })


@app.route("/llm", methods=["GET"])
def llm_config_endpoint():
    """The plugged-in LLM + hardware-aware thread counts.

    The C++ daemon reads this ONCE at startup (no per-command cost) instead
    of compile-time constants, so any OpenAI-compatible local runtime works."""
    from core.config import LLM
    from core.llm.hardware import component_threads
    return jsonify({
        "configured": LLM.configured,
        "runtime": LLM.runtime,
        "chat_url": LLM.chat_url,
        "model": LLM.model,
        "context_tokens": LLM.context_tokens,
        "vision": {"chat_url": LLM.vision.chat_url, "model": LLM.vision.model},
        "source": LLM.source,
        **component_threads(),
    })


@app.route("/active_context", methods=["GET"])
def get_context():
    """Return only the active runtime context block."""
    return jsonify({"context": _context_block()})

@app.route("/preferences", methods=["GET"])
def get_preferences():
    """Return the formatted preferences block for LLM injection."""
    from core.preferences import get_preferences_prompt
    return jsonify({"preferences": get_preferences_prompt()})

@app.route("/session", methods=["GET"])
def get_session():
    """Return the current cross-turn active context."""
    return jsonify(ACTIVE_CONTEXT)


@app.route("/screen_context", methods=["GET"])
def screen_context_endpoint():
    """
    Fetch structured screen context for system prompt injection.
    Used by the C++ daemon for PATH 4 deep intent.

    Query params:
      ?force=true  — bypass cache (default: false)
    """
    force = request.args.get("force", "false").lower() == "true"
    try:
        ctx = get_screen_context_block(force=force)
        digest = get_screen_digest(force=force)
        return jsonify({
            "context_block": ctx,
            "app": digest.get("app", "Unknown"),
            "page_type": digest.get("page_type", "unknown"),
            "url": digest.get("url"),
            "summary": digest.get("summary", ""),
            "key_elements": digest.get("key_elements", []),
        })
    except Exception as e:
        return jsonify({"context_block": "", "error": str(e)})


@app.route("/mcp/status", methods=["GET"])
def mcp_server_status():
    """MCP-specific details — server names, RSS, tool lists."""
    if _mcp is None:
        return jsonify({"status": "not_initialized"})
    # Trigger RSS watchdog while we're at it
    health = _mcp.health_check()
    return jsonify({
        "servers": health,
        "summary": _mcp.status_summary(),
    })


# ── Learning endpoints (used by C++ daemon) ─────────────────────────

@app.route("/context", methods=["GET"])
def get_learned_context():
    """
    Fetch learned context for a query.  The C++ daemon calls this BEFORE
    sending the user's command to Ollama, so the LLM sees personalized
    context from past interactions.

    Query params:
      ?query=open+chrome   — the user's command (URL-encoded)
    """
    from core.learning import get_context_for_http
    query = request.args.get("query", "")
    if not query:
        return jsonify({"context_text": "", "profile_summary": ""})
    try:
        ctx = get_context_for_http(query)
        # Append knowledge context (procedures, facts, corrections)
        try:
            from core.knowledge import get_knowledge_context
            knowledge_block = get_knowledge_context(query)
            if knowledge_block:
                existing = ctx.get("context_text", "")
                ctx["context_text"] = (existing + "\n\n" + knowledge_block).strip() if existing else knowledge_block
        except Exception:
            pass
        # Append active context
        ctx_block = _context_block()
        if ctx_block:
            existing = ctx.get("context_text", "")
            ctx["context_text"] = (existing + "\n\n" + ctx_block).strip() if existing else ctx_block
        # Include continuation state for daemon
        if ACTIVE_CONTEXT["awaiting_user_choice"]:
            ctx["awaiting_user_choice"] = True
            ctx["pending_clarification"] = ACTIVE_CONTEXT.get("pending_clarification", "")
            ctx["pending_file_choices"] = ACTIVE_CONTEXT.get("pending_file_choices", [])
        return jsonify(ctx)
    except Exception as e:
        return jsonify({"context_text": "", "error": str(e)})


from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

# Learning writes (ChromaDB insert + profile update) run on ONE background
# worker so /learn returns immediately and the daemon never waits on it.
_LEARN_EXECUTOR = _ThreadPoolExecutor(max_workers=1, thread_name_prefix="learn")


@app.route("/learn", methods=["POST"])
def log_learning():
    """
    Log an interaction for learning.  The C++ daemon calls this AFTER
    each command completes so the learning engine can track episodes
    and update the user profile.

    Body: {
      "command": "open chrome",
      "path": "tool",
      "tools": ["open_application"],
      "result": "Opened Chrome.",
      "success": true
    }
    """
    data = request.get_json(silent=True)
    if not data or "command" not in data:
        return jsonify({"error": "Missing 'command'"}), 400
    _LEARN_EXECUTOR.submit(_record_learning, dict(data))
    return jsonify({"status": "queued"})


def _record_learning(data: dict) -> None:
    """Background half of /learn (see _LEARN_EXECUTOR)."""
    ACTIVE_CONTEXT["last_command"] = str(data.get("command", ""))[:300]
    ACTIVE_CONTEXT["last_reply"] = str(data.get("result", ""))[:300]
    # A world-knowledge question DEMASCAS couldn't answer from what it has
    # read → researched in the background while you're idle.
    try:
        from core.web.crawler import note_question
        if note_question(str(data.get("command", ""))):
            print(f"[WEB] Queued for background research: {str(data.get('command', ''))[:60]}")
    except Exception:
        pass
    # Experience → trajectory → (maybe) a new learned skill.
    try:
        from core.skills import trajectories, induce
        turns = data.get("llm_turns") if isinstance(data.get("llm_turns"), list) else []
        traj = trajectories.finish(
            command=str(data.get("command", "")), path=str(data.get("path", "")),
            success=bool(data.get("success", True)), reply=str(data.get("result", "")),
            llm_turns=turns[-6:], skill_id=str(data.get("skill_id", "")))
        if traj["success"] and traj["path"] in ("tool", "agent"):
            induce.induce_from_trajectory(traj, set(TOOL_MAP))
        # Learned app manuals improve with use: shortcuts / menu items that
        # were part of a task get verified (or marked as failing).
        from core.appknowledge import learner
        apps = set(learner.apps_in_text(traj["command"]))
        if ACTIVE_CONTEXT.get("active_app"):
            apps.add(str(ACTIVE_CONTEXT["active_app"]))
        for a in apps:
            learner.note_outcome(a, traj["tool_calls"], traj["success"] and traj.get("feedback") != "negative")
    except Exception as e:
        print(f"[LEARN] skill learning skipped: {e}")
    from core.learning import after_command
    try:
        # Keep the model warm while DEMASCAS is in use (Ollama).
        from core.config import LLM
        from core.llm.governor import touch
        touch(LLM)
    except Exception:
        pass
    try:
        tools_used = data.get("tools", [])
        success = data.get("success", True)

        after_command(
            command=data["command"],
            path_taken=data.get("path", "tool"),
            tools_used=tools_used,
            result=data.get("result", ""),
            success=success,
        )

        # (Multi-step successes now become skills — see core/skills/induce.py,
        #  called above.  The old name-only "macros" couldn't be replayed.)

        # Track app opens if present
        for tool in tools_used:
            if tool in ("open_application", "activate_application"):
                # Try to extract app name from the result
                pass

        # Auto-detect and save corrections on failure
        if not success:
            try:
                from core.knowledge import detect_and_save_correction
                detect_and_save_correction(
                    command=data["command"],
                    tools_used=tools_used,
                    result=data.get("result", ""),
                    success=success,
                )
            except Exception:
                pass  # non-fatal

    except Exception as e:
        print(f"[LEARN] Failed to record interaction: {e}")


@app.route("/agent", methods=["POST"])
def agent_endpoint():
    """
    Request:  {"command": "...", "context": {...}}   (context optional)
    Response: {"success": bool, "summary": str, "steps_taken": int,
               "tool_calls": [str], "error": str|null, "data": dict|null}
    """
    if not _AGENTS_AVAILABLE:
        return jsonify({"success": False, "summary": "", "error": "Agent system not loaded"}), 503

    # Strict JSON (no force=True): a browser cannot send application/json
    # cross-site without a CORS preflight, which this server never grants.
    data = request.get_json(silent=True) or {}
    command = _normalize_user_command(str(data.get("command", "")).strip())
    if not command:
        return jsonify({"success": False, "summary": "", "error": "Missing or empty 'command'"}), 400

    context = data.get("context") or dict(ACTIVE_CONTEXT)
    started = time.time()

    try:
        result = run_agent(command, context)
        payload = {
            "success": result.success,
            "summary": result.summary or "",
            "steps_taken": result.steps_taken,
            "tool_calls": result.tool_calls,
            "error": result.error or "",
            "data": result.data or {},
        }
        # Surface any confirmation the agent triggered so the daemon asks.
        with _PENDING_LOCK:
            fresh = [(p["created"], tok) for tok, p in _PENDING.items() if p["created"] >= started]
        if fresh:
            tok = max(fresh)[1]
            with _PENDING_LOCK:
                p = _PENDING.get(tok)
            if p is not None:
                payload["pending_confirmation"] = {
                    "confirm_token": tok,
                    "pending_tool": p["tool"],
                    "confirm_speech": _describe_action(p["tool"], p["args"]),
                }
        return jsonify(payload), 200
    except Exception as e:
        logger.exception(f"[/agent] {e}")
        return jsonify({
            "success": False,
            "summary": "Internal agent error.",
            "steps_taken": 0,
            "tool_calls": [],
            "error": str(e),
            "data": {},
        }), 500


@app.route("/agent/status", methods=["GET"])
def agent_status_endpoint():
    """What the running agent is doing (for "what are you doing?")."""
    try:   # the agents run from the top-level "agents" package (see imports)
        from agents.base_agent import PROGRESS
    except Exception:
        return jsonify({"running": False}), 200
    p = dict(PROGRESS)
    p["elapsed"] = round(time.time() - p.get("started", time.time()), 1) if p.get("running") else 0
    return jsonify(p), 200


@app.route("/agent/cancel", methods=["POST"])
def agent_cancel_endpoint():
    """Stop the running agent before its next step ("stop" / "cancel")."""
    for mod in ("server.agents.base_agent", "agents.base_agent"):
        try:
            m = __import__(mod, fromlist=["CANCEL_EVENT"])
            m.CANCEL_EVENT.set()
        except Exception:
            continue
    return jsonify({"status": "cancelling"}), 200


@app.route("/agent/route", methods=["POST"])
def agent_route_debug():
    """
    Request:  {"command": "..."}
    Response: {"domain": str, "should_use_agent": bool, "scores": dict}
    """
    if not _AGENTS_AVAILABLE:
        return jsonify({"error": "Agent system not loaded"}), 503

    data = request.get_json(silent=True) or {}
    command = _normalize_user_command(str(data.get("command", "")).strip())
    if not command:
        return jsonify({"error": "Missing or empty 'command'"}), 400

    dispatcher = get_dispatcher()
    return jsonify({
        "domain": route_command(command),
        "should_use": should_use_agent(command),
        "scores": dispatcher.scores(command),
    }), 200


# ── Entry point ──────────────────────────────────────────────────────

def _flush_learning() -> None:
    """Persist batched learning data (profile, people graph) on shutdown."""
    try:
        from core.learning import flush
        flush()
    except Exception:
        pass


def _warm_up() -> None:
    """Load the embedding model and ChromaDB collections in the background
    so the FIRST spoken command doesn't pay ~1 s of cold-start cost."""
    t0 = time.time()
    try:
        from core.embeddings import warm_up
        warm_up()
        from core.memory import retrieve_memories
        retrieve_memories("warm up", top_k=1)
        from core.learning import get_context_for_http
        get_context_for_http("warm up")
        from core.knowledge import get_knowledge_context
        get_knowledge_context("warm up")
        from core.skills.store import get_store
        get_store().match("warm up")          # builds the skill intent index
        from core.routing import route
        route("warm up")                      # builds the chat/action index
        from core.web.store import recall, stats
        recall("warm up")                     # loads the web-knowledge matrix
        print(f"[✓] Web knowledge: {stats()['chunks']} notes from {stats()['pages']} pages")
        from core.skills.induce import prune_unsafe_learned
        pruned = prune_unsafe_learned()
        if pruned:
            print(f"[SKILLS] Disabled {len(pruned)} unsafe learned skill(s): {pruned}")
        print(f"[✓] Memory/embedding warm-up done in {time.time() - t0:.1f} s")
    except Exception as e:
        print(f"[!] Warm-up failed (non-fatal): {e}")

    # Load the LLM now (Ollama) unless the laptop is short on free RAM.
    try:
        from core.config import LLM
        from core.llm import catalog, governor, hardware
        if LLM.configured:
            cm = catalog.find(LLM.model)
            free = hardware.profile().ram_available_gb
            print(f"[*] LLM preload: {governor.preload(LLM, free, cm.ram_gb if cm else None)}")
    except Exception as e:
        print(f"[!] LLM preload failed (non-fatal): {e}")


def _on_sigterm(signum, frame):
    # Default SIGTERM kills Python without running atexit handlers, which
    # would drop batched profile writes and leak MCP subprocesses.
    raise SystemExit(0)


def run_server() -> None:
    """Start the tool server.  Used by both `python main.py` and
    `python server/tool_server.py` so they behave identically."""
    if _should_boot_mcp():
        # Boot MCP servers before Flask starts accepting requests
        _boot_mcp()
        atexit.register(_shutdown_mcp)
    else:
        print("[*] MCP boot disabled via DEMASCAS_SKIP_MCP_BOOT=1; native tools only.")

    from core.config import LLM
    if LLM.configured:
        print(f"[✓] LLM: {LLM.model} via {LLM.runtime} ({LLM.chat_url}) [{LLM.source}]")
    else:
        print("[!] No language model is set up yet — DEMASCAS will only run shortcuts\n"
              "    and non-LLM actions.  Plug one in with:  python main.py setup")

    atexit.register(_flush_learning)
    atexit.register(lambda: _LEARN_EXECUTOR.shutdown(wait=True))
    threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
    try:   # learn into the model when idle + plugged in (see core/finetune)
        from core.finetune.scheduler import start_background
        start_background()
    except Exception as e:
        print(f"[!] Auto fine-tuning unavailable: {e}")
    try:   # read the web while idle (see core/web/crawler.py)
        from core.web.crawler import start_background as start_web
        start_web()
    except Exception as e:
        print(f"[!] Background web research unavailable: {e}")
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        pass  # not in main thread (e.g. imported by a test runner)

    mcp_count = len(_mcp.get_mcp_tool_names()) if _mcp else 0
    total = len(TOOL_MAP) + mcp_count

    print(f"[*] DEMASCAS Tool Server starting ({total} tools: "
          f"{len(TOOL_MAP)} native + {mcp_count} MCP)...")
    print(f"[*] Endpoints:")
    print(f"      POST http://{_server_host()}:{_server_port()}/tool")
    print(f"      GET  http://{_server_host()}:{_server_port()}/tools")
    print(f"      POST http://{_server_host()}:{_server_port()}/tools_for_query")
    print(f"      GET  http://{_server_host()}:{_server_port()}/health")
    print(f"      GET  http://{_server_host()}:{_server_port()}/session")
    print(f"      GET  http://{_server_host()}:{_server_port()}/screen_context")
    print(f"      GET  http://{_server_host()}:{_server_port()}/mcp/status")
    print(f"      GET  http://{_server_host()}:{_server_port()}/context?query=...")
    print(f"      POST http://{_server_host()}:{_server_port()}/learn")
    print(f"      POST http://{_server_host()}:{_server_port()}/agent")
    print(f"      POST http://{_server_host()}:{_server_port()}/confirm")
    print()

    # Disable Flask's default request logging for clean output
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # threaded=True: agents call back into /tool while /agent is running.
    app.run(host=_server_host(), port=_server_port(), debug=False,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    run_server()
