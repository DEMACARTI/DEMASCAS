"""
DEMASCAS — actuators/app_script.py
Drive a scriptable app through its own scripting interface (macOS
AppleScript) — far more reliable than clicking menus.

Guard rails (a small model writes these scripts):
  * the script only ever runs inside `tell application "<app>" … end tell`
  * no shell commands, no System Events (keystrokes/clicks), no other apps,
    no file-system reach-arounds
  * destructive verbs (delete, close, quit, save over, …) need a spoken yes —
    see is_destructive(), used by the tool server's confirmation gate
"""

from __future__ import annotations

import re
import subprocess
import sys

_FORBIDDEN = re.compile(
    r"do\s+shell\s+script|system\s+events|tell\s+application|run\s+script|"
    r"load\s+script|store\s+script|osascript|path\s+to\s+startup|"
    r"\bFinder\b|\bTerminal\b|keystroke|key\s+code|password",
    re.IGNORECASE,
)
_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|erase|empty|quit|close|clear|overwrite|replace|save)\b",
    re.IGNORECASE,
)


def is_destructive(script: str) -> bool:
    return bool(_DESTRUCTIVE.search(script or ""))


def run_app_script(app_name: str, script: str, timeout: int = 20) -> dict:
    """Run AppleScript statements inside `tell application "<app_name>"`.
    Example: app_name="Microsoft PowerPoint",
             script='set t to name of active presentation\\nreturn t'"""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] App scripting is only available on macOS so far."}
    app_name = (app_name or "").strip().strip('"')
    if not app_name or '"' in app_name or len(app_name) > 60:
        return {"success": False, "message": "[-] Which app?"}
    body = (script or "").strip()
    if not body:
        return {"success": False, "message": "[-] Empty script."}
    if _FORBIDDEN.search(body):
        return {"success": False,
                "message": "[-] That script reaches outside the app; only commands for "
                           f"{app_name} itself are allowed."}
    full = f'tell application "{app_name}"\n{body}\nend tell'
    try:
        p = subprocess.run(["osascript", "-e", full], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "message": f"[-] {app_name} didn't respond in {timeout} s."}
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return {"success": False, "message": f"[-] {app_name} said: {(err[-1] if err else 'error')[:200]}"}
    return {"success": True, "message": (p.stdout or "").strip()[:1500] or "Done."}


# ── Menu bar: works for almost every Mac app, scriptable or not ───────

_RISKY_MENU = re.compile(
    r"\b(quit|log ?out|sign ?out|delete|remove|leave|close|erase|clear|reset|uninstall|"
    r"block|report|kick|ban|unfriend|discard|empty|revert|restart|shut ?down)\b",
    re.IGNORECASE,
)


def is_risky_menu(menu_path: str) -> bool:
    return bool(_RISKY_MENU.search(menu_path or ""))


def _as_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def click_menu_item(app_name: str, menu_path: str, timeout: int = 10) -> dict:
    """Click a menu-bar command, e.g. app_name="Discord", menu_path="View > Zoom In"
    (submenus: "File > Export > PDF").  The app is brought to the front first."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Menu control is only available on macOS so far."}
    parts = [p.strip() for p in re.split(r"\s*(?:>|→|/)\s*", menu_path or "") if p.strip()]
    if len(parts) < 2 or len(parts) > 3 or not app_name:
        return {"success": False, "message": "[-] Give the menu path like 'View > Zoom In'."}
    top = parts[0]
    if len(parts) == 2:
        target = f"menu item {_as_str(parts[1])} of menu {_as_str(top)} of menu bar item {_as_str(top)} of menu bar 1"
    else:
        target = (f"menu item {_as_str(parts[2])} of menu {_as_str(parts[1])} of menu item {_as_str(parts[1])} "
                  f"of menu {_as_str(top)} of menu bar item {_as_str(top)} of menu bar 1")
    script = (f'tell application {_as_str(app_name)} to activate\n'
              f'delay 0.3\n'
              f'tell application "System Events" to tell process {_as_str(app_name)}\n'
              f'  click {target}\n'
              f'end tell')
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "message": f"[-] {app_name} didn't respond."}
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return {"success": False, "message": f"[-] Couldn't click {menu_path}: {(err[-1] if err else '')[:160]}"}
    return {"success": True, "message": f"Clicked {menu_path} in {app_name}."}
