"""
DEMASCAS — core/axui.py
Fast, in-process access to an app's accessibility tree (macOS).

* Electron apps (Discord, Slack, VS Code, Notion, …) hide their UI from the
  accessibility API until a client sets AXManualAccessibility — before
  this, DEMASCAS saw 0 elements in Discord.  enable_electron() does that.
* Walking the tree in-process (pyobjc) instead of one AppleScript Apple
  Event per element: Discord, 1,362 nodes — 21 s → 0.64 s cold / 0.09 s warm.

Read helpers never click; press() is the only action, used by tools.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
import time

_ENABLED: dict[int, float] = {}          # pid → when we enabled accessibility


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import ApplicationServices  # noqa: F401
        return True
    except Exception:
        return False


def pid_of(app: str) -> int:
    try:
        out = subprocess.run(["pgrep", "-x", app], capture_output=True, text=True, timeout=2).stdout
        return int(out.split()[0]) if out.strip() else 0
    except Exception:
        return 0


def frontmost_app() -> str:
    try:
        out = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of '
                              'first application process whose frontmost is true'],
                             capture_output=True, text=True, timeout=3).stdout
        return out.strip()
    except Exception:
        return ""


def is_electron(app: str) -> bool:
    for base in ("/Applications", os.path.expanduser("~/Applications")):
        if os.path.isdir(os.path.join(base, f"{app}.app", "Contents", "Frameworks",
                                      "Electron Framework.framework")):
            return True
    return False


def _attr(e, name):
    import ApplicationServices as AS
    err, v = AS.AXUIElementCopyAttributeValue(e, name, None)
    return v if err == 0 else None


def enable_electron(app: str) -> bool:
    """Expose an Electron app's UI to accessibility (once per launch)."""
    if not available() or not is_electron(app):
        return False
    pid = pid_of(app)
    if not pid:
        return False
    if pid in _ENABLED:
        return True
    import ApplicationServices as AS
    from Foundation import NSNumber
    el = AS.AXUIElementCreateApplication(pid)
    err = AS.AXUIElementSetAttributeValue(el, "AXManualAccessibility", NSNumber.numberWithBool_(True))
    if err == 0:
        _ENABLED[pid] = time.time()
        time.sleep(0.4)                    # the app builds its tree on demand
        return True
    return False


INTERACTIVE = ("AXButton", "AXLink", "AXTab", "AXRadioButton", "AXCheckBox", "AXMenuItem",
               "AXTextField", "AXTextArea", "AXPopUpButton", "AXCell", "AXRow")


def snapshot(app: str, roles: tuple = INTERACTIVE, max_nodes: int = 8000,
             max_depth: int = 40) -> list[dict]:
    """[{role, name, el}] for named controls of the app's windows."""
    if not available():
        return []
    enable_electron(app)
    pid = pid_of(app)
    if not pid:
        return []
    import ApplicationServices as AS
    root = AS.AXUIElementCreateApplication(pid)
    out, stack, seen = [], [(root, 0)], 0
    while stack and seen < max_nodes:
        e, d = stack.pop()
        seen += 1
        role = _attr(e, "AXRole")
        if role in roles:
            name = _attr(e, "AXDescription") or _attr(e, "AXTitle") or _attr(e, "AXPlaceholderValue") or ""
            if name:
                out.append({"role": str(role), "name": str(name), "el": e})
        if d < max_depth:
            for c in (_attr(e, "AXChildren") or []):
                stack.append((c, d + 1))
    out.reverse()                          # document order (stack popped last-first)
    return out


def _clean(name: str) -> str:
    n = re.sub(r"\((?:direct|group) message\).*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


def find(app: str, query: str, roles: tuple = INTERACTIVE, cutoff: float = 0.6,
         items: list[dict] | None = None) -> dict | None:
    """Best control whose name matches `query` (exact > word > fuzzy).
    'Hatem Thai' (as heard) finds 'Hatim Tai (direct message)'."""
    items = items if items is not None else snapshot(app, roles)
    q = _clean(query)
    if not q or not items:
        return None
    for it in items:
        if _clean(it["name"]) == q:
            return it
    for it in items:
        if re.search(rf"\b{re.escape(q)}\b", _clean(it["name"])):
            return it
    names = [_clean(it["name"]) for it in items]
    best = difflib.get_close_matches(q, names, n=1, cutoff=cutoff)
    if best:
        return items[names.index(best[0])]
    return None


def press(item: dict) -> bool:
    import ApplicationServices as AS
    return AS.AXUIElementPerformAction(item["el"], "AXPress") == 0


def focus(item: dict) -> bool:
    import ApplicationServices as AS
    return AS.AXUIElementSetAttributeValue(item["el"], "AXFocused", True) == 0


def message_box(app: str, items: list[dict] | None = None) -> dict | None:
    """The chat input ("Message @Hatim Tai") — its label names the open chat."""
    items = items if items is not None else snapshot(app, ("AXTextArea", "AXTextField"))
    for it in items:
        if it["role"] in ("AXTextArea", "AXTextField") and re.match(r"(?i)message\b", it["name"]):
            return it
    return None


def tree_text(app: str, limit: int = 120) -> str:
    """Compact 'ROLE : name' listing (for agents' screen reads)."""
    rows = [f"- [{it['role'][2:].lower()}] : '{it['name'][:80]}'" for it in snapshot(app)[:limit]]
    return "\n".join(rows)
