"""
DEMASCAS — sensors/vision.py
The Eyes: macOS Accessibility Tree extraction & multi-window awareness.

  - get_active_window_tree(app_name=None) — UI tree of frontmost or named app
  - list_open_windows()                   — all visible windows across all apps

Uses native macOS System Events via subprocess. No third-party deps.

OPTIMIZATIONS:
  - 5-second subprocess timeout prevents indefinite hangs on heavy UIs.
  - O(n) set-based deduplication replaces O(n²) dict.fromkeys approach.
"""

import subprocess
import os
import pathlib

# Timeout in seconds for the osascript subprocess
_VISION_TIMEOUT = 5


def get_active_window_tree(app_name: str | None = None) -> str:
    """
    Queries macOS System Events via AppleScript to return a text-based map
    of a window's UI elements.

    If app_name is provided, scans that specific application.
    Otherwise, scans the currently frontmost application.

    Uses a safe 3-level deep scan to avoid -1700 AppleScript crashes on
    deeply nested accessibility trees.

    Args:
        app_name: Optional — the name of the application to scan
                  (e.g. 'Safari', 'Finder'). If None, scans the frontmost app.

    Returns:
        A formatted string containing the app name and its UI tree,
        or an error message if Accessibility permissions are not granted.
    """
    if app_name:
        # Sanitize
        safe_name = app_name.replace('\\', '').replace('"', '').strip()
        target_line = f'set frontProcess to application process "{safe_name}"'
    else:
        target_line = "set frontProcess to first application process whose frontmost is true"

    applescript_code = f"""
    tell application "System Events"
        {target_line}
        set appName to name of frontProcess

        if not (exists (window 1 of frontProcess)) then
            return "ACTIVE APP: " & appName & "\\nSTATUS: No active windows."
        end if

        set theWindow to window 1 of frontProcess
        set elementList to ""
        script TreeWalker
            property maxDepth : 6
            property maxNodes : 600
            property nodeCount : 0

            on labelOf(elem)
                set lbl to ""
                try
                    set lbl to name of elem
                end try
                if lbl is missing value or lbl is "" then
                    try
                        set lbl to description of elem
                    end try
                end if
                if lbl is missing value or lbl is "" then
                    try
                        set lbl to value of elem as text
                    end try
                end if
                if lbl is missing value then set lbl to ""
                return lbl
            end labelOf

            on walk(parentElem, depth)
                if depth > maxDepth then return ""
                if nodeCount > maxNodes then return ""

                set outText to ""
                try
                    set children to UI elements of parentElem
                    repeat with childElem in children
                        set nodeCount to nodeCount + 1
                        if nodeCount > maxNodes then exit repeat

                        set lbl to ""
                        try
                            set lbl to my labelOf(childElem)
                        end try

                        if lbl is not "" then
                            try
                                set outText to outText & "- [" & (class of childElem) & "] : '" & lbl & "'\\n"
                            end try
                        end if

                        try
                            set outText to outText & my walk(childElem, depth + 1)
                        end try
                    end repeat
                end try
                return outText
            end walk
        end script

        set elementList to TreeWalker's walk(theWindow, 1)
        if elementList is "" then
            return "ACTIVE APP: " & appName & "\\nSTATUS: Window found but no readable UI labels."
        end if

        return "ACTIVE APP: " & appName & "\\nUI TREE MAP:\\n" & elementList
    end tell
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_VISION_TIMEOUT,
        )
        # Deduplicate lines with O(n) seen-set (preserves order)
        lines = result.stdout.strip().split('\n')
        seen: set[str] = set()
        unique_lines: list[str] = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                unique_lines.append(line)
        return '\n'.join(unique_lines)
    except subprocess.TimeoutExpired:
        return "[-] Accessibility Error: UI tree scan timed out (app may have too many elements)."
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip()
        if app_name:
            return f"[-] Could not scan '{app_name}': {err}"
        return f"[-] Accessibility Error: {err}"


def list_open_windows() -> str:
    """
    Lists all currently visible windows across all running applications.
    Returns each app with its window titles — useful for multi-window
    navigation and deciding which app to activate or inspect.

    Returns:
        A formatted string listing all apps and their window titles,
        or an error message.
    """
    print("[*] DEMASCAS is listing all open windows...")

    applescript_code = """
    set output to ""
    tell application "System Events"
        set allProcs to every application process whose background only is false
        repeat with proc in allProcs
            set procName to name of proc
            try
                set wins to windows of proc
                if (count of wins) > 0 then
                    set output to output & procName & ":\\n"
                    repeat with w in wins
                        try
                            set wTitle to name of w
                            if wTitle is not missing value and wTitle is not "" then
                                set output to output & "  - " & wTitle & "\\n"
                            else
                                set output to output & "  - (untitled window)\\n"
                            end if
                        on error
                            set output to output & "  - (window)\\n"
                        end try
                    end repeat
                end if
            end try
        end repeat
    end tell
    if output is "" then
        return "No visible windows found."
    end if
    return output
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript_code],
            capture_output=True, text=True, check=True,
            timeout=_VISION_TIMEOUT,
        )
        return result.stdout.strip() or "No visible windows found."
    except subprocess.TimeoutExpired:
        return "[-] Timeout listing open windows."
    except subprocess.CalledProcessError as e:
        return f"[-] Error listing windows: {e.stderr}"


# ---------------------------------------------------------------------------
# Compressed accessibility tree — reduces ~1000 tokens to ~150 tokens
# ---------------------------------------------------------------------------

def compress_tree(tree_text: str) -> str:
    """
    Compress the raw accessibility tree output to reduce token usage.
    Keeps only actionable elements (buttons, text fields, links, menus)
    and removes pure containers (groups, scroll areas, etc.).

    Args:
        tree_text: Raw output from get_active_window_tree()

    Returns:
        Compressed tree text with only actionable elements.
    """
    if not tree_text or tree_text.startswith("[-]"):
        return tree_text

    # Actionable element types to keep
    KEEP_TYPES = {
        'button', 'text field', 'static text', 'link',
        'menu item', 'menu button', 'pop up button',
        'check box', 'radio button', 'combo box',
        'tab', 'tab group', 'toolbar',
        'search field', 'text area', 'slider',
    }

    lines = tree_text.split('\n')
    result = []

    for line in lines:
        # Keep header lines (ACTIVE APP, UI TREE MAP, STATUS)
        if not line.startswith("- ["):
            result.append(line)
            continue

        # Extract element type: "- [button] : 'Submit'"
        bracket_start = line.find('[')
        bracket_end = line.find(']')
        if bracket_start < 0 or bracket_end < 0:
            continue

        elem_type = line[bracket_start + 1:bracket_end].lower().strip()

        if elem_type in KEEP_TYPES:
            result.append(line)

    return '\n'.join(result)


def get_active_window_tree_compact(app_name: str | None = None) -> str:
    """
    Get compressed accessibility tree — actionable elements only.
    Use this for LLM context injection to save tokens.
    """
    raw = get_active_window_tree(app_name)
    return compress_tree(raw)


# ---------------------------------------------------------------------------
# Screenshot capture — base64 encoded for VLM input
# ---------------------------------------------------------------------------

# Retina displays produce screenshots at 2x native resolution.
# macOS mouse events use logical "point" coordinates, not pixels.
# VLM coordinates must be scaled: pixel_coords / RETINA_FACTOR = point_coords.
RETINA_FACTOR = 2.0

# Scale factor for resizing screenshots before sending to VLM.
# 0.4 = 40% of original size — reduces tokens & latency significantly.
SCREENSHOT_SCALE = 0.4


def _get_screen_size() -> tuple[int, int]:
    """Get main display size in logical points (width, height)."""
    try:
        result = subprocess.run(
            ['osascript', '-e',
             'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=5,
        )
        # Returns: "0, 0, 1440, 900" or similar
        parts = result.stdout.strip().split(",")
        if len(parts) >= 4:
            return int(parts[2].strip()), int(parts[3].strip())
    except Exception:
        pass
    # Fallback — common MacBook Air resolution
    return 1440, 900


def take_screenshot_base64() -> str:
    """
    Capture the entire screen and return as base64-encoded PNG string.
    Uses macOS screencapture utility (no third-party deps).

    The screenshot is optionally resized by SCREENSHOT_SCALE to reduce
    payload size for VLM queries.

    Returns:
        Base64-encoded PNG string, or error message.
    """
    import tempfile
    import base64
    import os

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        tmp_path = f.name

    try:
        subprocess.run(
            ['screencapture', '-x', '-C', tmp_path],
            capture_output=True, check=True, timeout=5,
        )
        with open(tmp_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        return f"[-] Screenshot failed: {e}"
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Vision-language model query — uses VLM server (Ollama or MLX)
# ---------------------------------------------------------------------------

_VLM_ENDPOINT_FILE = os.path.join(str(pathlib.Path.home()), ".demascas", "vlm_endpoint.txt")
# Default to Ollama port, override with DEMASCAS_VLM_ENDPOINT env var
_VLM_BASE = os.environ.get("DEMASCAS_VLM_ENDPOINT", "http://127.0.0.1:11434")
_VLM_MODEL = os.environ.get("DEMASCAS_VLM_MODEL", "qwen2.5-vl:3b")


def _get_vlm_url() -> str:
    """Read the VLM endpoint from the discovery file, with fallback probing.
    Priority: 1) cached file  2) probe common paths  3) hardcoded default."""
    # 1) Try the file written by start_vlm_server.py
    try:
        if os.path.exists(_VLM_ENDPOINT_FILE):
            path = open(_VLM_ENDPOINT_FILE).read().strip()
            if path:
                return f"{_VLM_BASE}{path}"
    except Exception:
        pass

    # 2) Probe known endpoint paths
    import requests
    candidates = ["/v1/chat/completions", "/chat", "/generate"]
    for path in candidates:
        try:
            r = requests.post(
                f"{_VLM_BASE}{path}",
                json={"model": _VLM_MODEL, "messages": [], "max_tokens": 1},
                timeout=3,
            )
            # Any non-404 means this endpoint exists (even 400/422 is fine)
            if r.status_code != 404:
                # Cache to file for next time
                try:
                    os.makedirs(os.path.dirname(_VLM_ENDPOINT_FILE), exist_ok=True)
                    with open(_VLM_ENDPOINT_FILE, "w") as f:
                        f.write(path)
                except Exception:
                    pass
                return f"{_VLM_BASE}{path}"
        except Exception:
            continue

    # 3) Fallback — return the most common path
    return f"{_VLM_BASE}/v1/chat/completions"


def vision_query(prompt: str, screenshot_b64: str | None = None) -> str:
    """
    Send a screenshot + text prompt to the MLX vision-language model.
    If no screenshot is provided, one is captured automatically.

    Args:
        prompt: Text prompt describing what to analyze/find.
        screenshot_b64: Optional base64-encoded PNG. If None, captures screen.

    Returns:
        VLM response text, or error message.
    """
    import requests

    if screenshot_b64 is None:
        screenshot_b64 = take_screenshot_base64()
        if screenshot_b64.startswith("[-]"):
            return screenshot_b64  # propagate error

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}"
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }
    ]

    try:
        vlm_url = _get_vlm_url()
        resp = requests.post(
            vlm_url,
            json={
                "model": _VLM_MODEL,
                "messages": messages,
                "max_tokens": 200,
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        return "[-] VLM server not running. Start with: ollama run qwen2.5-vl:3b"
    except Exception as e:
        return f"[-] VLM query failed: {e}"


def vision_execute(action: str) -> str:
    """
    High-level vision tool: takes a screenshot, sends to VLM with the
    requested action, and executes the VLM's response (click, type, scroll).

    The VLM is asked to return structured JSON with pixel coordinates.

    Args:
        action: Natural language description of what to do.
                e.g. "Click the search bar" or "Find the submit button"

    Returns:
        Result of the executed action, or error message.
    """
    import json as json_mod

    # Capture screenshot
    screenshot_b64 = take_screenshot_base64()
    if screenshot_b64.startswith("[-]"):
        return screenshot_b64

    # Ask VLM to locate the element and return coordinates
    vlm_prompt = (
        f"Look at this screenshot and help me: {action}\n\n"
        "Respond with ONLY a JSON object in one of these formats:\n"
        '{"action":"click","x":123,"y":456}\n'
        '{"action":"type","x":123,"y":456,"text":"hello"}\n'
        '{"action":"scroll","direction":"down","amount":3}\n'
        '{"action":"not_found","reason":"element not visible"}\n'
        "Output ONLY the JSON, nothing else."
    )

    vlm_response = vision_query(vlm_prompt, screenshot_b64)
    if vlm_response.startswith("[-]"):
        return vlm_response

    # Parse VLM response
    try:
        # Find JSON in the response
        first_brace = vlm_response.find('{')
        last_brace = vlm_response.rfind('}')
        if first_brace < 0 or last_brace < 0:
            return f"[-] VLM did not return JSON: {vlm_response[:200]}"

        result = json_mod.loads(vlm_response[first_brace:last_brace + 1])
        act = result.get("action", "unknown")

        if act == "not_found":
            return f"[-] Element not found: {result.get('reason', 'unknown')}"

        # ── Retina coordinate correction ───────────────────────
        # VLM returns pixel coordinates within the (possibly scaled) screenshot.
        # We need to convert to macOS logical points:
        #   point = pixel_in_screenshot / SCREENSHOT_SCALE / RETINA_FACTOR
        # Then clamp to screen bounds for safety.
        screen_w, screen_h = _get_screen_size()

        def _correct_coords(px: int, py: int) -> tuple[int, int]:
            """Convert VLM pixel coords → macOS logical points with bounds check."""
            x = int(px / SCREENSHOT_SCALE / RETINA_FACTOR)
            y = int(py / SCREENSHOT_SCALE / RETINA_FACTOR)
            x = max(0, min(x, screen_w - 1))
            y = max(0, min(y, screen_h - 1))
            return x, y

        if act == "click":
            raw_x, raw_y = result["x"], result["y"]
            x, y = _correct_coords(raw_x, raw_y)
            print(f"[VISION] Click: VLM=({raw_x},{raw_y}) → macOS=({x},{y})")
            try:
                import pyautogui
                pyautogui.click(x, y)
                return f"Clicked at ({x}, {y})."
            except ImportError:
                # Fallback to AppleScript
                subprocess.run(
                    ['osascript', '-e',
                     f'tell application "System Events" to click at {{{x}, {y}}}'],
                    capture_output=True, timeout=5,
                )
                return f"Clicked at ({x}, {y})."

        if act == "type":
            raw_x, raw_y = result.get("x", 0), result.get("y", 0)
            x, y = _correct_coords(raw_x, raw_y)
            text = result.get("text", "")
            print(f"[VISION] Type at: VLM=({raw_x},{raw_y}) → macOS=({x},{y})")
            try:
                import pyautogui
                pyautogui.click(x, y)
                pyautogui.typewrite(text, interval=0.02)
                return f"Typed '{text}' at ({x}, {y})."
            except ImportError:
                return "[-] pyautogui not installed for type action."

        if act == "scroll":
            direction = result.get("direction", "down")
            amount = result.get("amount", 3)
            try:
                import pyautogui
                scroll_val = -amount if direction == "down" else amount
                pyautogui.scroll(scroll_val)
                return f"Scrolled {direction} by {amount}."
            except ImportError:
                return "[-] pyautogui not installed for scroll action."

        return f"[-] Unknown VLM action: {act}"

    except (json_mod.JSONDecodeError, KeyError) as e:
        return f"[-] Failed to parse VLM response: {e}. Raw: {vlm_response[:200]}"


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import time

    print("[*] Listing all open windows...")
    print(list_open_windows())
    print()

    print("[*] Tapping into macOS Accessibility API...")
    print("[*] You have 3 seconds to click on another app to scan it...")
    time.sleep(3)

    tree_data = get_active_window_tree()
    print("\n" + "=" * 40)
    print(tree_data)
    print("=" * 40)

    print("\n[*] Compressed tree:")
    print(compress_tree(tree_data))
    print("=" * 40)
