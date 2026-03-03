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

# Timeout in seconds for AppleScript execution
_ACTION_TIMEOUT = 10


def _sanitize_for_applescript(text: str) -> str:
    """Strip characters that could break or inject into AppleScript strings."""
    return text.replace('\\', '').replace('"', '').replace('\n', ' ').strip()


def click_ui_element(element_name: str) -> str:
    """
    Sends a native background click to a named UI element using a recursive
    AppleScript search through the frontmost application's window.

    Includes fast-path overrides for Apple's protected window controls
    (close, minimize, zoom).

    Args:
        element_name: The exact name of the UI element as it appears in the
                      accessibility tree (e.g. 'close button', 'Save').

    Returns:
        A success/failure message string.
    """
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

    # Parse the combo into AppleScript modifiers + key
    parts = [p.strip().lower() for p in keys.split('+')]
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

    # Special key names that need 'key code' instead of 'keystroke'
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

    as_modifiers = ', '.join(modifier_map.get(m, '') for m in modifiers if m in modifier_map)

    if key_char in special_keys:
        code = special_keys[key_char]
        if as_modifiers:
            script = f'tell application "System Events" to key code {code} using {{{as_modifiers}}}'
        else:
            script = f'tell application "System Events" to key code {code}'
    else:
        if as_modifiers:
            script = f'tell application "System Events" to keystroke "{key_char}" using {{{as_modifiers}}}'
        else:
            script = f'tell application "System Events" to keystroke "{key_char}"'

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
