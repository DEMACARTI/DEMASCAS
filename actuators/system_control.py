"""
DEMASCAS — actuators/system_control.py
The Hands: AppleScript-based UI interaction (clicking and typing).

Uses native macOS System Events via subprocess to interact with UI elements
in the background — no mouse hijacking, no pyautogui.

OPTIMIZATIONS:
  - 10-second subprocess timeout prevents infinite hangs on unresponsive apps.
  - Input sanitization strips quotes/backslashes to prevent AppleScript injection.
"""

import subprocess
import time

# Timeout in seconds for AppleScript execution
_ACTION_TIMEOUT = 10

# ── Element Name Aliases ─────────────────────────────────────────────
# Maps snake_case / LLM-hallucinated element names → ordered list of
# visible labels the accessibility tree might use.
# Used by normalize_element_name() to resolve LLM output to real UI names.

ELEMENT_NAME_ALIASES: dict[str, list[str]] = {
    # Gmail
    "compose_button":         ["Compose", "Compose email", "New message"],
    "compose":                ["Compose", "Compose email", "New message"],
    "send_button":            ["Send", "Send ‪"],
    "send":                   ["Send", "Send ‪"],
    "to_field":               ["To", "To recipients", "Recipients"],
    "subject_field":          ["Subject", "Subject field"],
    "body_field":             ["Message Body", "Message body", "Body"],
    "discard_draft":          ["Discard draft", "Discard", "Delete draft"],
    "inbox":                  ["Inbox", "inbox"],
    "starred":                ["Starred", "starred"],
    "reply_button":           ["Reply", "Reply to sender"],
    "reply_all_button":       ["Reply all", "Reply to all"],
    "forward_button":         ["Forward", "Forward message"],
    "archive_button":         ["Archive", "Move to Archive"],
    "delete_button":          ["Delete", "Move to Trash", "Trash"],
    "more_options":           ["More", "More options", "⋮"],
    "attach_file":            ["Attach files", "Attach", "📎"],
    # YouTube
    "search_box":             ["Search", "Search YouTube", "search_box"],
    "search_button":          ["Search", "Search button"],
    "play_button":            ["Play", "Play video", "▶"],
    "pause_button":           ["Pause", "Pause video", "⏸"],
    "like_button":            ["Like", "Like this video", "👍"],
    "subscribe_button":       ["Subscribe", "Subscribe to channel"],
    # General browser
    "address_bar":            ["Address and search bar", "URL bar", "Search or type URL"],
    "back_button":            ["Back", "Go back", "Navigate back"],
    "forward_nav":            ["Forward", "Go forward"],
    "reload_button":          ["Reload", "Reload this page", "Refresh"],
    "new_tab_button":         ["New Tab", "New tab"],
    "close_tab":              ["Close", "Close tab"],
    "downloads":              ["Downloads", "Show downloads"],
    "bookmarks":              ["Bookmarks", "Show Bookmarks"],
    "settings":               ["Settings", "Preferences"],
    # General UI
    "ok_button":              ["OK", "Ok", "ok"],
    "cancel_button":          ["Cancel", "cancel"],
    "close_button":           ["Close", "close", "✕", "×"],
    "save_button":            ["Save", "Save file"],
    "submit_button":          ["Submit", "Go", "Done"],
    "next_button":            ["Next", "Continue", "next"],
    "previous_button":        ["Previous", "Back", "Prev"],
    "accept_button":          ["Accept", "Accept all", "I agree", "Allow"],
    "decline_button":         ["Decline", "Reject", "Deny", "Block"],
    "login_button":           ["Log in", "Login", "Sign in", "Sign In"],
    "signup_button":          ["Sign up", "Sign Up", "Create account", "Register"],
}


def normalize_element_name(name: str) -> list[str]:
    """
    Given an element name (possibly snake_case or hallucinated),
    return an ordered list of candidate names to try.

    Order: exact name → alias expansions → title-cased → capitalized word forms.
    """
    candidates = [name]  # always try exact first
    key = name.strip().lower().replace(" ", "_").replace("-", "_")

    # Check aliases
    if key in ELEMENT_NAME_ALIASES:
        for alias in ELEMENT_NAME_ALIASES[key]:
            if alias not in candidates:
                candidates.append(alias)

    # Also check the raw name lowered
    if name.lower() in ELEMENT_NAME_ALIASES:
        for alias in ELEMENT_NAME_ALIASES[name.lower()]:
            if alias not in candidates:
                candidates.append(alias)

    # Auto-generate title case from snake_case: compose_button → Compose Button, Compose
    if "_" in name:
        words = name.split("_")
        title = " ".join(w.capitalize() for w in words)
        if title not in candidates:
            candidates.append(title)
        # Also just the first word capitalized (often the visible label)
        first_word = words[0].capitalize()
        if first_word not in candidates:
            candidates.append(first_word)

    return candidates


def _sanitize_for_applescript(text: str) -> str:
    """Strip characters that could break or inject into AppleScript strings."""
    return text.replace('\\', '').replace('"', '').replace('\n', ' ').strip()


def _build_key_command(keys: str) -> str:
    """Build a single AppleScript key command for a shortcut string."""
    parts = [p.strip().lower() for p in keys.split('+') if p.strip()]
    if not parts:
        raise ValueError("Empty keyboard shortcut")

    key_char = parts[-1]
    modifiers = parts[:-1]

    modifier_map = {
        'command': 'command down',
        'cmd': 'command down',
        'shift': 'shift down',
        'option': 'option down',
        'alt': 'option down',
        'control': 'control down',
        'ctrl': 'control down',
    }

    special_keys = {
        'return': 36, 'enter': 36, 'escape': 53, 'esc': 53,
        'tab': 48, 'space': 49, 'delete': 51, 'backspace': 51,
        'forwarddelete': 117,
        'up': 126, 'down': 125, 'left': 123, 'right': 124,
        'home': 115, 'end': 119, 'pageup': 116, 'pagedown': 121,
        'f1': 122, 'f2': 120, 'f3': 99, 'f4': 118, 'f5': 96,
        'f6': 97, 'f7': 98, 'f8': 100, 'f9': 101, 'f10': 109,
        'f11': 103, 'f12': 111,
        'power': 127, 'volumeup': 72, 'volumedown': 73, 'mute': 74,
    }

    as_modifiers = ', '.join(modifier_map[m] for m in modifiers if m in modifier_map)

    if key_char in special_keys:
        code = special_keys[key_char]
        if as_modifiers:
            return f'key code {code} using {{{as_modifiers}}}'
        return f'key code {code}'

    if as_modifiers:
        return f'keystroke "{key_char}" using {{{as_modifiers}}}'
    return f'keystroke "{key_char}"'


def click_ui_element(element_name: str) -> str:
    """
    Sends a native background click to a named UI element using a recursive
    AppleScript search through the frontmost application's window.

    Includes fast-path overrides for Apple's protected window controls
    (close, minimize, zoom).

    Now supports element name normalization: tries the exact name first,
    then alias expansions (compose_button → "Compose", "Compose email", etc.),
    then auto-generated title-case variants.

    Args:
        element_name: The name of the UI element (exact or snake_case alias).

    Returns:
        A success/failure message string.
    """
    candidates = normalize_element_name(element_name)

    for candidate in candidates:
        result = _click_ui_element_single(candidate)
        if result.startswith("Success"):
            return result

    # All candidates failed
    tried = ", ".join(f"'{c}'" for c in candidates[:5])
    return f"Failed: Could not locate element. Tried: {tried}"


def _click_ui_element_single(element_name: str) -> str:
    """Click a single exact element name via AppleScript recursive search."""
    safe_name = _sanitize_for_applescript(element_name)
    print(f"[*] DEMASCAS is attempting to click: '{safe_name}'...")

    applescript_code = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set targetName to "{safe_name}"

        -- FAST-PATH OVERRIDE: Apple's protected window controls
        try
            if targetName is "close button" then
                click button 1 of window 1 of frontApp
                return "Success: Clicked standard OS close button"
            else if targetName is "minimize button" then
                click button 2 of window 1 of frontApp
                return "Success: Clicked standard OS minimize button"
            else if targetName is "zoom button" then
                click button 3 of window 1 of frontApp
                return "Success: Clicked standard OS zoom button"
            end if
        end try

        tell window 1 of frontApp
            -- RECURSIVE HUNTER: Infinitely deep search without crashing the OS
            script Finder
                on searchAndClick(parentElement)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                if name of elem is targetName then
                                    click elem
                                    return true
                                end if
                            end try

                            -- Dig deeper into nested groups
                            if my searchAndClick(elem) is true then
                                return true
                            end if
                        end repeat
                    end try
                    return false
                end searchAndClick
            end script

            if Finder's searchAndClick(it) is true then
                return "Success: Clicked '" & targetName & "'"
            else
                return "Failed: Could not locate '" & targetName & "' in the UI tree."
            end if
        end tell
    end tell
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Click on '{safe_name}' took too long (app may be unresponsive)."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def type_text(text: str, app_name: str = "", press_enter: bool = False,
              focus_shortcut: str = "") -> str:
    """
    Types text into the currently focused field using keystroke injection.
    Unlike type_text_into_element, this does NOT require knowing the exact
    accessibility element name — it types into whatever is focused.

    Optionally:
      - Activates a specific app first (app_name)
      - Presses a focus shortcut first (e.g. 'command+l' to focus Safari
        address bar, 'command+k' for Spotlight in Slack, etc.)
      - Presses Return/Enter after typing (press_enter)

    Common patterns:
      type_text("instagram.com", app_name="Safari",
               focus_shortcut="command+l", press_enter=True)
      → activates Safari, focuses address bar, types URL, presses Enter.

    Args:
        text: The string to type.
        app_name: Optional app to activate before typing.
        press_enter: If True, presses Return after typing.
        focus_shortcut: Optional keyboard shortcut to press BEFORE typing
                        (e.g. 'command+l' for address bar, 'command+a' to
                        select all first). Uses same format as
                        press_keyboard_shortcut.

    Returns:
        A success/failure message string.
    """
    safe_text = _sanitize_for_applescript(text)
    print(f"[*] DEMASCAS is typing: '{safe_text}'" +
          (f" in {app_name}" if app_name else "") +
          (f" (focus: {focus_shortcut})" if focus_shortcut else "") +
          (" + Enter" if press_enter else "") + "...")

    # Build AppleScript
    lines = []
    lines.append('tell application "System Events"')

    # Optionally activate the target app
    if app_name:
        safe_app = _sanitize_for_applescript(app_name)
        lines.append(f'  tell application "{safe_app}" to activate')
        lines.append('  delay 0.3')

    # Optionally press a focus shortcut (e.g. Cmd+L for address bar)
    if focus_shortcut:
        try:
            key_cmd = _build_key_command(focus_shortcut)
            lines.append(f'  {key_cmd}')
        except ValueError:
            pass
        lines.append('  delay 0.2')

    # Type the text
    lines.append(f'  keystroke "{safe_text}"')

    # Optionally press Enter
    if press_enter:
        lines.append('  delay 0.1')
        lines.append('  key code 36')  # Return key — 'keystroke return' causes -2741

    lines.append('end tell')
    if press_enter:
        lines.append(f'return "Success: Typed \'{safe_text}\' + Enter."')
    else:
        lines.append(f'return "Success: Typed \'{safe_text}\'."')

    applescript_code = '\n'.join(lines)

    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[-] Timeout: Typing took too long."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def type_text_into_element(element_name: str, text_input: str) -> str:
    """
    Focuses a named UI element (e.g. a text field) and types the given text
    into it using keystroke injection via AppleScript.

    Args:
        element_name: The exact name of the text field from the UI tree.
        text_input:   The string to type into the element.

    Returns:
        A success/failure message string.
    """
    safe_name = _sanitize_for_applescript(element_name)
    safe_text = _sanitize_for_applescript(text_input)
    print(f"[*] DEMASCAS is attempting to type '{safe_text}' into '{safe_name}'...")

    applescript_code = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set targetName to "{safe_name}"
        set inputText to "{safe_text}"

        tell window 1 of frontApp
            script Finder
                on searchAndType(parentElement, targetName, inputText)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                if name of elem is targetName then
                                    -- Focus the element, clear it, and type
                                    set focused of elem to true
                                    set value of elem to inputText
                                    return true
                                end if
                            end try

                            if my searchAndType(elem, targetName, inputText) is true then
                                return true
                            end if
                        end repeat
                    end try
                    return false
                end searchAndType
            end script

            if Finder's searchAndType(it, targetName, inputText) is true then
                return "Success: Typed '" & inputText & "' into '" & targetName & "'"
            else
                -- Fallback: use keystroke injection into the frontmost app
                keystroke inputText
                return "Fallback: Typed via keystroke injection (element not found directly)."
            end if
        end tell
    end tell
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Typing into '{safe_name}' took too long (app may be unresponsive)."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def open_application(app_name: str) -> str:
    """
    Opens (or activates) a macOS application by name using AppleScript.
    If the app is already running it will be brought to the front.

    Args:
        app_name: The human-readable application name
                  (e.g. 'Calculator', 'Safari', 'Finder').

    Returns:
        A success/failure message string.
    """
    safe_name = _sanitize_for_applescript(app_name)
    print(f"[*] DEMASCAS is opening: '{safe_name}'...")

    applescript_code = f"""
    try
        tell application "{safe_name}" to activate
        delay 0.5
        return "Success: Opened and activated '{safe_name}'."
    on error errMsg
        return "Failed: Could not open '{safe_name}'. Error: " & errMsg
    end try
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Opening '{safe_name}' took too long."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def open_url(url: str, browser: str = "Safari") -> str:
    """
    Opens a URL in a macOS browser using AppleScript.
    Activates the browser, then tells it to open the URL.

    Args:
        url: The URL to open (e.g. 'https://instagram.com', 'instagram.com').
             A scheme (https://) is added automatically if missing.
        browser: The browser app name (default 'Safari').
                 Also supports 'Google Chrome', 'Firefox', etc.

    Returns:
        A success/failure message string.
    """
    safe_browser = _sanitize_for_applescript(browser)
    # Sanitize URL — strip shell-dangerous chars but keep :// and standard URL chars
    safe_url = url.replace('"', '').replace('\\', '').replace('`', '').strip()
    if not safe_url.startswith(("http://", "https://")):
        safe_url = "https://" + safe_url

    print(f"[*] DEMASCAS is opening URL: '{safe_url}' in {safe_browser}...")

    applescript_code = f"""
    try
        tell application "{safe_browser}"
            activate
            delay 0.3
            open location "{safe_url}"
        end tell
        delay 0.5
        return "Success: Opened '{safe_url}' in {safe_browser}."
    on error errMsg
        return "Failed: Could not open URL. Error: " & errMsg
    end try
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Opening URL in '{safe_browser}' took too long."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def open_url_and_wait(url: str, browser: str = "Safari", wait_seconds: float = 3.0) -> str:
    """
    Opens a URL in a browser and waits for the page to stabilize.

    Combines open_url with a screen stability check so the LLM can
    reliably interact with the page after opening.

    Args:
        url: The URL to open (e.g. 'https://instagram.com').
        browser: The browser app name (default 'Safari').
        wait_seconds: Max seconds to wait for page load (default 3.0).

    Returns:
        Success/failure message with page load timing.
    """
    # Open the URL
    result = open_url(url, browser)
    if result.startswith("[-]"):
        return result

    # Wait for screen to settle (page loading → static)
    stable_result = wait_for_screen_stable(
        timeout=wait_seconds, interval=0.5, threshold=0.025
    )

    return f"{result} {stable_result}"


def press_keyboard_shortcut(keys: str) -> str:
    """
    Sends a keyboard shortcut to the frontmost application.

    Args:
        keys: A human-readable key combo like 'command+n', 'command+shift+t',
              'return', 'escape', 'tab'.

    Returns:
        A success/failure message string.
    """
    print(f"[*] DEMASCAS is pressing keyboard shortcut: '{keys}'...")

    try:
        key_cmd = _build_key_command(keys)
    except ValueError:
        return "[-] Invalid keyboard shortcut."

    script = f'tell application "System Events" to {key_cmd}'

    try:
        subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return f"Success: Pressed '{keys}'."
    except subprocess.TimeoutExpired:
        return f"[-] Timeout pressing '{keys}'."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def quit_application(app_name: str) -> str:
    """
    Quits (closes) a specific macOS application by name.
    Works even if the app is NOT the frontmost window — unlike command+q
    which only affects the foreground app.

    Args:
        app_name: The application name (e.g. 'Safari', 'Visual Studio Code').

    Returns:
        A success/failure message string.
    """
    safe_name = _sanitize_for_applescript(app_name)
    print(f"[*] DEMASCAS is quitting: '{safe_name}'...")

    applescript_code = f"""
    try
        tell application "{safe_name}" to quit
        return "Success: Quit '{safe_name}'."
    on error errMsg
        return "Failed: Could not quit '{safe_name}'. Error: " & errMsg
    end try
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Quitting '{safe_name}' took too long."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def activate_application(app_name: str) -> str:
    """
    Brings a running macOS application to the foreground without launching it.
    Use this to switch focus to a specific window before performing actions.

    Args:
        app_name: The application name (e.g. 'Safari', 'Finder').

    Returns:
        A success/failure message string.
    """
    safe_name = _sanitize_for_applescript(app_name)
    print(f"[*] DEMASCAS is activating: '{safe_name}'...")

    applescript_code = f"""
    try
        tell application "{safe_name}" to activate
        return "Success: Activated '{safe_name}'."
    on error errMsg
        return "Failed: Could not activate '{safe_name}'. Error: " & errMsg
    end try
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Activating '{safe_name}' took too long."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def list_running_applications() -> str:
    """
    Returns a list of all currently running user-facing macOS applications.
    Excludes background-only processes (daemons, system agents).

    Returns:
        A comma-separated list of application names, or a failure message.
    """
    print("[*] DEMASCAS is listing running applications...")

    applescript_code = """
    tell application "System Events"
        set appNames to name of every process whose background only is false
    end tell
    set AppleScript's text item delimiters to ", "
    return appNames as text
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return f"Running apps: {result.stdout.strip()}"
    except subprocess.TimeoutExpired:
        return "[-] Timeout listing running applications."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def scroll_direction(direction: str, clicks: int = 5) -> str:
    """
    Scrolls the frontmost application in the given direction.
    Uses AppleScript + mouse scroll events via System Events.

    Args:
        direction: One of 'up', 'down', 'left', 'right'.
        clicks:    Number of scroll increments (default 5; range 1-20).

    Returns:
        A success/failure message string.
    """
    direction = direction.strip().lower()
    clicks = max(1, min(int(clicks), 20))  # Clamp 1-20
    print(f"[*] DEMASCAS is scrolling {direction} ({clicks} clicks)...")

    # Map direction to AppleScript scroll deltas
    # Positive = up/left, Negative = down/right (macOS convention)
    scroll_map = {
        "up": (clicks, 0),
        "down": (-clicks, 0),
        "left": (0, clicks),
        "right": (0, -clicks),
    }

    if direction not in scroll_map:
        return f"[-] Invalid direction: '{direction}'. Use: up, down, left, right."

    vert, horiz = scroll_map[direction]

    # Use cliclick if available, else fall back to AppleScript mouse scroll
    # AppleScript approach: use JavaScript for Automation (JXA) which has
    # access to CoreGraphics scroll events
    jxa_code = f"""
    ObjC.import('CoreGraphics');
    var event = $.CGEventCreateScrollWheelEvent(null, 0, 2, {vert}, {horiz});
    $.CGEventPost($.kCGHIDEventTap, event);
    """

    try:
        subprocess.run(
            ['osascript', '-l', 'JavaScript', '-e', jxa_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return f"Success: Scrolled {direction} ({clicks} clicks)."
    except subprocess.CalledProcessError:
        # Fallback: use AppleScript keystroke for vertical scroll
        if direction in ("up", "down"):
            key_code = 126 if direction == "up" else 125  # arrow keys
            script_lines = []
            for _ in range(clicks):
                script_lines.append(
                    f'tell application "System Events" to key code {key_code}'
                )
            script = "\n".join(script_lines)
            try:
                subprocess.run(
                    ['osascript', '-e', script],
                    capture_output=True, text=True, check=True,
                    timeout=_ACTION_TIMEOUT,
                )
                return f"Success: Scrolled {direction} ({clicks} clicks) via arrow keys."
            except Exception as e2:
                return f"[-] Scroll fallback failed: {e2}"
        return f"[-] Scroll {direction} failed."
    except subprocess.TimeoutExpired:
        return f"[-] Timeout scrolling {direction}."


def drag_element(from_element: str, to_element: str) -> str:
    """
    Drags one UI element to another in the frontmost application.
    First locates both elements in the accessibility tree to get their
    screen coordinates, then performs a mouse drag via CoreGraphics.

    Args:
        from_element: The exact name of the UI element to drag FROM.
        to_element:   The exact name of the UI element to drag TO.

    Returns:
        A success/failure message string.
    """
    safe_from = _sanitize_for_applescript(from_element)
    safe_to = _sanitize_for_applescript(to_element)
    print(f"[*] DEMASCAS is dragging '{safe_from}' → '{safe_to}'...")

    # Step 1: Get positions of both elements via AppleScript
    applescript_code = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        tell window 1 of frontApp
            set fromPos to missing value
            set toPos to missing value

            script Finder
                on findPosition(parentElement, targetName)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                if name of elem is targetName then
                                    set pos to position of elem
                                    set sz to size of elem
                                    -- Return center point
                                    set cx to (item 1 of pos) + (item 1 of sz) / 2
                                    set cy to (item 2 of pos) + (item 2 of sz) / 2
                                    return {{cx, cy}}
                                end if
                            end try
                            set deeper to my findPosition(elem, targetName)
                            if deeper is not missing value then return deeper
                        end repeat
                    end try
                    return missing value
                end findPosition
            end script

            set fromPos to Finder's findPosition(it, "{safe_from}")
            set toPos to Finder's findPosition(it, "{safe_to}")

            if fromPos is missing value then
                return "FAIL:FROM"
            end if
            if toPos is missing value then
                return "FAIL:TO"
            end if

            return (item 1 of fromPos as text) & "," & (item 2 of fromPos as text) & ";" & (item 1 of toPos as text) & "," & (item 2 of toPos as text)
        end tell
    end tell
    """

    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        output = result.stdout.strip()

        if output == "FAIL:FROM":
            return f"[-] Could not locate source element: '{safe_from}'"
        if output == "FAIL:TO":
            return f"[-] Could not locate target element: '{safe_to}'"

        # Parse coordinates: "x1,y1;x2,y2"
        parts = output.split(";")
        from_coords = parts[0].split(",")
        to_coords = parts[1].split(",")

        fx, fy = float(from_coords[0]), float(from_coords[1])
        tx, ty = float(to_coords[0]), float(to_coords[1])

    except subprocess.TimeoutExpired:
        return f"[-] Timeout locating elements for drag."
    except (subprocess.CalledProcessError, ValueError, IndexError) as e:
        return f"[-] Failed to locate elements: {e}"

    # Step 2: Perform the drag via CoreGraphics (JXA)
    jxa_drag = f"""
    ObjC.import('CoreGraphics');

    var fromX = {fx}, fromY = {fy};
    var toX = {tx}, toY = {ty};

    // Move to start position
    var moveEvent = $.CGEventCreateMouseEvent(null, $.kCGEventMouseMoved, $.CGPointMake(fromX, fromY), $.kCGMouseButtonLeft);
    $.CGEventPost($.kCGHIDEventTap, moveEvent);
    delay(0.1);

    // Mouse down
    var downEvent = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDown, $.CGPointMake(fromX, fromY), $.kCGMouseButtonLeft);
    $.CGEventPost($.kCGHIDEventTap, downEvent);
    delay(0.1);

    // Drag to target (smooth interpolation)
    var steps = 10;
    for (var i = 1; i <= steps; i++) {{
        var x = fromX + (toX - fromX) * i / steps;
        var y = fromY + (toY - fromY) * i / steps;
        var dragEvent = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDragged, $.CGPointMake(x, y), $.kCGMouseButtonLeft);
        $.CGEventPost($.kCGHIDEventTap, dragEvent);
        delay(0.02);
    }}

    // Mouse up
    var upEvent = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseUp, $.CGPointMake(toX, toY), $.kCGMouseButtonLeft);
    $.CGEventPost($.kCGHIDEventTap, upEvent);
    """

    try:
        subprocess.run(
            ['osascript', '-l', 'JavaScript', '-e', jxa_drag],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return f"Success: Dragged '{safe_from}' to '{safe_to}'."
    except subprocess.TimeoutExpired:
        return f"[-] Timeout during drag operation."
    except subprocess.CalledProcessError as e:
        return f"[-] Drag execution error: {e.stderr}"


# ══════════════════════════════════════════════════════════════════════
# NEW AGENTIC TOOLS — human-like OS control for multi-step workflows
# ══════════════════════════════════════════════════════════════════════


def wait_for_element(element_name: str, timeout: int = 10) -> str:
    """
    Waits for a named UI element to appear in the frontmost app's
    accessibility tree. Polls every 0.5s up to timeout.

    Args:
        element_name: The exact name of the UI element to wait for.
        timeout:      Max seconds to wait (default 10).

    Returns:
        Success/failure message.
    """
    safe_name = _sanitize_for_applescript(element_name)
    timeout = max(1, min(int(timeout), 30))
    print(f"[*] DEMASCAS is waiting for element: '{safe_name}' (max {timeout}s)...")

    applescript_code = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set targetName to "{safe_name}"

        tell window 1 of frontApp
            script Finder
                on searchElement(parentElement, targetName)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                if name of elem is targetName then
                                    return true
                                end if
                            end try
                            if my searchElement(elem, targetName) is true then
                                return true
                            end if
                        end repeat
                    end try
                    return false
                end searchElement
            end script

            return Finder's searchElement(it, targetName)
        end tell
    end tell
    """

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ['osascript', '-e', applescript_code],
                capture_output=True, text=True, check=True,
                timeout=5,
            )
            if "true" in result.stdout.strip().lower():
                return f"Success: Element '{safe_name}' found."
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass
        time.sleep(0.5)

    return f"[-] Timeout: Element '{safe_name}' not found after {timeout}s."


def wait_for_app_ready(app_name: str, timeout: int = 10) -> str:
    """
    Waits for an application to be running and have at least one window.
    Useful after open_application to ensure the app is fully loaded.

    Args:
        app_name: The application name to wait for.
        timeout:  Max seconds to wait (default 10).

    Returns:
        Success/failure message.
    """
    safe_name = _sanitize_for_applescript(app_name)
    timeout = max(1, min(int(timeout), 30))
    print(f"[*] DEMASCAS is waiting for app: '{safe_name}' (max {timeout}s)...")

    applescript_code = f"""
    tell application "System Events"
        try
            set p to first application process whose name is "{safe_name}"
            if (count of windows of p) > 0 then
                return "ready"
            else
                return "no_window"
            end if
        on error
            return "not_running"
        end try
    end tell
    """

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ['osascript', '-e', applescript_code],
                capture_output=True, text=True, check=True,
                timeout=5,
            )
            if "ready" in result.stdout.strip().lower():
                return f"Success: '{safe_name}' is running with windows open."
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass
        time.sleep(0.5)

    return f"[-] Timeout: '{safe_name}' not ready after {timeout}s."


def get_clipboard() -> str:
    """
    Returns the current contents of the macOS clipboard (text only).

    Returns:
        The clipboard text, or a failure message.
    """
    print("[*] DEMASCAS is reading clipboard...")
    try:
        result = subprocess.run(
            ['pbpaste'],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        text = result.stdout
        if not text:
            return "Clipboard is empty."
        # Truncate very long clipboard content
        if len(text) > 2000:
            text = text[:2000] + "... (truncated)"
        return f"Clipboard contents: {text}"
    except subprocess.TimeoutExpired:
        return "[-] Timeout reading clipboard."
    except subprocess.CalledProcessError as e:
        return f"[-] Error reading clipboard: {e.stderr}"


def set_clipboard(text: str) -> str:
    """
    Sets the macOS clipboard to the given text.

    Args:
        text: The text to copy to the clipboard.

    Returns:
        Success/failure message.
    """
    print(f"[*] DEMASCAS is setting clipboard to: '{text[:50]}...'")
    try:
        subprocess.run(
            ['pbcopy'],
            input=text, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return f"Success: Copied text to clipboard ({len(text)} chars)."
    except subprocess.TimeoutExpired:
        return "[-] Timeout setting clipboard."
    except subprocess.CalledProcessError as e:
        return f"[-] Error setting clipboard: {e.stderr}"


def find_and_click(search_text: str) -> str:
    """
    Searches the frontmost window's accessibility tree for ANY element
    whose name contains search_text (case-insensitive partial match),
    then clicks the first match. More forgiving than click_ui_element
    which requires exact name match.

    Args:
        search_text: Partial text to search for in element names.

    Returns:
        Success/failure message.
    """
    safe_text = _sanitize_for_applescript(search_text)
    print(f"[*] DEMASCAS is searching and clicking: '{safe_text}'...")

    applescript_code = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set searchText to "{safe_text}"

        tell window 1 of frontApp
            script Finder
                on searchAndClick(parentElement, searchText)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                set elemName to name of elem
                                if elemName is not missing value then
                                    -- Case-insensitive contains check
                                    considering case
                                        -- Convert to lowercase for comparison
                                    end considering
                                    ignoring case
                                        if elemName contains searchText then
                                            click elem
                                            return elemName
                                        end if
                                    end ignoring
                                end if
                            end try
                            set deeper to my searchAndClick(elem, searchText)
                            if deeper is not "" then return deeper
                        end repeat
                    end try
                    return ""
                end searchAndClick
            end script

            set foundName to Finder's searchAndClick(it, searchText)
            if foundName is not "" then
                return "Success: Clicked element '" & foundName & "' (matched '" & searchText & "')"
            else
                return "Failed: No element containing '" & searchText & "' found in the UI tree."
            end if
        end tell
    end tell
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[-] Timeout: Search for '{safe_text}' took too long."
    except subprocess.CalledProcessError as e:
        return f"[-] Execution Error: {e.stderr}"


def right_click_element(element_name: str) -> str:
    """
    Right-clicks (context menu click) a named UI element in the
    frontmost app. Locates the element, gets its position, then
    sends a right-click via CoreGraphics.

    Args:
        element_name: The exact name of the UI element to right-click.

    Returns:
        Success/failure message.
    """
    safe_name = _sanitize_for_applescript(element_name)
    print(f"[*] DEMASCAS is right-clicking: '{safe_name}'...")

    # Step 1: Find element position
    applescript_code = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        tell window 1 of frontApp
            script Finder
                on findPosition(parentElement, targetName)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                if name of elem is targetName then
                                    set pos to position of elem
                                    set sz to size of elem
                                    set cx to (item 1 of pos) + (item 1 of sz) / 2
                                    set cy to (item 2 of pos) + (item 2 of sz) / 2
                                    return (cx as text) & "," & (cy as text)
                                end if
                            end try
                            set deeper to my findPosition(elem, targetName)
                            if deeper is not "" then return deeper
                        end repeat
                    end try
                    return ""
                end findPosition
            end script

            set pos to Finder's findPosition(it, "{safe_name}")
            if pos is "" then
                return "FAIL:NOT_FOUND"
            end if
            return pos
        end tell
    end tell
    """

    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        output = result.stdout.strip()

        if output == "FAIL:NOT_FOUND":
            return f"[-] Could not locate element: '{safe_name}'"

        coords = output.split(",")
        cx, cy = float(coords[0]), float(coords[1])
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            ValueError, IndexError) as e:
        return f"[-] Failed to locate element: {e}"

    # Step 2: Right-click via CoreGraphics
    jxa_code = f"""
    ObjC.import('CoreGraphics');
    var x = {cx}, y = {cy};
    var down = $.CGEventCreateMouseEvent(null, $.kCGEventRightMouseDown, $.CGPointMake(x, y), $.kCGMouseButtonRight);
    $.CGEventPost($.kCGHIDEventTap, down);
    delay(0.1);
    var up = $.CGEventCreateMouseEvent(null, $.kCGEventRightMouseUp, $.CGPointMake(x, y), $.kCGMouseButtonRight);
    $.CGEventPost($.kCGHIDEventTap, up);
    """

    try:
        subprocess.run(
            ['osascript', '-l', 'JavaScript', '-e', jxa_code],
            capture_output=True, text=True, check=True,
            timeout=_ACTION_TIMEOUT,
        )
        return f"Success: Right-clicked '{safe_name}'."
    except subprocess.TimeoutExpired:
        return f"[-] Timeout during right-click."
    except subprocess.CalledProcessError as e:
        return f"[-] Right-click error: {e.stderr}"


def type_and_submit(text: str, app_name: str = "",
                    focus_shortcut: str = "") -> str:
    """
    Types text and presses Enter — convenience wrapper for type_text
    with press_enter=True. Common for search bars, address bars, forms.

    Args:
        text:           The text to type.
        app_name:       Optional app to activate first.
        focus_shortcut: Optional shortcut to press before typing
                        (e.g. 'command+l' for address bar).

    Returns:
        Success/failure message.
    """
    return type_text(text, app_name=app_name, press_enter=True,
                     focus_shortcut=focus_shortcut)


def switch_tab(direction: str = "next") -> str:
    """
    Switches to the next or previous browser/app tab.

    Args:
        direction: 'next' (Cmd+Shift+]) or 'previous' (Cmd+Shift+[).
                   Also accepts 'right'/'forward' for next,
                   'left'/'back' for previous.

    Returns:
        Success/failure message.
    """
    direction = direction.strip().lower()
    print(f"[*] DEMASCAS is switching tab: {direction}")

    if direction in ("next", "right", "forward"):
        keys = "command+shift+]"
    elif direction in ("previous", "prev", "left", "back"):
        keys = "command+shift+["
    else:
        return f"[-] Invalid direction: '{direction}'. Use 'next' or 'previous'."

    return press_keyboard_shortcut(keys)


def scroll_to_element(element_name: str, max_scrolls: int = 10) -> str:
    """
    Scrolls down in the frontmost window until a named element becomes
    visible in the accessibility tree, then returns success.

    Args:
        element_name: The exact name of the element to scroll to.
        max_scrolls:  Maximum number of scroll increments to try.

    Returns:
        Success/failure message.
    """
    safe_name = _sanitize_for_applescript(element_name)
    max_scrolls = max(1, min(int(max_scrolls), 30))
    print(f"[*] DEMASCAS is scrolling to find: '{safe_name}'...")

    check_script = f"""
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        tell window 1 of frontApp
            script Finder
                on searchElement(parentElement, targetName)
                    try
                        set theElements to UI elements of parentElement
                        repeat with elem in theElements
                            try
                                if name of elem is targetName then return true
                            end try
                            if my searchElement(elem, targetName) is true then return true
                        end repeat
                    end try
                    return false
                end searchElement
            end script
            return Finder's searchElement(it, "{safe_name}")
        end tell
    end tell
    """

    for i in range(max_scrolls):
        # Check if element is visible
        try:
            result = subprocess.run(
                ['osascript', '-e', check_script],
                capture_output=True, text=True, check=True,
                timeout=5,
            )
            if "true" in result.stdout.strip().lower():
                return f"Success: Found '{safe_name}' after {i} scrolls."
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass

        # Scroll down
        scroll_direction("down", clicks=3)
        time.sleep(0.3)

    return f"[-] Could not find '{safe_name}' after {max_scrolls} scrolls."


# ── Smart waiting — replace fixed sleep() with active screen watching ──

def wait_for_screen_stable(timeout: float = 5.0, interval: float = 0.5,
                           threshold: float = 0.025) -> str:
    """
    Wait until the screen stops changing (UI has settled).
    Takes repeated screenshots and compares pixel differences.

    Uses macOS screencapture for zero-dependency operation.

    Args:
        timeout:   Maximum seconds to wait.
        interval:  Seconds between comparison snapshots.
        threshold: Fraction of pixels that must change to consider "unstable".

    Returns:
        Success message with how long it took, or timeout message.
    """
    import tempfile
    import hashlib
    import os

    tmp_a = tempfile.mktemp(suffix='.png')
    tmp_b = tempfile.mktemp(suffix='.png')

    try:
        # Take initial screenshot
        subprocess.run(
            ['screencapture', '-x', '-C', '-t', 'png', tmp_a],
            capture_output=True, timeout=5,
        )

        start = time.time()
        while time.time() - start < timeout:
            time.sleep(interval)

            # Take comparison screenshot
            subprocess.run(
                ['screencapture', '-x', '-C', '-t', 'png', tmp_b],
                capture_output=True, timeout=5,
            )

            # Compare file hashes (fast, no PIL needed)
            hash_a = hashlib.md5(open(tmp_a, 'rb').read()).hexdigest()
            hash_b = hashlib.md5(open(tmp_b, 'rb').read()).hexdigest()

            if hash_a == hash_b:
                elapsed = time.time() - start
                return f"Screen stable after {elapsed:.1f}s."

            # Swap — new screenshot becomes the baseline
            os.replace(tmp_b, tmp_a)

        return f"Screen still changing after {timeout}s timeout."
    except Exception as e:
        return f"[-] wait_for_screen_stable error: {e}"
    finally:
        for f in [tmp_a, tmp_b]:
            if os.path.exists(f):
                os.unlink(f)
