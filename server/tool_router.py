"""
DEMASCAS — server/tool_router.py
Smart tool filtering: sends only 3-5 relevant tools per query instead of all 21+.

Qwen 3B performs dramatically better with fewer tool schemas in context.
This module categorizes tools and matches user intent to select the
smallest relevant subset.
"""

# ── File search keywords — triggers find_file + open_file inclusion ──

FILE_SEARCH_KEYWORDS = [
    "open", "find", "search", "look for", "locate", "show me",
    "pdf", "ppt", "pptx", "doc", "docx", "xlsx", "xls", "txt", "zip",
    "mp4", "mp3", "jpg", "png", "csv",
    "file", "folder", "document", "certificate", "named", "called",
    "with name", "in downloads", "in documents", "in desktop", "download",
]


def should_exclude_open_url(query: str) -> bool:
    """Return True if open_url should be excluded from tool set.
    Excludes open_url when query is clearly about local files, not web navigation."""
    local_file_signals = [
        "pdf", "ppt", "pptx", "doc", "docx", "file", "folder",
        "in downloads", "in documents", "named", "called", "certificate",
        "xlsx", "xls", "txt", "zip", "mp4", "mp3",
    ]
    web_signals = [
        "http", "www", "website", "google", "open browser",
        "navigate to", ".com", ".org", ".io", ".net", ".edu",
    ]
    q = query.lower()
    has_local = any(s in q for s in local_file_signals)
    has_web = any(s in q for s in web_signals)
    return has_local and not has_web


# ── Tool categories ──────────────────────────────────────────────────

TOOL_CATEGORIES = {
    "ui_interaction": [
        "click_ui_element", "type_text", "type_text_into_element",
        "scroll_direction", "drag_element", "find_and_click",
        "right_click_element", "scroll_to_element",
    ],
    "app_management": [
        "open_application", "quit_application", "activate_application",
        "list_running_applications", "wait_for_app_ready",
    ],
    "web_browsing": [
        "open_url", "search_web", "read_webpage", "switch_tab",
    ],
    "browser": [
        "browser_navigate", "browser_navigate_and_wait",
        "browser_new_tab", "browser_close_tab",
        "browser_list_tabs", "browser_switch_to_tab",
        "browser_back", "browser_forward", "browser_reload",
        "browser_get_page_text", "browser_get_links", "browser_get_inputs",
        "browser_click", "browser_click_text",
        "browser_type", "browser_type_by_label", "browser_press_key",
        "browser_scroll", "browser_scroll_to_element",
        "browser_fill_form", "browser_select_option",
        "browser_wait_for_element", "browser_wait_for_page_load",
        "browser_get_dom_summary", "browser_get_url",
    ],
    "web_agent": [
        "web_analyze_page", "web_scan_for_obstacles", "web_handle_obstacle",
        "web_search", "web_read_article", "web_research",
        "web_execute_goal", "web_smart_click",
    ],
    "gmail_browser": [
        "gmail_compose_draft", "gmail_read_email",
        "browser_type", "browser_click_text",
    ],
    "youtube": [
        "web_youtube_search", "web_youtube_play_first",
        "browser_click_text", "browser_get_page_text",
    ],
    "ecommerce": [
        "web_ecommerce_search", "web_smart_click",
        "browser_get_page_text", "browser_click_text",
    ],
    "file_management": [
        "list_files_in_folder", "create_file", "delete_file",
        "summarize_latest_pdf", "find_file", "open_file",
    ],
    "screen_reading": [
        "get_active_window_tree", "list_open_windows",
    ],
    "vision": [
        "vision_execute",
    ],
    "keyboard": [
        "press_keyboard_shortcut",
    ],
    "memory": [
        "save_memory", "retrieve_memories",
    ],
    "clipboard": [
        "get_clipboard", "set_clipboard",
    ],
    "typing": [
        "type_text", "type_and_submit",
    ],
    "weather": [
        "search_web", "read_webpage",
    ],
    "realtime": [
        "smart_search", "search_web",
    ],
}

# ── Intent keyword → category mapping ────────────────────────────────
# Each keyword maps to one or more categories to include.

INTENT_KEYWORDS = {
    # UI interaction
    "click":       ["ui_interaction", "screen_reading"],
    "tap":         ["ui_interaction", "screen_reading"],
    "press":       ["ui_interaction", "keyboard"],
    "button":      ["ui_interaction", "screen_reading"],
    "select":      ["ui_interaction", "screen_reading"],
    "check":       ["ui_interaction", "screen_reading"],
    "toggle":      ["ui_interaction", "screen_reading"],
    "right click": ["ui_interaction", "screen_reading"],
    "right-click": ["ui_interaction", "screen_reading"],
    "find and click": ["ui_interaction", "screen_reading"],

    # Typing
    "type":        ["typing", "ui_interaction"],
    "write":       ["typing", "ui_interaction"],
    "enter":       ["typing", "keyboard"],
    "input":       ["typing", "ui_interaction"],
    "fill":        ["typing", "ui_interaction"],
    "search for":  ["typing", "web_browsing"],

    # Scrolling
    "scroll":      ["ui_interaction", "screen_reading"],
    "drag":        ["ui_interaction", "screen_reading"],

    # App management
    "open":        ["app_management", "web_browsing"],
    "launch":      ["app_management"],
    "start":       ["app_management"],
    "close":       ["app_management", "keyboard"],
    "quit":        ["app_management"],
    "exit":        ["app_management"],
    "switch":      ["app_management", "web_browsing"],
    "activate":    ["app_management"],
    "running":     ["app_management"],
    "apps":        ["app_management"],

    # Web
    "website":     ["web_browsing", "browser"],
    "webpage":     ["web_browsing", "browser"],
    "url":         ["web_browsing", "browser"],
    "browse":      ["web_browsing", "browser"],
    "google":      ["web_browsing", "web_agent"],
    "search":      ["web_browsing", "memory"],
    "tab":         ["web_browsing", "browser", "keyboard"],
    "safari":      ["web_browsing", "app_management"],
    "chrome":      ["web_browsing", "app_management"],
    "firefox":     ["web_browsing", "app_management"],
    "brave":       ["browser", "web_agent", "app_management"],
    "browser":     ["browser", "web_agent"],
    "navigate":    ["browser", "web_browsing"],
    "web page":    ["browser", "web_agent"],
    "cookie":      ["web_agent", "browser"],
    "captcha":     ["web_agent"],
    "popup":       ["web_agent", "browser"],
    "login wall":  ["web_agent"],
    "paywall":     ["web_agent"],
    "obstacle":    ["web_agent"],
    "dom":         ["browser", "web_agent"],
    "selector":    ["browser"],
    "javascript":  ["browser"],
    "form":        ["browser"],
    "checkbox":    ["browser"],
    "dropdown":    ["browser"],
    "article":     ["web_agent"],
    "research":    ["web_agent"],

    # Gmail (browser-based)
    "gmail":       ["gmail_browser", "browser"],
    "email":       ["gmail_browser"],
    "compose":     ["gmail_browser"],
    "draft":       ["gmail_browser"],
    "inbox":       ["gmail_browser", "browser"],
    "mail":        ["gmail_browser"],

    # YouTube
    "youtube":     ["youtube", "browser"],
    "video":       ["youtube", "browser"],
    "play":        ["youtube"],
    "watch":       ["youtube", "browser"],

    # E-commerce
    "amazon":      ["ecommerce", "browser"],
    "shop":        ["ecommerce", "browser"],
    "buy":         ["ecommerce", "browser"],
    "product":     ["ecommerce", "browser"],
    "price":       ["ecommerce", "realtime"],

    # Files
    "file":        ["file_management"],
    "folder":      ["file_management"],
    "download":    ["file_management"],
    "create":      ["file_management"],
    "delete":      ["file_management"],
    "remove":      ["file_management"],
    "pdf":         ["file_management"],
    "document":    ["file_management"],
    "list files":  ["file_management"],
    "find":        ["file_management"],
    "search file": ["file_management"],
    "look for":    ["file_management"],
    "locate":      ["file_management"],
    "open file":   ["file_management"],
    "open pdf":    ["file_management"],
    "open ppt":    ["file_management"],
    "open doc":    ["file_management"],
    "ppt":         ["file_management"],
    "pptx":        ["file_management"],
    "docx":        ["file_management"],
    "xlsx":        ["file_management"],

    # Screen reading
    "screen":      ["screen_reading"],
    "see":         ["screen_reading"],
    "read":        ["screen_reading", "web_browsing"],
    "look":        ["screen_reading"],
    "window":      ["screen_reading", "app_management"],
    "what's on":   ["screen_reading"],
    "ui":          ["screen_reading", "ui_interaction"],
    "vision":      ["vision"],
    "can't find":  ["vision", "screen_reading"],
    "not visible": ["vision"],
    "looks like":  ["vision", "screen_reading"],

    # Keyboard shortcuts
    "shortcut":    ["keyboard"],
    "copy":        ["keyboard", "clipboard"],
    "paste":       ["keyboard", "clipboard"],
    "undo":        ["keyboard"],
    "redo":        ["keyboard"],
    "cut":         ["keyboard", "clipboard"],
    "minimize":    ["keyboard"],
    "maximize":    ["keyboard"],
    "new tab":     ["keyboard", "web_browsing"],
    "new window":  ["keyboard"],

    # Memory
    "remember":    ["memory"],
    "recall":      ["memory"],
    "memory":      ["memory"],
    "forget":      ["memory"],
    "you know":    ["memory"],

    # Clipboard
    "clipboard":   ["clipboard"],
    "copied":      ["clipboard"],

    # Weather / realtime (always needs web search)
    "weather":     ["weather"],
    "temperature": ["weather"],
    "forecast":    ["weather"],
    "rain":        ["weather"],
    "humidity":    ["weather"],
    "degrees":     ["weather"],
    "climate":     ["weather"],
    "news":        ["weather", "web_browsing"],
    "latest":      ["weather", "web_browsing"],
    "stock":       ["weather"],
    "score":       ["weather"],

    # Realtime / smart search
    "price of":    ["realtime"],
    "who won":     ["realtime"],
    "what happened": ["realtime"],
    "tell me about": ["realtime"],
    "find out about": ["realtime"],
    "look up":     ["realtime", "web_browsing"],
    "search for":  ["realtime", "typing", "web_browsing"],
}


def select_tools_for_query(query: str, all_schemas: list[dict],
                           max_tools: int = 8,
                           active_app: str = None,
                           active_url: str = None) -> list[dict]:
    """
    Given a user query and all available tool schemas, return only
    the most relevant subset (3-8 tools).

    Always includes get_active_window_tree (screen reading is always useful),
    UNLESS Brave Browser is active (where it's useless for web content —
    use browser_get_dom_summary instead).

    When file-search keywords are detected, ALWAYS includes find_file + open_file
    and excludes open_url (prevents URL hallucination for local file queries).

    Args:
        query:       The user's raw command string.
        all_schemas: Full list of Ollama-format tool schemas.
        max_tools:   Maximum number of tools to return (default 8).
        active_app:  Currently active application (from ACTIVE_CONTEXT).
        active_url:  Currently active URL (from ACTIVE_CONTEXT).

    Returns:
        Filtered list of tool schemas.
    """
    query_lower = query.lower()
    is_brave_active = (active_app or "").lower() in ("brave browser", "brave")
    is_gmail_url = active_url and "mail.google.com" in (active_url or "")

    # ── Detect file-search intent ────────────────────────────────
    has_file_intent = any(kw in query_lower for kw in FILE_SEARCH_KEYWORDS)
    exclude_open_url = should_exclude_open_url(query)

    # Find matching categories based on intent keywords
    matched_categories = set()
    for keyword, categories in INTENT_KEYWORDS.items():
        if keyword in query_lower:
            matched_categories.update(categories)

    # If file intent detected, ensure file_management is included
    if has_file_intent:
        matched_categories.add("file_management")

    # ── Brave Browser active → auto-include browser/web_agent cats ──
    if is_brave_active:
        matched_categories.add("browser")
        matched_categories.add("web_agent")

    # ── Gmail URL active → auto-include gmail_browser ────────────
    if is_gmail_url:
        matched_categories.add("gmail_browser")
        matched_categories.add("browser")

    # If no categories matched, return a broad default set
    if not matched_categories:
        # Default: screen reading + keyboard + app management
        matched_categories = {"screen_reading", "keyboard", "app_management",
                              "ui_interaction"}

    # Collect tool names from matched categories
    matched_tools = set()
    for cat in matched_categories:
        if cat in TOOL_CATEGORIES:
            matched_tools.update(TOOL_CATEGORIES[cat])

    # ── Screen reading tool selection based on active app ────────
    if is_brave_active:
        # get_active_window_tree can't see web content — use DOM summary
        matched_tools.discard("get_active_window_tree")
        matched_tools.add("browser_get_dom_summary")
    else:
        # Non-browser: accessibility tree is the primary screen reader
        matched_tools.add("get_active_window_tree")

    # ── Gmail URL → prioritize gmail tools at top ────────────────
    if is_gmail_url:
        matched_tools.add("gmail_compose_draft")
        matched_tools.add("gmail_read_email")

    # ── File-search enforcement ──────────────────────────────────
    # When file intent detected, ALWAYS include find_file + open_file
    if has_file_intent:
        matched_tools.add("find_file")
        matched_tools.add("open_file")

    # ── open_url exclusion for local file queries ────────────────
    if exclude_open_url:
        matched_tools.discard("open_url")
        matched_tools.discard("open_url_and_wait")
        matched_tools.discard("search_web")
        matched_tools.discard("read_webpage")

    # Build name→schema lookup
    schema_map = {}
    for s in all_schemas:
        name = s.get("function", {}).get("name", "")
        if name:
            schema_map[name] = s

    # Filter to only tools that exist in our schema list
    # Prioritize gmail tools when on Gmail
    priority_tools = []
    regular_tools = []
    for name in matched_tools:
        if name in schema_map:
            if is_gmail_url and name.startswith("gmail_"):
                priority_tools.append(schema_map[name])
            else:
                regular_tools.append(schema_map[name])

    result = priority_tools + regular_tools

    # Cap at max_tools — prioritize by keeping the order from matched_tools
    if len(result) > max_tools:
        result = result[:max_tools]

    return result
