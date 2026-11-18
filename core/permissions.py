"""
DEMASCAS — core/permissions.py
Which macOS permissions DEMASCAS has, without triggering any permission
prompt.  macOS grants them to the app that STARTED DEMASCAS (usually
Terminal), so run this from that same app.

  python main.py doctor --permissions
  "what can't you access?"  (tool: check_permissions)
"""

from __future__ import annotations

import os
import sqlite3
import sys

_PANE = "System Settings → Privacy & Security → "


def _accessibility() -> bool | None:
    try:
        import ApplicationServices as AS
        return bool(AS.AXIsProcessTrusted())
    except Exception:
        return None


def _screen_recording() -> bool | None:
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def _microphone() -> bool | None:
    try:
        import AVFoundation as AV
        st = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
        return None if st == 0 else st == 3
    except Exception:
        return None


def _contacts() -> bool | None:
    try:
        import Contacts
        st = Contacts.CNContactStore.authorizationStatusForEntityType_(0)
        return None if st == 0 else st in (3, 4)          # 4 = limited
    except Exception:
        return None


def _eventkit(entity: int) -> bool | None:
    try:
        import EventKit
        st = EventKit.EKEventStore.authorizationStatusForEntityType_(entity)
        return None if st == 0 else st in (3, 4)          # 3 authorized / full, 4 write-only
    except Exception:
        return None


def _full_disk() -> bool | None:
    p = os.path.expanduser("~/Library/Application Support/CallHistoryDB/CallHistory.storedata")
    if not os.path.exists(p):
        return None
    try:
        sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1).execute("PRAGMA schema_version").fetchone()
        return True
    except sqlite3.Error:
        return False


# (name, check, what it's for, where to allow it)
_CHECKS = [
    ("Accessibility", _accessibility, "clicking, typing and reading app windows", _PANE + "Accessibility"),
    ("Screen Recording", _screen_recording, "reading text on screen", _PANE + "Screen Recording"),
    ("Microphone", _microphone, "hearing you", _PANE + "Microphone"),
    ("Contacts", _contacts, "finding and calling people", _PANE + "Contacts"),
    ("Calendars", lambda: _eventkit(0), "your agenda and new events", _PANE + "Calendars"),
    ("Reminders", lambda: _eventkit(1), "reminders", _PANE + "Reminders"),
    ("Full Disk Access", _full_disk, "your call history", _PANE + "Full Disk Access"),
]


def status() -> list[dict]:
    """[{name, ok: True/False/None (not asked yet), why, fix}] — prompts nothing."""
    if sys.platform != "darwin":
        return []
    out = []
    for name, fn, why, fix in _CHECKS:
        try:
            ok = fn()
        except Exception:
            ok = None
        out.append({"name": name, "ok": ok, "why": why, "fix": fix})
    return out


def check_permissions() -> dict:
    """Spoken: what DEMASCAS can't reach, and how to allow it."""
    st = status()
    missing = [s for s in st if s["ok"] is False]
    unasked = [s["name"] for s in st if s["ok"] is None]
    if not missing:
        msg = "I have every permission I need."
        if unasked:
            msg += f" macOS will ask about {', '.join(unasked)} the first time I need it."
        return {"success": True, "permissions": st, "message": msg}
    parts = [f"{s['name']}, for {s['why']}" for s in missing]
    return {"success": True, "permissions": st,
            "message": "I can't use: " + "; ".join(parts) + ". Turn them on in System Settings, Privacy and Security, "
                       "for the app you start me from, usually Terminal."}


def report() -> str:
    lines = []
    for s in status():
        mark = {True: "✓", False: "✗", None: "·"}[s["ok"]]
        tail = "" if s["ok"] else (f" — not asked yet (macOS asks on first use)" if s["ok"] is None else f" — allow it: {s['fix']}")
        lines.append(f"  {mark} {s['name']:<17} {s['why']}{tail}")
    return "\n".join(lines)
