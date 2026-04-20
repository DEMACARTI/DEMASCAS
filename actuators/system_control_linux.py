"""
DEMASCAS — actuators/system_control_linux.py
The Hands: Linux UI automation via xdotool + AT-SPI2.

Uses:
  - xdotool for keyboard/mouse input simulation
  - AT-SPI2 (Assistive Technology Service Provider Interface) for reading UI trees
  - xprop/xwininfo for window information

Install dependencies:
  sudo pacman -S xdotool libatspi2 python-pyatspi

Usage:
  Same function signatures as system_control.py (macOS version)
"""

import os
import subprocess
import time
from typing import Optional, List, Dict, Any

# Timeout in seconds for command execution
_ACTION_TIMEOUT = 10

# ── Element Name Aliases (same as macOS version) ─────────────────────
ELEMENT_NAME_ALIASES: dict[str, list[str]] = {
    # Gmail
    "compose_button": ["Compose", "Compose email", "New message"],
    "compose": ["Compose", "Compose email", "New message"],
    "send_button": ["Send"],
    "send": ["Send"],
    "to_field": ["To", "To recipients", "Recipients"],
    "subject_field": ["Subject", "Subject field"],
    "body_field": ["Message Body", "Message body", "Body"],
    # General UI
    "ok_button": ["OK", "Ok"],
    "cancel_button": ["Cancel"],
    "close_button": ["Close", "close"],
    "save_button": ["Save"],
    "submit_button": ["Submit", "Go", "Done"],
}


def normalize_element_name(name: str) -> list[str]:
    """Return ordered list of candidate names to try."""
    candidates = [name]
    key = name.strip().lower().replace(" ", "_").replace("-", "_")

    if key in ELEMENT_NAME_ALIASES:
        for alias in ELEMENT_NAME_ALIASES[key]:
            if alias not in candidates:
                candidates.append(alias)

    if "_" in name:
        words = name.split("_")
        title = " ".join(w.capitalize() for w in words)
        if title not in candidates:
            candidates.append(title)
        if words[0].capitalize() not in candidates:
            candidates.append(words[0].capitalize())

    return candidates


# ── Helper: Run shell command with timeout ───────────────────────────

def _run_cmd(cmd: List[str], timeout: float = _ACTION_TIMEOUT) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


# ── Window Management ────────────────────────────────────────────────

def get_active_window() -> Optional[Dict[str, Any]]:
    """Get info about the currently active window."""
    # Get active window ID via xdotool
    ret, stdout, _ = _run_cmd(["xdotool", "getactivewindow"])
    if ret != 0:
        return None

    window_id = stdout.strip()

    # Get window name
    ret, name, _ = _run_cmd(["xdotool", "getwindowname", window_id])
    if ret != 0:
        name = "Unknown"

    # Get window class
    ret, winclass, _ = _run_cmd(["xdotool", "getwindowclassname", window_id])
    if ret != 0:
        winclass = "unknown"

    # Get window geometry
    ret, geom, _ = _run_cmd(["xdotool", "getwindowgeometry", "--shell", window_id])
    geometry = {}
    if ret == 0:
        for line in geom.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                geometry[k] = int(v)

    return {
        "id": window_id,
        "name": name.strip(),
        "class": winclass.strip().replace(".", ""),
        "geometry": geometry,
    }


def list_open_windows() -> str:
    """List all visible windows."""
    ret, stdout, _ = _run_cmd(["xdotool", "search", "--onlyvisible", "--name", ""])
    if ret != 0:
        return "[-] No windows found"

    windows = []
    for wid in stdout.strip().split("\n"):
        if wid:
            ret, name, _ = _run_cmd(["xdotool", "getwindowname", wid])
            windows.append(f"  [{wid}] {name.strip()}")

    return "Open windows:\n" + "\n".join(windows)


# ── AT-SPI2 Accessibility Tree ───────────────────────────────────────

def _get_atspi_tree() -> Optional[Any]:
    """Get AT-SPI2 accessibility tree. Returns pyatspi root or None."""
    try:
        import pyatspi
        desktop = pyatspi.Registry.getDesktop(0)
        return desktop
    except Exception as e:
        print(f"[!] AT-SPI2 not available: {e}")
        return None


def _find_element_by_name(node: Any, name: str, depth: int = 0, max_depth: int = 10) -> Optional[Any]:
    """Recursively search AT-SPI tree for element with matching name."""
    if depth > max_depth:
        return None

    try:
        # Check current node
        if hasattr(node, "name") and node.name:
            if name.lower() in node.name.lower():
                return node

        # Search children
        if hasattr(node, "__iter__"):
            for child in node:
                result = _find_element_by_name(child, name, depth + 1, max_depth)
                if result:
                    return result
    except Exception:
        pass

    return None


def get_active_window_tree(app_name: Optional[str] = None) -> str:
    """
    Read the accessibility tree of the active window.
    Returns structured text representation of UI elements.
    """
    desktop = _get_atspi_tree()
    if not desktop:
        return "[-] AT-SPI2 not available. Ensure accessibility is enabled."

    try:
        # Get active application
        if app_name:
            for app in desktop:
                if app.name and app_name.lower() in app.name.lower():
                    tree = _tree_to_string(app, max_depth=6)
                    return f"Application: {app.name}\n{tree}"
            return f"[-] Application '{app_name}' not found"
        else:
            # Get all applications
            result = []
            for app in desktop:
                if app.name:
                    result.append(f"Application: {app.name}")
                    result.append(_tree_to_string(app, max_depth=4))
            return "\n".join(result)
    except Exception as e:
        return f"[-] Error reading accessibility tree: {e}"


def _tree_to_string(node: Any, indent: int = 0, max_depth: int = 10, current_depth: int = 0) -> str:
    """Convert AT-SPI node to string representation."""
    if current_depth > max_depth:
        return ""

    lines = []
    prefix = "  " * indent

    try:
        if hasattr(node, "name") and node.name:
            role = str(node.getRoleName()) if hasattr(node, "getRoleName") else "unknown"
            lines.append(f"{prefix}{node.name} [{role}]")

        if hasattr(node, "__iter__"):
            for child in node:
                child_str = _tree_to_string(child, indent + 1, max_depth, current_depth + 1)
                if child_str:
                    lines.append(child_str)
    except Exception:
        pass

    return "\n".join(lines)


# ── UI Interaction ───────────────────────────────────────────────────

def click_ui_element(element_name: str) -> str:
    """
    Click a UI element by name using AT-SPI2 + xdotool fallback.
    """
    # Try AT-SPI2 first
    desktop = _get_atspi_tree()
    if desktop:
        for app in desktop:
            element = _find_element_by_name(app, element_name)
            if element:
                try:
                    # Use AT-SPI doClick action
                    actions = element.queryAction()
                    for i in range(actions.nActions):
                        if "click" in actions.getName(i).lower():
                            actions.doAction(i)
                            return f"[+] Clicked '{element_name}' via AT-SPI"
                    # Fallback: click at position
                    if hasattr(element, "getPosition") and hasattr(element, "getSize"):
                        pos = element.getPosition(0)
                        size = element.getSize()
                        x = pos.x + size.width // 2
                        y = pos.y + size.height // 2
                        ret, _, _ = _run_cmd(["xdotool", "mousemove", str(x), str(y), "click", "1"])
                        if ret == 0:
                            return f"[+] Clicked '{element_name}' at ({x}, {y})"
                except Exception as e:
                    pass

    # Fallback: xdotool search by window name
    ret, stdout, _ = _run_cmd(["xdotool", "search", "--name", element_name])
    if ret == 0 and stdout.strip():
        window_id = stdout.strip().split("\n")[0]
        _run_cmd(["xdotool", "windowactivate", window_id])
        _run_cmd(["xdotool", "click", "1"])
        return f"[+] Clicked window matching '{element_name}'"

    return f"[-] Element '{element_name}' not found"


def find_and_click(search_text: str) -> str:
    """Search for element by partial name match and click it."""
    return click_ui_element(search_text)


def right_click_element(element_name: str) -> str:
    """Right-click a UI element."""
    # Click the element first
    click_result = click_ui_element(element_name)
    if "[-]" in click_result:
        return click_result

    # Then right-click
    ret, _, _ = _run_cmd(["xdotool", "click", "3"])
    if ret == 0:
        return f"[+] Right-clicked '{element_name}'"
    return "[-] Right-click failed"


def type_text(text: str, app_name: Optional[str] = None,
              press_enter: bool = False,
              focus_shortcut: Optional[str] = None) -> str:
    """
    Type text into the currently focused field.
    """
    # Activate app if specified
    if app_name:
        ret, _, _ = _run_cmd(["xdotool", "search", "--name", app_name, "windowactivate"])
        if ret != 0:
            # Try launching the app
            _run_cmd(["xdotool", "search", "--class", app_name])

    # Press focus shortcut if provided
    if focus_shortcut:
        keys = focus_shortcut.replace("+", "").replace("command", "ctrl").replace("⌘", "ctrl")
        _run_cmd(["xdotool", "key", keys])
        time.sleep(0.1)

    # Type the text
    ret, _, _ = _run_cmd(["xdotool", "type", "--", text])
    if ret != 0:
        return f"[-] Failed to type text"

    # Press Enter if requested
    if press_enter:
        _run_cmd(["xdotool", "key", "Return"])

    return f"[+] Typed '{text[:50]}{'...' if len(text) > 50 else ''}'"


def type_text_into_element(element_name: str, text_input: str) -> str:
    """Focus a named element and type into it."""
    # Click the element first to focus it
    click_result = click_ui_element(element_name)
    if "[-]" in click_result:
        return click_result

    time.sleep(0.1)
    return type_text(text_input)


def type_and_submit(text: str, app_name: Optional[str] = None,
                    focus_shortcut: Optional[str] = None) -> str:
    """Type text and press Enter."""
    return type_text(text, app_name, press_enter=True, focus_shortcut=focus_shortcut)


# ── Keyboard Shortcuts ───────────────────────────────────────────────

def press_keyboard_shortcut(keys: str) -> str:
    """
    Press a keyboard shortcut.
    Converts macOS-style to Linux-style:
      command → ctrl, option → alt, control → ctrl
    """
    # Convert macOS modifiers to Linux
    linux_keys = keys.lower()
    linux_keys = linux_keys.replace("command", "ctrl")
    linux_keys = linux_keys.replace("cmd", "ctrl")
    linux_keys = linux_keys.replace("⌘", "ctrl")
    linux_keys = linux_keys.replace("option", "alt")
    linux_keys = linux_keys.replace("alt", "alt")
    linux_keys = linux_keys.replace("⌥", "alt")
    linux_keys = linux_keys.replace("control", "ctrl")
    linux_keys = linux_keys.replace("⌃", "ctrl")
    linux_keys = linux_keys.replace("shift", "shift")
    linux_keys = linux_keys.replace("⇧", "shift")

    # Handle special keys
    special_keys = {
        "return": "Return", "enter": "Return", "escape": "Escape",
        "tab": "Tab", "backspace": "BackSpace", "delete": "Delete",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
        "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5",
        "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10",
        "f11": "F11", "f12": "F12",
    }

    # Split and convert
    key_parts = linux_keys.split("+")
    converted = []
    for part in key_parts:
        part = part.strip()
        if part in special_keys:
            converted.append(special_keys[part])
        elif part not in ("ctrl", "alt", "shift", "super"):
            converted.append(part)
        else:
            converted.append(part)

    xdotool_keys = "+".join(converted)
    ret, _, _ = _run_cmd(["xdotool", "key", xdotool_keys])

    if ret == 0:
        return f"[+] Pressed {keys} → {xdotool_keys}"
    return f"[-] Failed to press shortcut: {keys}"


# ── Application Management ───────────────────────────────────────────

def open_application(app_name: str) -> str:
    """Open or activate an application."""
    # Check if already running
    ret, stdout, _ = _run_cmd(["xdotool", "search", "--name", app_name])
    if ret == 0 and stdout.strip():
        window_id = stdout.strip().split("\n")[0]
        _run_cmd(["xdotool", "windowactivate", window_id])
        return f"[+] Activated existing '{app_name}'"

    # Try to launch via xdg-open or common paths
    app_lower = app_name.lower()

    # Map common app names to Linux executables
    app_map = {
        "firefox": "firefox",
        "google chrome": "google-chrome",
        "chrome": "google-chrome",
        "brave": "brave",
        "visual studio code": "code",
        "vs code": "code",
        "terminal": "gnome-terminal",
        "files": "nautilus",
        "calculator": "gnome-calculator",
        "text editor": "gedit",
    }

    cmd_name = app_map.get(app_lower, app_lower)

    # Try xdg-open first (for URLs and default apps)
    ret, _, _ = _run_cmd(["xdg-open", cmd_name], timeout=3)
    if ret == 0:
        return f"[+] Opened '{app_name}'"

    # Try direct execution
    ret, _, _ = _run_cmd([cmd_name], timeout=3)
    if ret == 0:
        return f"[+] Launched '{app_name}'"

    return f"[-] Could not open '{app_name}'"


def activate_application(app_name: str) -> str:
    """Bring an application to the foreground."""
    return open_application(app_name)


def open_url(url: str, browser: str = "firefox") -> str:
    """
    Open a URL in a browser on Linux.

    Tries a named browser first when provided, then falls back to xdg-open.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    browser_map = {
        "chrome": "google-chrome",
        "google chrome": "google-chrome",
        "brave": "brave",
        "brave browser": "brave",
        "firefox": "firefox",
    }
    browser_cmd = browser_map.get(browser.lower().strip(), browser.strip())

    if browser_cmd:
        ret, _, _ = _run_cmd([browser_cmd, url], timeout=5)
        if ret == 0:
            return f"[+] Opened {url} in {browser}"

    ret, _, _ = _run_cmd(["xdg-open", url], timeout=5)
    if ret == 0:
        return f"[+] Opened {url}"

    return f"[-] Could not open URL: {url}"


def open_url_and_wait(url: str, browser: str = "firefox", wait_seconds: float = 3.0) -> str:
    """Open a URL and wait briefly for the browser/page to initialize."""
    result = open_url(url, browser=browser)
    if result.startswith("[-]"):
        return result
    time.sleep(max(0.0, float(wait_seconds)))
    return f"{result} (waited {wait_seconds:.1f}s)"


def quit_application(app_name: str) -> str:
    """Quit an application."""
    # Find windows matching the name
    ret, stdout, _ = _run_cmd(["xdotool", "search", "--name", app_name])
    if ret != 0 or not stdout.strip():
        return f"[-] Application '{app_name}' not found"

    window_ids = stdout.strip().split("\n")
    for wid in window_ids:
        _run_cmd(["xdotool", "windowclose", wid])

    return f"[+] Closed '{app_name}'"


def switch_tab(direction: str = "next") -> str:
    """
    Switch browser/app tabs using common Linux shortcuts.

    next     -> Ctrl+Tab
    previous -> Ctrl+Shift+Tab
    """
    direction = (direction or "next").strip().lower()
    if direction == "previous":
        return press_keyboard_shortcut("ctrl+shift+tab")
    return press_keyboard_shortcut("ctrl+tab")


def list_running_applications() -> str:
    """List all running applications."""
    ret, stdout, _ = _run_cmd(["xdotool", "search", "--onlyvisible", "--name", ""])
    if ret != 0:
        return "[-] No applications found"

    apps = set()
    for wid in stdout.strip().split("\n"):
        if wid:
            ret, winclass, _ = _run_cmd(["xdotool", "getwindowclassname", wid])
            if ret == 0:
                apps.add(winclass.strip())

    return "Running applications:\n" + "\n".join(sorted(apps))


# ── Scrolling ────────────────────────────────────────────────────────

def scroll_direction(direction: str, clicks: int = 5) -> str:
    """Scroll in a direction."""
    direction = direction.lower()
    key_map = {
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
    }

    if direction not in key_map:
        return f"[-] Invalid direction: {direction}"

    key = key_map[direction]
    for _ in range(clicks):
        _run_cmd(["xdotool", "key", key])

    return f"[+] Scrolled {direction} ({clicks} clicks)"


def scroll_to_element(element_name: str, max_scrolls: int = 10) -> str:
    """Scroll until an element is visible."""
    for i in range(max_scrolls):
        # Check if element is visible
        desktop = _get_atspi_tree()
        if desktop:
            for app in desktop:
                element = _find_element_by_name(app, element_name)
                if element:
                    return f"[+] Found '{element_name}' after {i} scrolls"

        # Scroll down
        _run_cmd(["xdotool", "key", "Down"])
        time.sleep(0.2)

    return f"[-] Element '{element_name}' not found after {max_scrolls} scrolls"


# ── Drag and Drop ────────────────────────────────────────────────────

def drag_element(from_element: str, to_element: str) -> str:
    """Drag one element to another."""
    # Find source element position
    desktop = _get_atspi_tree()
    if not desktop:
        return "[-] AT-SPI2 not available"

    src = None
    dst = None

    for app in desktop:
        if not src:
            src = _find_element_by_name(app, from_element)
        if not dst:
            dst = _find_element_by_name(app, to_element)

    if not src or not dst:
        return f"[-] Could not find elements"

    try:
        src_pos = src.getPosition(0)
        src_size = src.getSize()
        dst_pos = dst.getPosition(0)
        dst_size = dst.getSize()

        src_x = src_pos.x + src_size.width // 2
        src_y = src_pos.y + src_size.height // 2
        dst_x = dst_pos.x + dst_size.width // 2
        dst_y = dst_pos.y + dst_size.height // 2

        # Move to source and drag
        _run_cmd(["xdotool", "mousemove", str(src_x), str(src_y)])
        _run_cmd(["xdotool", "mousedown", "1"])
        time.sleep(0.2)
        _run_cmd(["xdotool", "mousemove", str(dst_x), str(dst_y)])
        time.sleep(0.2)
        _run_cmd(["xdotool", "mouseup", "1"])

        return f"[+] Dragged '{from_element}' to '{to_element}'"
    except Exception as e:
        return f"[-] Drag failed: {e}"


# ── Wait Helpers ─────────────────────────────────────────────────────

def wait_for_element(element_name: str, timeout: int = 10) -> str:
    """Wait for an element to appear."""
    start = time.time()
    while time.time() - start < timeout:
        desktop = _get_atspi_tree()
        if desktop:
            for app in desktop:
                element = _find_element_by_name(app, element_name)
                if element:
                    return f"[+] Found '{element_name}'"
        time.sleep(0.5)

    return f"[-] Element '{element_name}' not found within {timeout}s"


def wait_for_app_ready(app_name: str, timeout: int = 10) -> str:
    """Wait for an application to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        ret, _, _ = _run_cmd(["xdotool", "search", "--name", app_name])
        if ret == 0:
            return f"[+] Application '{app_name}' is ready"
        time.sleep(0.5)

    return f"[-] Application '{app_name}' not found within {timeout}s"


# ── Clipboard ────────────────────────────────────────────────────────

def get_clipboard() -> str:
    """Get clipboard contents."""
    ret, stdout, _ = _run_cmd(["xclip", "-selection", "clipboard", "-o"])
    if ret == 0:
        return stdout
    # Try xsel as fallback
    ret, stdout, _ = _run_cmd(["xsel", "--clipboard", "--output"])
    if ret == 0:
        return stdout
    return "[-] Clipboard empty or xclip/xsel not installed"


def set_clipboard(text: str) -> str:
    """Set clipboard contents."""
    # Try xclip first
    proc = subprocess.Popen(
        ["xclip", "-selection", "clipboard"],
        stdin=subprocess.PIPE, text=True
    )
    proc.communicate(text)
    if proc.returncode == 0:
        return f"[+] Copied to clipboard"

    # Fallback to xsel
    proc = subprocess.Popen(
        ["xsel", "--clipboard", "--input"],
        stdin=subprocess.PIPE, text=True
    )
    proc.communicate(text)
    if proc.returncode == 0:
        return f"[+] Copied to clipboard"

    return "[-] Failed to set clipboard (install xclip or xsel)"


# ── URL/App Detection Helpers (for run.sh integration) ───────────────

def extract_url(command: str) -> Optional[Dict[str, str]]:
    """Extract URL from command (simple heuristic)."""
    import re
    url_pattern = r'(https?://[^\s]+)|([a-zA-Z0-9-]+\.[a-z]{2,}(/\S*)?)'
    match = re.search(url_pattern, command)
    if match:
        url = match.group(1) or match.group(2)
        if not url.startswith("http"):
            url = "https://" + url
        return {"url": url}
    return None


def extract_app_open(command: str) -> Optional[Dict[str, str]]:
    """Extract app name from 'open X' command."""
    lower = command.lower()
    if lower.startswith("open "):
        app_name = command[5:].strip()
        # Filter out URL-like commands
        if "http" not in app_name and "." not in app_name.split()[0]:
            return {"app_name": app_name}
    return None


def extract_app_close(command: str) -> Optional[Dict[str, str]]:
    """Extract app name from 'close X' or 'quit X' command."""
    lower = command.lower()
    for prefix in ["close ", "quit ", "exit "]:
        if lower.startswith(prefix):
            app_name = command[len(prefix):].strip()
            if "window" not in app_name and "tab" not in app_name:
                return {"app_name": app_name}
    return None
