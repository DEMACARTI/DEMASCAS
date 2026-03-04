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
import sys
import os

# Ensure the DEMASCAS package root is on the path so we can import
# actuators/, sensors/, core/, and mcp/ directly.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from flask import Flask, request, jsonify

# ── Import all native tool functions ─────────────────────────────────

from actuators.file_system import (
    summarize_latest_pdf, list_files_in_folder, create_file, delete_file,
    find_file, open_file,
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
from core.utils import resolve_safe_path


# ── Safe click wrapper (3-tier fallback) ─────────────────────────────

def click_ui_element_safe(element_name: str) -> str:
    """
    Safe wrapper around click_ui_element with 3-tier fallback:
      1. wait_for_element → click_ui_element  (accessibility tree)
      2. If click fails → vision_execute      (VLM screenshot-based click)
      3. If VLM fails → structured error message

    This prevents the LLM from getting stuck in retry loops when an
    element isn't immediately available.
    """
    import time

    # Tier 1: Wait for element to appear, then click via accessibility tree
    try:
        wait_result = wait_for_element(element_name, timeout=3)
        if "Success" in wait_result:
            click_result = click_ui_element(element_name)
            if "Success" in click_result:
                return click_result
            # Click found element but failed — fall through to vision
            print(f"[SAFE-CLICK] Accessibility click failed: {click_result}")
        else:
            print(f"[SAFE-CLICK] Element not in tree: {wait_result}")
    except Exception as e:
        print(f"[SAFE-CLICK] Tier 1 error: {e}")

    # Tier 2: Vision-based click via VLM
    try:
        print(f"[SAFE-CLICK] Falling back to VLM vision for '{element_name}'...")
        time.sleep(0.3)  # let UI settle
        vision_result = vision_execute(f"Click the element labeled '{element_name}'")
        if not vision_result.startswith("[-]"):
            return vision_result
        print(f"[SAFE-CLICK] VLM vision failed: {vision_result}")
    except Exception as e:
        print(f"[SAFE-CLICK] Tier 2 error: {e}")

    # Tier 3: Structured error — helps LLM choose an alternative
    return (
        f"[-] Could not click '{element_name}'. "
        "The element was not found in the accessibility tree or via screen vision. "
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
}

_NATIVE_NAMES = frozenset(TOOL_MAP.keys())

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


def _update_context(tool_name: str, args: dict, result: str, success: bool):
    """Auto-update ACTIVE_CONTEXT after every tool call.
    Tracks app/folder/file/url, detects multi-match file results,
    and sets pending state for continuation detection."""
    now = _dt.datetime.now().isoformat()
    ACTIVE_CONTEXT["last_updated"] = now
    ACTIVE_CONTEXT["turn_count"] += 1

    # Always record what happened
    ACTIVE_CONTEXT["last_tool"] = tool_name
    ACTIVE_CONTEXT["last_tool_args"] = args

    if not success or (isinstance(result, str) and result.startswith("[-]")):
        ACTIVE_CONTEXT["last_failed_action"] = f"{tool_name}({args})"
        ACTIVE_CONTEXT["last_tool_result"] = result
        return

    ACTIVE_CONTEXT["last_tool_result"] = result
    ACTIVE_CONTEXT["last_action_summary"] = f"{tool_name}({args})"

    # ── App tracking ──
    if tool_name in ("open_application", "activate_application"):
        ACTIVE_CONTEXT["active_app"] = args.get("app_name", None)
    elif tool_name == "quit_application":
        if ACTIVE_CONTEXT["active_app"] == args.get("app_name"):
            ACTIVE_CONTEXT["active_app"] = None
    elif tool_name == "open_url" or tool_name == "open_url_and_wait":
        ACTIVE_CONTEXT["active_app"] = "Safari"
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
            import json as _json
            parsed = _json.loads(result) if isinstance(result, str) else result
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

# ── Tool alias normalization — tolerate common LLM hallucinated names ──

TOOL_ALIASES = {
    "click": "click_ui_element",
    "click_element": "click_ui_element",
    "click_ui": "click_ui_element",
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
    canonical = TOOL_ALIASES.get(name.strip().lower(), name)
    normalized = dict(args or {})

    # Generic noisy fields sometimes emitted by the model
    normalized.pop("window_title", None)
    normalized.pop("args", None)

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

    if canonical == "open_url":
        if "website" in normalized and "url" not in normalized:
            normalized["url"] = normalized.pop("website")
        if "link" in normalized and "url" not in normalized:
            normalized["url"] = normalized.pop("link")

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

# ── MCP singleton — booted once at server start ─────────────────────

_mcp: MCPManager | None = None


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
    name, args = _normalize_tool_call(name, args)

    def _get_tool_args(func, raw_args: dict) -> dict:
        """Safely extract and type-coerce arguments for a tool function."""
        import inspect
        sig = inspect.signature(func)
        cleaned = {}
        for param_name, param in sig.parameters.items():
            if param_name in raw_args:
                val = raw_args[param_name]
                # Coerce string "true"/"false" to bool if param expects bool
                if param.annotation is bool or (
                    param.default is not inspect.Parameter.empty
                    and isinstance(param.default, bool)):
                    if isinstance(val, str):
                        val = val.lower() in ("true", "1", "yes")
                cleaned[param_name] = val
            elif param.default is not inspect.Parameter.empty:
                pass  # use default
            else:
                cleaned[param_name] = raw_args.get(param_name)
        return cleaned

    # Priority 1: native (in-process, ~0 µs overhead)
    func = TOOL_MAP.get(name)
    if func is not None:
        try:
            safe_args = _get_tool_args(func, args)
            result = str(func(**safe_args))
            response = {"result": result}

            # Track active context
            _update_context(name, args, result, success=True)

            # Auto-learn from tool results (non-fatal)
            try:
                from core.knowledge import auto_save_from_tool_result
                auto_save_from_tool_result(name, args, result, success=True)
            except Exception:
                pass

            # Auto-capture screen state after UI-mutating tools
            if name in UI_MUTATING_TOOLS:
                try:
                    import time
                    time.sleep(0.3)  # let UI settle
                    screen = get_active_window_tree()
                    if screen and len(screen) < 5000:  # don't bloat response
                        response["screen_state"] = screen
                except Exception:
                    pass  # non-fatal

            return jsonify(response), 200
        except TypeError as e:
            _update_context(name, args, str(e), success=False)
            return jsonify({"result": f"[-] Bad arguments for {name}: {e}"}), 200
        except Exception as e:
            _update_context(name, args, str(e), success=False)
            return jsonify({"result": f"[-] Tool error ({name}): {e}"}), 200

    # Priority 2: MCP (JSON-RPC over stdio, ~5-10 ms)
    if _mcp is not None and _mcp.is_mcp_tool(name):
        try:
            result = _mcp.call_tool(name, args)
            return jsonify({"result": result}), 200
        except Exception as e:
            return jsonify({"result": f"[-] MCP tool error ({name}): {e}"}), 200

    return jsonify({"result": f"[-] Unknown tool: {name}. Use get_active_window_tree to read the screen."}), 200


@app.route("/tools", methods=["GET"])
def get_tool_schemas():
    """Return ALL tool schemas (native + MCP) in Ollama format."""
    return jsonify(_all_tool_schemas())


@app.route("/tools_for_query", methods=["POST"])
def get_filtered_tools():
    """Return only the most relevant tool schemas for a given query.
    Body: {"query": "click the save button", "max_tools": 8}
    Returns: filtered list of Ollama-format tool schemas (3-8 tools).
    """
    data = request.get_json(silent=True)
    if not data or "query" not in data:
        return jsonify(_all_tool_schemas())  # fallback to all

    query = data["query"]
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
    """Readiness probe — includes native + MCP counts."""
    mcp_count = len(_mcp.get_mcp_tool_names()) if _mcp else 0
    return jsonify({
        "status": "ok",
        "native": len(TOOL_MAP),
        "mcp": mcp_count,
        "tools": len(TOOL_MAP) + mcp_count,
    })


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


@app.route("/deep_intent", methods=["POST"])
def deep_intent_endpoint():
    """
    PATH 4 — Deep Intent Analysis endpoint.
    Takes user command + screen context and returns a structured intent plan.

    Body: {"command": "reply to that email", "screen_context": "...", "user_profile": "..."}
    Returns: task classification, prompt template, people context.
    """
    from core.prompt_templates import classify_task, get_template_for_task, get_deep_intent_system_prompt
    from core.learning import get_people_context, get_profile_summary

    data = request.get_json(silent=True)
    if not data or "command" not in data:
        return jsonify({"error": "Missing 'command'"}), 400

    command = data["command"]
    screen_ctx = data.get("screen_context", "")
    user_profile = data.get("user_profile", "")

    # Auto-fetch screen context if not provided
    if not screen_ctx:
        try:
            screen_ctx = get_screen_context_block(force=True)
        except Exception:
            screen_ctx = ""

    # Auto-fetch user profile if not provided
    if not user_profile:
        try:
            user_profile = get_profile_summary()
        except Exception:
            user_profile = ""

    # People context
    people_ctx = ""
    try:
        people_ctx = get_people_context(command)
    except Exception:
        pass

    # Classify task
    task_type = classify_task(command)

    # Get task-specific template
    task_prompt = get_template_for_task(
        task_type,
        screen_context=screen_ctx,
        user_profile=user_profile,
        contact_info=people_ctx,
        topic=command,
    )

    # Get deep intent system prompt (with real tool names)
    # Fetch filtered tools for this command so deep intent only references real tools
    all_schemas = _all_tool_schemas()
    filtered_schemas = select_tools_for_query(command, all_schemas, max_tools=12)
    tool_names = [s["function"]["name"] for s in filtered_schemas if "function" in s]

    deep_prompt = get_deep_intent_system_prompt(
        user_command=command,
        screen_context=screen_ctx,
        user_profile=user_profile,
        people_context=people_ctx,
        tool_names=tool_names,
    )

    return jsonify({
        "task_type": task_type,
        "task_prompt": task_prompt or "",
        "deep_intent_prompt": deep_prompt,
        "screen_context": screen_ctx,
        "user_profile": user_profile,
        "people_context": people_ctx,
    })


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
    from core.learning import get_context_for_http, get_all_macros_for_rag
    query = request.args.get("query", "")
    if not query:
        return jsonify({"context_text": "", "profile_summary": ""})
    try:
        ctx = get_context_for_http(query)
        # Append macro context if available
        macro_ctx = get_all_macros_for_rag(query)
        if macro_ctx:
            existing = ctx.get("context_text", "")
            ctx["context_text"] = (existing + "\n\n" + macro_ctx).strip() if existing else macro_ctx
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
    from core.learning import after_command, track_app_open, record_successful_sequence
    data = request.get_json(silent=True)
    if not data or "command" not in data:
        return jsonify({"error": "Missing 'command'"}), 400

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

        # Record multi-step sequences for macro promotion
        if success and len(tools_used) >= 2:
            try:
                record_successful_sequence(
                    command=data["command"],
                    tools_used=tools_used,
                    result=data.get("result", ""),
                )
            except Exception:
                pass  # non-fatal

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

        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 200


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    # Boot MCP servers before Flask starts accepting requests
    _boot_mcp()
    atexit.register(_shutdown_mcp)

    mcp_count = len(_mcp.get_mcp_tool_names()) if _mcp else 0
    total = len(TOOL_MAP) + mcp_count

    print(f"[*] DEMASCAS Tool Server starting ({total} tools: "
          f"{len(TOOL_MAP)} native + {mcp_count} MCP)...")
    print(f"[*] Endpoints:")
    print(f"      POST http://127.0.0.1:5001/tool")
    print(f"      GET  http://127.0.0.1:5001/tools")
    print(f"      POST http://127.0.0.1:5001/tools_for_query")
    print(f"      GET  http://127.0.0.1:5001/health")
    print(f"      GET  http://127.0.0.1:5001/session")
    print(f"      GET  http://127.0.0.1:5001/screen_context")
    print(f"      POST http://127.0.0.1:5001/deep_intent")
    print(f"      GET  http://127.0.0.1:5001/mcp/status")
    print(f"      GET  http://127.0.0.1:5001/context?query=...")
    print(f"      POST http://127.0.0.1:5001/learn")
    print()

    # Disable Flask's default banner and request logging for clean output
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
