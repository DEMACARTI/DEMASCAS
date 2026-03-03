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

        if exists (window 1 of frontProcess) then
            set theWindow to window 1 of frontProcess
            set elementList to ""

            -- SAFE SCAN: Level 1 (Window)
            set level1 to UI elements of theWindow
            repeat with L1 in level1
                try
                    set eName to name of L1
                    if eName is not missing value and eName is not "" then
                        set elementList to elementList & "- [" & (class of L1) & "] : '" & eName & "'\\n"
                    end if

                    -- SAFE SCAN: Level 2 (Groups/Toolbars)
                    set level2 to UI elements of L1
                    repeat with L2 in level2
                        try
                            set eName2 to name of L2
                            if eName2 is not missing value and eName2 is not "" then
                                set elementList to elementList & "- [" & (class of L2) & "] : '" & eName2 & "'\\n"
                            end if

                            -- SAFE SCAN: Level 3 (Buttons inside Groups)
                            set level3 to UI elements of L2
                            repeat with L3 in level3
                                try
                                    set eName3 to name of L3
                                    if eName3 is not missing value and eName3 is not "" then
                                        set elementList to elementList & "- [" & (class of L3) & "] : '" & eName3 & "'\\n"
                                    end if
                                end try
                            end repeat
                        end try
                    end repeat
                end try
            end repeat

            return "ACTIVE APP: " & appName & "\\nUI TREE MAP:\\n" & elementList
        else
            return "ACTIVE APP: " & appName & "\\nSTATUS: No active windows."
        end if
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
