"""
DEMASCAS — core/verify.py
Did the action actually happen?  Every action tool used to report
"Success" when the command RAN, not when it WORKED.  These checks look at
the computer afterwards (read-only) and say verified True / False / None
(None = no check for this tool, or it can't be checked here).

  open_application   the app's process is running (and usually in front)
  activate_…         the app is in front
  open_url           the browser shows that site
  click / menu       the app's screen changed (before/after snapshot)
  type_text          the focused field now contains the text
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from urllib.parse import urlparse

CLICKS = {"find_and_click", "click_ui_element", "right_click_element", "click_menu_item", "browser_click_text", "web_smart_click"}
TYPES = {"type_text", "type_text_into_element"}


def screen_hash(app: str = "") -> str | None:
    """A fingerprint of what the app shows (control names + roles)."""
    try:
        from core import axui
        app = app or axui.frontmost_app()
        items = axui.snapshot(app, max_nodes=4000) if app else []
        if not items:
            return None
        blob = "|".join(f"{i['role']}:{i['name']}" for i in items)
        return hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()
    except Exception:
        return None


def before(name: str, args: dict) -> dict:
    """State captured BEFORE an action that has an after-check."""
    if name in CLICKS:
        return {"hash": screen_hash(str(args.get("app_name") or ""))}
    return {}


def _browser_url(browser: str = "") -> str:
    scripts = {
        "Safari": 'tell application "Safari" to get URL of front document',
        "Google Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
        "Brave Browser": 'tell application "Brave Browser" to get URL of active tab of front window',
        "Arc": 'tell application "Arc" to get URL of active tab of front window',
        "Microsoft Edge": 'tell application "Microsoft Edge" to get URL of active tab of front window',
    }
    names = [browser] if browser in scripts else list(scripts)
    for b in names:
        try:
            from core import axui
            if not axui.pid_of(b):
                continue
            out = subprocess.run(["osascript", "-e", scripts[b]], capture_output=True, text=True, timeout=3).stdout.strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def _same_site(a: str, b: str) -> bool:
    ha = (urlparse(a if "://" in a else "https://" + a).hostname or "").removeprefix("www.")
    hb = (urlparse(b if "://" in b else "https://" + b).hostname or "").removeprefix("www.")
    return bool(ha) and ha == hb


def after(name: str, args: dict, state: dict, result) -> bool | None:
    """True = it really happened, False = it didn't, None = can't tell."""
    if sys.platform != "darwin":
        return None
    try:
        from core import axui
        if name in ("open_application", "activate_application"):
            app = str(args.get("app_name") or "")
            # the tool resolves names ("chrome" → "Google Chrome"); use the one it reports
            msg = str(result.get("message", "") if isinstance(result, dict) else result)
            if "'" in msg:
                app = msg.split("'")[1] or app
            for _ in range(10):
                if axui.pid_of(app):
                    return True
                time.sleep(0.3)
            return False
        if name in ("open_url", "open_url_and_wait", "browser_navigate", "browser_navigate_and_wait"):
            want = str(args.get("url") or "")
            for _ in range(6):
                if _same_site(_browser_url(str(args.get("browser") or "")), want):
                    return True
                time.sleep(0.5)
            return False
        if name in CLICKS:
            old = state.get("hash")
            if old is None:
                return None
            time.sleep(0.5)
            new = screen_hash(str(args.get("app_name") or ""))
            return None if new is None else new != old
        if name in TYPES:
            text = str(args.get("text") or "").strip()
            if not text or args.get("press_enter"):
                return None          # Enter clears the field — can't compare
            value = axui.focused_value(str(args.get("app_name") or ""))
            return None if value is None else text[:40] in value
    except Exception:
        return None
    return None
