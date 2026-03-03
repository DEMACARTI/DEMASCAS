"""
DEMASCAS — core/toolbox.py
JSON tool schemas for Ollama's native function-calling API.

Each schema describes a tool the LLM can invoke. The 'function.name' field
must match a key in TOOL_MAP (defined in core/agent.py).

To add a new capability:
  1. Write the Python function in the appropriate actuators/ module.
  2. Add its JSON schema here in the TOOLS list.
  3. Register it in core/agent.py → TOOL_MAP.
"""

TOOLS = [
    # ------------------------------------------------------------------
    # App Management
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": (
                "Opens or activates a macOS application by name. "
                "Use this to launch apps like Calculator, Safari, Notes, etc. "
                "If the app is already running it will be brought to the front."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The name of the macOS application to open (e.g. 'Calculator', 'Safari').",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_keyboard_shortcut",
            "description": (
                "Sends a keyboard shortcut to the frontmost application. "
                "IMPORTANT DISTINCTIONS: "
                "command+w = close WINDOW/TAB only (app stays open). "
                "command+q = QUIT/EXIT the entire APPLICATION (closes everything). "
                "If user says 'close the app', 'quit', 'exit', 'close Safari', etc. → use command+q. "
                "If user says 'close the window', 'close this tab' → use command+w. "
                "Other examples: command+n (new window), command+t (new tab), "
                "command+shift+t (reopen tab), command+m (minimize), return, escape, tab, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "The key combo, e.g. 'command+n', 'command+shift+t', 'return', 'escape'.",
                    }
                },
                "required": ["keys"],
            },
        },
    },

    # ------------------------------------------------------------------
    # UI Interaction
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "click_ui_element",
            "description": (
                "Clicks a named UI element in the frontmost macOS application. "
                "The element_name MUST be extracted exactly from the UI tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_name": {
                        "type": "string",
                        "description": "The exact name of the UI element to click.",
                    }
                },
                "required": ["element_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text_into_element",
            "description": (
                "Focuses a named text field and types the given text into it. "
                "The element_name MUST be extracted exactly from the UI tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_name": {
                        "type": "string",
                        "description": "The exact name of the text field to type into.",
                    },
                    "text_input": {
                        "type": "string",
                        "description": "The text string to type.",
                    },
                },
                "required": ["element_name", "text_input"],
            },
        },
    },

    # ------------------------------------------------------------------
    # File / RAG
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "summarize_latest_pdf",
            "description": (
                "Finds the most recently downloaded PDF in ~/Downloads, reads the "
                "first 3 pages, and returns a bullet-point summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },

    # ------------------------------------------------------------------
    # Vision (read the screen)
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_active_window_tree",
            "description": (
                "Reads the macOS Accessibility Tree of a window and returns a "
                "structured text map of all UI elements. If app_name is provided, "
                "scans that specific app; otherwise scans the frontmost window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Optional app name to scan (e.g. 'Safari', 'Finder'). If omitted, scans the frontmost app.",
                    }
                },
                "required": [],
            },
        },
    },

    # ------------------------------------------------------------------
    # App lifecycle — quit / activate / list
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "quit_application",
            "description": (
                "Quits (closes) a SPECIFIC macOS application by name. "
                "Works even if the app is NOT in the foreground. "
                "Use this when the user says 'close Safari', 'quit VS Code', "
                "'exit Chrome', etc. — targets the named app directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The name of the application to quit (e.g. 'Safari', 'Visual Studio Code').",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_application",
            "description": (
                "Brings a running macOS application to the foreground. "
                "Use this to switch focus to a specific app window before "
                "performing keyboard shortcuts or UI interactions on it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The name of the application to activate (e.g. 'Finder', 'Safari').",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_running_applications",
            "description": (
                "Returns a list of all currently running user-facing macOS "
                "applications. Use this when the user asks 'what apps are open', "
                "'which windows are running', or you need to know what's open "
                "before performing an action on a specific app."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },

    # ------------------------------------------------------------------
    # File browsing
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_files_in_folder",
            "description": (
                "Lists files in a folder (default: ~/Downloads), sorted by "
                "newest first with file sizes. Use when user asks 'what files "
                "are in Downloads', 'show me my downloads', 'list files in "
                "Documents', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_path": {
                        "type": "string",
                        "description": "Path to folder (e.g. '~/Downloads', '~/Documents', '~/Desktop'). Defaults to ~/Downloads.",
                    }
                },
                "required": [],
            },
        },
    },

    # ------------------------------------------------------------------
    # Web browsing
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Searches the web using DuckDuckGo and returns the top results "
                "with titles, URLs, and snippets. Use when the user asks to "
                "search for something online, look something up, or find "
                "information you don't know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query (e.g. 'latest iPhone price', 'weather in Delhi').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": (
                "Fetches a webpage URL and extracts the main readable text "
                "content. Strips HTML tags, scripts, and navigation clutter. "
                "Use when the user asks to read a webpage, summarize a URL, "
                "or get content from a specific website."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to read (e.g. 'https://en.wikipedia.org/wiki/Python').",
                    }
                },
                "required": ["url"],
            },
        },
    },

    # ------------------------------------------------------------------
    # Persistent memory
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Saves a piece of information to DEMASCAS's long-term memory. "
                "Use when the user tells you to remember something, states a "
                "preference, or shares personal info like their name, schedule, "
                "or favourite things."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The information to remember (e.g. 'User prefers dark mode', 'Meeting at 3 PM').",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_memories",
            "description": (
                "Searches DEMASCAS's long-term memory for relevant information. "
                "Use when the user asks 'do you remember', 'what did I tell you', "
                "or when context from past conversations would help answer a question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for in memory (e.g. 'user preferences', 'meetings', 'name').",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return (default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },

    # ------------------------------------------------------------------
    # Scroll & drag
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "scroll_direction",
            "description": (
                "Scrolls the frontmost application window in a given direction. "
                "Use when the user asks to scroll up, scroll down, scroll left, "
                "or scroll right on the current page or document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Scroll direction: 'up', 'down', 'left', or 'right'.",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "Number of scroll increments (default 5, range 1-20). Use higher for 'scroll a lot'.",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag_element",
            "description": (
                "Drags one UI element to another in the frontmost application. "
                "Locates both elements by name in the accessibility tree and "
                "performs a mouse drag. Use for rearranging items, moving files, "
                "or drag-and-drop actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_element": {
                        "type": "string",
                        "description": "The exact name of the UI element to drag FROM (from accessibility tree).",
                    },
                    "to_element": {
                        "type": "string",
                        "description": "The exact name of the UI element to drag TO (from accessibility tree).",
                    },
                },
                "required": ["from_element", "to_element"],
            },
        },
    },

    # ------------------------------------------------------------------
    # Multi-window vision
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_open_windows",
            "description": (
                "Lists all currently visible windows across all running "
                "applications, showing each app with its window titles. "
                "Use when the user asks 'what windows are open', 'show all "
                "windows', or you need to see what's on screen before "
                "switching to a specific window."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]
