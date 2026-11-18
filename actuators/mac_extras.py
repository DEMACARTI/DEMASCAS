"""
DEMASCAS — actuators/mac_extras.py
Small things people ask for that used to cause friction (live log 18 Nov):
open the path you copied, open the exact Privacy & Security pane.
"""

from __future__ import annotations

import os
import subprocess

_PANES = {
    "screen recording": "Privacy_ScreenCapture", "screen": "Privacy_ScreenCapture",
    "accessibility": "Privacy_Accessibility", "microphone": "Privacy_Microphone", "mic": "Privacy_Microphone",
    "contacts": "Privacy_Contacts", "calendar": "Privacy_Calendars", "calendars": "Privacy_Calendars",
    "reminders": "Privacy_Reminders", "full disk access": "Privacy_AllFiles", "automation": "Privacy_Automation",
    "camera": "Privacy_Camera", "privacy": "", "permissions": "", "security": "",
}


def open_settings_pane(pane: str = "privacy") -> dict:
    """Open System Settings at a Privacy & Security pane ('screen recording',
    'accessibility', 'microphone', 'full disk access', …)."""
    key = (pane or "privacy").lower().strip()
    anchor = next((v for k, v in _PANES.items() if k in key), "")
    url = "x-apple.systempreferences:com.apple.preference.security" + (f"?{anchor}" if anchor else "")
    r = subprocess.run(["open", url], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"success": False, "message": "[-] I couldn't open System Settings."}
    label = next((k for k, v in _PANES.items() if v == anchor and anchor), "Privacy & Security")
    return {"success": True, "message": f"Opened {label.title()} settings. Turn DEMASCAS on there."}


def _kind(path: str) -> str:
    if path.endswith(".app"):
        return "an app"
    if os.path.isdir(path):
        n = len([x for x in os.listdir(path) if not x.startswith(".")])
        return f"a folder with {n} item{'s' if n != 1 else ''}"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    size = os.path.getsize(path)
    nice = f"{size / 1e6:.1f} MB" if size >= 1e6 else f"{max(1, size // 1000)} KB"
    return f"a {ext.upper() or 'plain'} file ({nice})"


def open_copied_path() -> dict:
    """Open the file / folder / app whose path is on the clipboard and say what it is."""
    try:
        text = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout.strip().strip("'\"")
    except Exception:
        return {"success": False, "message": "[-] I couldn't read the clipboard."}
    if not text:
        return {"success": False, "message": "Your clipboard is empty."}
    path = os.path.expanduser(text.splitlines()[0].strip())
    if path.startswith("file://"):
        from urllib.parse import unquote
        path = unquote(path[7:])
    if not os.path.exists(path):
        return {"success": False, "message": f"What you copied isn't a path on this Mac: “{text[:80]}”."}
    what = _kind(path)
    r = subprocess.run(["open", path], capture_output=True, text=True, timeout=10)
    name = os.path.basename(path.rstrip("/")) or path
    if r.returncode != 0:
        return {"success": False, "message": f"It's {what} — {name} — but I couldn't open it."}
    return {"success": True, "path": path, "message": f"Opening {name} — it's {what}."}
