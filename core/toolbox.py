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
                "If the app is already running it will be brought to the front. "
                "DO NOT use this to open websites or URLs — use open_url instead."
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
            "name": "open_url",
            "description": (
                "Opens a URL/website in a macOS browser. "
                "Use this whenever the user asks to open a website, URL, or web page "
                "in a browser. Examples: 'open instagram.com in Safari', "
                "'go to youtube.com', 'open google.com in Chrome'. "
                "This actually navigates the browser to the URL — unlike open_application "
                "which only launches the app without navigating anywhere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open (e.g. 'instagram.com', 'https://youtube.com', 'github.com/user/repo').",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Browser to use (default 'Safari'). e.g. 'Safari', 'Google Chrome', 'Firefox'.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url_and_wait",
            "description": (
                "Opens a URL in a browser AND waits for the page to fully load before returning. "
                "Use this instead of open_url when you need to interact with the page after opening. "
                "For example: 'go to youtube.com and search for cats' — the wait ensures the page "
                "is ready before the next action (clicking, typing, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open (e.g. 'instagram.com', 'https://youtube.com').",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Browser to use (default 'Safari').",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Max seconds to wait for page load (default 3.0).",
                    }
                },
                "required": ["url"],
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
            "name": "type_text",
            "description": (
                "Types text into the currently focused field using keystroke injection. "
                "Does NOT require knowing the UI element name — types into whatever is focused. "
                "Can optionally: (1) activate a specific app first, (2) press a focus shortcut "
                "before typing (e.g. command+l to focus Safari/Chrome address bar), "
                "(3) press Enter/Return after typing. "
                "COMMON PATTERNS: "
                "'type instagram.com in Safari address bar and press enter' → "
                "type_text(text='instagram.com', app_name='Safari', focus_shortcut='command+l', press_enter=true). "
                "'write hello in Notes' → type_text(text='hello', app_name='Notes'). "
                "'type my email and press enter' → type_text(text='user@example.com', press_enter=true). "
                "Use this instead of type_text_into_element when you don't know the exact UI element name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to type (e.g. 'instagram.com', 'Hello World', 'user@email.com').",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Optional: app to activate before typing (e.g. 'Safari', 'Notes', 'Chrome').",
                    },
                    "press_enter": {
                        "type": "boolean",
                        "description": "If true, presses Enter/Return after typing. Default false.",
                    },
                    "focus_shortcut": {
                        "type": "string",
                        "description": "Optional: keyboard shortcut to press BEFORE typing to focus the right field. Examples: 'command+l' (browser address bar), 'command+a' (select all), 'command+k' (Slack search).",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text_into_element",
            "description": (
                "Focuses a named text field and types the given text into it. "
                "The element_name MUST be extracted exactly from the UI tree. "
                "Prefer type_text when you don't know the element name."
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
    # File creation & deletion (confirmation-gated)
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Creates a new file at the given path with optional text content. "
                "Parent directories are created automatically. "
                "IMPORTANT: You MUST ask the user for verbal confirmation BEFORE calling this tool. "
                "Say something like 'I will create a file called X at Y. Should I proceed?' "
                "and ONLY call this tool AFTER the user confirms. "
                "If the file already exists, the tool will refuse and you should inform the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path for the new file (e.g. '~/Desktop/notes.txt', '~/Documents/todo.md').",
                    },
                    "content": {
                        "type": "string",
                        "description": "Optional text content to write into the file. Default: empty file.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Deletes a file by moving it to the macOS Trash (recoverable). "
                "IMPORTANT: You MUST ask the user for verbal confirmation BEFORE calling this tool. "
                "Say something like 'I will delete the file X. Are you sure?' "
                "and ONLY call this tool AFTER the user confirms with 'yes', 'go ahead', 'do it', etc. "
                "NEVER delete without explicit user approval. "
                "Will refuse to delete non-empty directories for safety."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to delete (e.g. '~/Desktop/notes.txt').",
                    }
                },
                "required": ["file_path"],
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

    # ------------------------------------------------------------------
    # New agentic tools — human-like OS control
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "wait_for_element",
            "description": (
                "Waits for a named UI element to appear in the frontmost app. "
                "Polls every 0.5s up to timeout. Use after actions that change "
                "the UI (opening dialogs, loading pages) to ensure the target "
                "element exists before clicking or typing into it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_name": {
                        "type": "string",
                        "description": "The exact name of the UI element to wait for.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 10, max 30).",
                    },
                },
                "required": ["element_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_app_ready",
            "description": (
                "Waits for an application to be running and have at least one "
                "window. Use after open_application to ensure the app is fully "
                "loaded before interacting with it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The application name to wait for (e.g. 'Safari', 'Notes').",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 10, max 30).",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": (
                "Returns the current contents of the macOS clipboard (text). "
                "Use when the user asks 'what did I copy', 'show clipboard', "
                "or you need to read clipboard data after a copy operation."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": (
                "Sets the macOS clipboard to the given text. "
                "Use when the user asks to 'copy this text', or you need "
                "to prepare text for pasting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to copy to the clipboard.",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_and_click",
            "description": (
                "Searches the frontmost window's UI tree for ANY element "
                "whose name CONTAINS the search text (case-insensitive partial match), "
                "then clicks the first match. More forgiving than click_ui_element "
                "which requires an exact name. Use when you're not sure of the "
                "exact element name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_text": {
                        "type": "string",
                        "description": "Partial text to search for in element names (e.g. 'Submit', 'Save', 'Next').",
                    }
                },
                "required": ["search_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click_element",
            "description": (
                "Right-clicks (opens context menu) a named UI element. "
                "Use when you need to access right-click/context menu options "
                "on an element."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_name": {
                        "type": "string",
                        "description": "The exact name of the UI element to right-click.",
                    }
                },
                "required": ["element_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_and_submit",
            "description": (
                "Types text and presses Enter — convenience for search bars, "
                "address bars, login forms. Equivalent to type_text with "
                "press_enter=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to type and submit.",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Optional: app to activate first.",
                    },
                    "focus_shortcut": {
                        "type": "string",
                        "description": "Optional: shortcut to press before typing (e.g. 'command+l').",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_tab",
            "description": (
                "Switches to the next or previous browser/app tab. "
                "Use when the user says 'next tab', 'previous tab', "
                "'switch tab', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Tab direction: 'next' or 'previous' (default 'next').",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_to_element",
            "description": (
                "Scrolls down in the frontmost window until a named element "
                "becomes visible in the accessibility tree. Use when you need "
                "to find an element that may be off-screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_name": {
                        "type": "string",
                        "description": "The exact name of the element to scroll to.",
                    },
                    "max_scrolls": {
                        "type": "integer",
                        "description": "Maximum scroll attempts (default 10, max 30).",
                    },
                },
                "required": ["element_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vision_execute",
            "description": (
                "Uses the vision-language model (VLM) to look at the screen and "
                "execute a visual action. Takes a screenshot, sends it to the VLM "
                "to locate elements by appearance, then clicks/types/scrolls at "
                "the pixel coordinates. Use when accessibility tree can't find "
                "an element but you know it's visually on screen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "Natural language description of what to do. "
                            "e.g. 'Click the red submit button', 'Find the search bar and click it'."
                        ),
                    }
                },
                "required": ["action"],
            },
        },
    },

    # ------------------------------------------------------------------
    # Communication tools (JARVIS-grade)
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "read_current_email",
            "description": (
                "Uses AI vision to read and extract the email or message currently "
                "visible on screen. Works with Mail, Gmail, Outlook, etc. Returns "
                "structured data: sender, subject, body preview, timestamp. "
                "Use when the user says 'read this email', 'what does this email say', "
                "'who sent this', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compose_email",
            "description": (
                "Composes an email draft using AI. Does NOT send — returns the draft "
                "for user confirmation. Use when the user asks to 'write an email', "
                "'compose an email to John', 'draft an email about the project', etc. "
                "After composing, the user can confirm to have it typed into the email app."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "Who to send to (name or email address).",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject line (optional — will be auto-generated if empty).",
                    },
                    "key_points": {
                        "type": "string",
                        "description": "What the email should convey (e.g. 'thank them for the meeting, share next steps').",
                    },
                    "tone": {
                        "type": "string",
                        "description": "Tone: 'formal', 'casual', or 'professional' (default: professional).",
                    },
                },
                "required": ["key_points"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "smart_reply",
            "description": (
                "Reads the email/message currently on screen and generates a "
                "context-aware reply. Matches the tone of the original message. "
                "Use when the user says 'reply to this', 'respond to this email', "
                "'answer this message', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": "Optional guidance for the reply (e.g. 'accept the meeting', 'decline politely', 'ask for more details').",
                    },
                    "tone": {
                        "type": "string",
                        "description": "Override tone: 'formal', 'casual', 'professional'. If empty, matches original.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_and_execute",
            "description": (
                "Queues a potentially risky action for user confirmation before executing. "
                "Use this before actions that: delete files, send messages, modify settings, "
                "or make purchases. The system will ask the user 'Should I proceed?' and "
                "only execute after they confirm. "
                "IMPORTANT: Call this instead of directly executing risky actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_description": {
                        "type": "string",
                        "description": "Human-readable description of what will happen (e.g. 'Send email to john@example.com').",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "The actual tool to execute if confirmed.",
                    },
                    "tool_args": {
                        "type": "string",
                        "description": "JSON string of arguments for the tool.",
                    },
                },
                "required": ["action_description", "tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_contact",
            "description": (
                "Uses AI vision to extract contact information visible on screen. "
                "Works with email headers, contact cards, social profiles, signatures. "
                "Returns: name, email, phone, organization. "
                "Automatically saves the contact for future reference."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_research_and_report",
            "description": (
                "Performs structured web research on a topic and returns a synthesized "
                "spoken summary. Searches the web, reads top results, and combines "
                "findings into a coherent report. Use when the user asks to 'research X', "
                "'find out about X', 'give me a summary of X', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic to research (e.g. 'latest MacBook Pro features', 'best restaurants in Delhi').",
                    },
                    "depth": {
                        "type": "string",
                        "description": "'brief' (3-4 sentences) or 'detailed' (6-8 sentences). Default: brief.",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    # ------------------------------------------------------------------
    # File Search & Open
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "find_file",
            "description": (
                "Recursively searches a folder for files whose name contains a partial match. "
                "Returns up to 5 matching files with their full paths and a first_match_path field. "
                "Use this FIRST when the user asks to open, find, or locate a specific file. "
                "Then use open_file with the first_match_path from the result. "
                "Supports filtering by extension (pdf, pptx, docx, etc.). "
                "ALWAYS use ~/Downloads, ~/Documents, ~/Desktop as folder paths — NEVER use /Users/name/. "
                "If the user gives no specific filename, pass partial_name as empty string to list all files of that type. "
                "NEVER use 'FILE', 'file', 'a', 'the', or the extension word itself as partial_name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "The folder to search in (e.g. '~/Downloads', '~/Desktop', '~/Documents').",
                    },
                    "partial_name": {
                        "type": "string",
                        "description": "Partial filename to search for, case-insensitive (e.g. 'SIH', 'report', 'budget').",
                    },
                    "extension": {
                        "type": "string",
                        "description": "Optional file extension filter without dot (e.g. 'pdf', 'pptx', 'docx'). Omit to search all file types.",
                    },
                },
                "required": ["folder", "partial_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": (
                "Opens a file at the given absolute path using its default macOS application. "
                "Use this AFTER find_file to open a file that was found — pass the first_match_path "
                "from find_file's result as the file_path. "
                "Works with any file type: PDFs open in Preview, .pptx in Keynote/PowerPoint, etc. "
                "Path is automatically resolved — hallucinated usernames are corrected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Full absolute path to the file (e.g. '/Users/daksh/Downloads/SIH2025.pdf').",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    # ------------------------------------------------------------------
    # Smart Search & Realtime Information
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "smart_search",
            "description": (
                "Searches the web intelligently with caching and multi-source fallback. "
                "Chain: fact cache → Google Custom Search → DuckDuckGo fallback. "
                "Use this for any factual question, real-time info, prices, people, events, etc. "
                "Automatically caches results so repeated queries are instant. "
                "Prefer this over search_web for all general knowledge and real-time queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query (e.g. 'current price of gold', 'who is the PM of India').",
                    },
                    "ttl_hours": {
                        "type": "number",
                        "description": "Cache TTL in hours. Default 24. Use 0 for always-fresh results.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Gets current weather for any location. Uses wttr.in (free, no API key). "
                "Returns temperature, conditions, humidity, and wind speed. "
                "If no location given, auto-detects from IP."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or coordinates (e.g. 'Delhi', 'London', 'New York'). Leave empty for auto-detect.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": (
                "Gets latest news headlines using DuckDuckGo News. "
                "Returns top headlines with source, date, and URL. "
                "If no topic given, returns general top headlines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "News topic to search for (e.g. 'AI', 'cricket', 'elections'). Leave empty for top headlines.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of headlines to return (default 5, max 10).",
                    },
                },
                "required": [],
            },
        },
    },

    # ==================================================================
    # Brave Browser Automation (JS injection via AppleScript)
    # ==================================================================
    {
        "type": "function",
        "function": {
            "name": "browser_execute_js",
            "description": (
                "Execute arbitrary JavaScript in the active Brave Browser tab. "
                "Returns the JS return value. Use for custom browser automation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "js_code": {
                        "type": "string",
                        "description": "JavaScript code to execute. Must return a value.",
                    }
                },
                "required": ["js_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_url",
            "description": "Get the URL of the active Brave Browser tab.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_title",
            "description": "Get the title of the active Brave Browser tab.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Navigate the active Brave Browser tab to a URL. "
                "Adds https:// if missing. Use for quick navigation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to (e.g. 'google.com', 'https://github.com').",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate_and_wait",
            "description": (
                "Navigate to a URL in Brave and wait for the page to fully load. "
                "Use when you need to interact with the page after navigation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to.",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Max seconds to wait for page load (default 3.0).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_new_tab",
            "description": "Open a new tab in Brave Browser, optionally with a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Optional URL to open in the new tab.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close_tab",
            "description": "Close the active tab in Brave Browser.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_list_tabs",
            "description": "List all open tabs in Brave Browser with titles and URLs.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_switch_to_tab",
            "description": "Switch to a specific Brave Browser tab by index (1-based).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_index": {
                        "type": "integer",
                        "description": "1-based index of the tab to switch to.",
                    }
                },
                "required": ["tab_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_back",
            "description": "Navigate back in Brave Browser history.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_forward",
            "description": "Navigate forward in Brave Browser history.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_reload",
            "description": "Reload the current page in Brave Browser.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_page_text",
            "description": (
                "Extract the visible text content of the current Brave Browser page. "
                "Strips scripts, styles, and navigation. Use to read page content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 5000).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_links",
            "description": "Get all links on the current page with text and URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_links": {
                        "type": "integer",
                        "description": "Maximum links to return (default 20).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_inputs",
            "description": (
                "List all input fields, textareas, selects on the current Brave page. "
                "Returns name, type, placeholder for each. Useful before filling forms."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click an element in Brave Browser by CSS selector (e.g. '#submit', '.btn-primary'). "
                "If you only know the visible text (e.g. 'Compose', 'Sign In'), "
                "prefer browser_click_text instead — it is more reliable for text-based clicking. "
                "This tool auto-redirects to browser_click_text when the selector "
                "doesn't look like CSS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the element (e.g. '#submit', '.btn-primary', 'button[type=submit]'). "
                                       "Do NOT use names like 'compose_button' — use browser_click_text for text labels.",
                    }
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click_text",
            "description": (
                "Click an element in Brave Browser by its visible text. "
                "Searches buttons, links, and clickable elements. "
                "PREFERRED over browser_click when you know the text label. "
                "Use exact visible text like 'Compose', 'Sign In', 'Next' — "
                "NOT snake_case like 'compose_button'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The visible text of the element to click (e.g. 'Submit', 'Sign In', 'Compose', 'Next').",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": (
                "Type text into a Brave Browser element by CSS selector. "
                "Dispatches 'input' AND 'change' events for React compatibility. "
                "Handles contenteditable elements with execCommand."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the input element.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type into the element.",
                    },
                    "clear_first": {
                        "type": "boolean",
                        "description": "Clear existing content first (default true).",
                    },
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type_by_label",
            "description": (
                "Type into a Brave Browser input by its label, placeholder, or aria-label. "
                "Use when you know the field label but not the CSS selector."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label_or_placeholder": {
                        "type": "string",
                        "description": "Label, placeholder, or aria-label text of the input.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type.",
                    },
                    "clear_first": {
                        "type": "boolean",
                        "description": "Clear existing content first (default true).",
                    },
                },
                "required": ["label_or_placeholder", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": (
                "Dispatch a keyboard event in Brave Browser. "
                "Use for Enter, Tab, Escape, ArrowDown, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name (e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown').",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to target. Default: active element.",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": (
                "Scroll the Brave Browser page in a direction. "
                "Supports up, down, left, right, top, bottom."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Scroll direction: 'up', 'down', 'left', 'right', 'top', 'bottom'.",
                    },
                    "pixels": {
                        "type": "integer",
                        "description": "Pixels to scroll (default 500).",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill_form",
            "description": (
                "Fill multiple form fields at once in Brave Browser. "
                "Pass a JSON mapping of CSS selectors to values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_values": {
                        "type": "string",
                        "description": 'JSON mapping selector → value, e.g. \'{"#email": "user@example.com", "#password": "secret"}\'.',
                    }
                },
                "required": ["field_values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select_option",
            "description": "Select an option in a dropdown <select> element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the select element.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Option value or visible text to select.",
                    },
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for_element",
            "description": (
                "Wait for a CSS selector to appear on the Brave Browser page. "
                "Polls every 0.5s. Use after navigation or actions that change the page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to wait for.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 10).",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_dom_summary",
            "description": (
                "Get a structured summary of all interactive elements on the current "
                "Brave page — buttons, links, inputs, etc. Use to understand page layout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_elements": {
                        "type": "integer",
                        "description": "Max elements to include (default 50).",
                    }
                },
                "required": [],
            },
        },
    },

    # ==================================================================
    # Gmail-specific (via Brave Browser JS injection)
    # ==================================================================
    {
        "type": "function",
        "function": {
            "name": "gmail_compose_draft",
            "description": (
                "Open Gmail compose in Brave and fill To/Subject/Body as a DRAFT. "
                "NEVER auto-sends. The user must manually click Send. "
                "Use this tool DIRECTLY when the user asks to write/compose/draft an email. "
                "All fields are optional — call with just subject+body if recipient unknown, "
                "or with no args to open a blank compose window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address (optional — opens blank compose if empty).",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject (optional).",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body text (optional).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_read_email",
            "description": (
                "Read the currently open Gmail email in Brave Browser. "
                "Extracts sender, subject, date, and body."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },

    # ==================================================================
    # Web Agent — autonomous browsing intelligence
    # ==================================================================
    {
        "type": "function",
        "function": {
            "name": "web_analyze_page",
            "description": (
                "Deep analysis of the current Brave page: classifies site type, "
                "lists interactive elements, detects obstacles (cookie banners, "
                "login walls, CAPTCHAs), and shows web memory context."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_scan_for_obstacles",
            "description": (
                "Scan the current page for obstacles: cookie banners, login walls, "
                "CAPTCHAs, popups, paywalls. Returns list of detected obstacles."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_handle_obstacle",
            "description": (
                "Handle a detected web obstacle. Auto-dismisses cookie banners "
                "and popups. Reports CAPTCHAs, login walls, and paywalls for "
                "manual user intervention."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "obstacle_type": {
                        "type": "string",
                        "description": "Type: 'cookie_banner', 'popup_modal', 'captcha', 'login_wall', 'paywall'. Empty = auto-detect.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Perform a Google search IN Brave Browser and extract results. "
                "Returns titles, URLs, and snippets of top results. "
                "Use for searching from within the browser."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read_article",
            "description": (
                "Read the main text of a web page in Brave. "
                "If URL given, navigates first. Otherwise reads current page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Optional URL to navigate to first. If empty, reads current page.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_research",
            "description": (
                "Research a topic using multiple sources in Brave. Searches, reads "
                "top results, synthesizes findings. Use when user asks to research "
                "or learn about a topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic to research.",
                    },
                    "num_sources": {
                        "type": "integer",
                        "description": "Number of sources to read (default 3, max 5).",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_execute_goal",
            "description": (
                "Execute a high-level web browsing goal autonomously. "
                "Analyzes pages, handles obstacles, and takes action. "
                "Use for complex multi-step web tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Natural language goal (e.g. 'Search for Python tutorials on YouTube').",
                    }
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_smart_click",
            "description": (
                "Smart click in Brave: uses learned selectors from memory, "
                "then text search, then DOM search. Best for clicking elements "
                "when you know the visible label or description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element_description": {
                        "type": "string",
                        "description": "Element text, label, or CSS selector to click.",
                    }
                },
                "required": ["element_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_youtube_search",
            "description": "Search YouTube for a query in Brave Browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "YouTube search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_youtube_play_first",
            "description": "Play the first video from YouTube search results in Brave.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
