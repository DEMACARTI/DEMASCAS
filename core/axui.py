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


def focused_value(app: str = "") -> str | None:
    """Text in the focused field of the app (None = can't read it)."""
    if not available():
        return None
    app = app or frontmost_app()
    pid = pid_of(app) if app else 0
    if not pid:
        return None
    import ApplicationServices as AS
    el = _attr(AS.AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    if el is None:
        return None
    v = _attr(el, "AXValue")
    return str(v) if v is not None else None


def texts(app: str, max_nodes: int = 12000, max_depth: int = 45) -> list[str]:
    """Every visible text of the app's front window, in reading order
    (static text values + labelled groups) — what a person would read."""
    if not available():
        return []
    enable_electron(app)
    pid = pid_of(app)
    if not pid:
        return []
    import ApplicationServices as AS
    root = AS.AXUIElementCreateApplication(pid)
    wins = _attr(root, "AXWindows") or []
    start = wins[0] if wins else root
    out, stack, seen = [], [(start, 0)], 0
    while stack and seen < max_nodes:
        e, d = stack.pop()
        seen += 1
        role = _attr(e, "AXRole")
        if role == "AXStaticText":
            v = _attr(e, "AXValue") or _attr(e, "AXDescription") or ""
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        if d < max_depth:
            kids = _attr(e, "AXChildren") or []
            for c in reversed(kids):             # keep document order on the stack
                stack.append((c, d + 1))
    return out


_TERMINALS = ("Terminal", "iTerm2", "iTerm", "Warp", "Ghostty", "Alacritty", "kitty", "WezTerm")
_NOT_WINDOWS = ("Window Server", "Dock", "Control Center", "Control Centre", "Notification Center", "Notification Centre", "SystemUIServer",
                "Spotlight", "loginwindow", "WindowManager", "Wallpaper")


_CONSOLE: list = []


def _console_terminal() -> str:
    """The terminal app DEMASCAS was started from: walk up this process's
    parents (tool server → run.sh → shell → Terminal).  Cached."""
    if _CONSOLE:
        return _CONSOLE[0]
    found, pid = "", os.getpid()
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            break
        if not out:
            break
        ppid, _, comm = out.partition(" ")
        for t in _TERMINALS:
            if t.lower().replace(" ", "") in comm.lower().replace(" ", ""):
                found = t
        try:
            pid = int(ppid)
        except ValueError:
            break
        if pid <= 1 or found:
            break
    _CONSOLE.append(found)
    return found


def _is_demascas_console(app: str) -> bool:
    """The terminal in front is the one DEMASCAS itself is running in.
    (Terminal doesn't expose its text as controls, so the old check — looking
    for "demascas" among the window's buttons — never matched: live 18 Nov
    "what's on the screen?" read Terminal and found nothing.)"""
    if app and app == _console_terminal():
        return True
    try:
        names = " ".join(it["name"] for it in snapshot(app)[:60]).lower()
    except Exception:
        return False
    return any(k in names for k in ("demascas", "run.sh", "demascasd"))


def app_on_screen() -> str:
    """The app whose window the user is looking at.  Usually the frontmost
    one — but when that's the terminal running DEMASCAS (live log: "what's on
    the screen?" read DEMASCAS's own log), the window right behind it."""
    front = frontmost_app()
    if front not in _TERMINALS or not _is_demascas_console(front):
        return front
    try:
        import Quartz
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:   # front → back
            owner = str(w.get("kCGWindowOwnerName", ""))
            bounds = w.get("kCGWindowBounds") or {}
            if (w.get("kCGWindowLayer", 1) == 0 and owner and owner != front and owner not in _NOT_WINDOWS
                    and float(bounds.get("Width", 0)) > 200 and float(bounds.get("Height", 0)) > 150):
                return owner
    except Exception as e:
        print(f"[axui] window order unavailable: {e}")
    return front


def read_screen_text(app_name: str = "", max_chars: int = 4000) -> dict:
    """Everything readable in the front window of an app (web pages in
    Safari, documents, chats…) — what a person would read on screen."""
    app = app_name or app_on_screen()
    if not app:
        return {"success": False, "message": "[-] I can't tell which app is in front."}
    lines, seen, total = [], set(), 0
    for t in texts(app):
        t = " ".join(t.split())
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        lines.append(t)
        total += len(t) + 1
        if total >= max(500, min(int(max_chars or 4000), 12000)):
            break
    if not lines:
        # apps like Finder show names, not text: list what's there instead
        # (window content only — the menu bar's items aren't "what's on screen")
        lines = list(dict.fromkeys(it["name"] for it in snapshot(app)
                                   if len(it["name"]) > 1 and it["role"] not in ("AXMenuItem", "AXMenuBarItem")))[:80]
    if not lines:
        # nothing in the accessibility tree (canvas apps, images, games): read the pixels
        try:
            from core.screen import ocr
            if ocr.available():
                path = ocr.screenshot_window(_window_box(app))
                if path:
                    try:
                        lines = [t["text"] for t in ocr.read_text(path)][:120]
                    finally:
                        os.unlink(path)
        except Exception as e:
            print(f"[axui] OCR fallback failed: {e}")
    if not lines:
        return {"success": False, "message": f"[-] I couldn't read any text in {app}."}
    return {"success": True, "app": app, "message": f"On screen in {app}:\n" + "\n".join(lines)}


def _window_box(app: str) -> list | None:
    """The app's front window on screen, in points [x, y, w, h]."""
    try:
        import Quartz
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            b = w.get("kCGWindowBounds") or {}
            if str(w.get("kCGWindowOwnerName", "")) == app and w.get("kCGWindowLayer", 1) == 0 and float(b.get("Width", 0)) > 200:
                return [int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])]
    except Exception:
        pass
    return None


def tree_text(app: str, limit: int = 120) -> str:
    """Compact 'ROLE : name' listing (for agents' screen reads)."""
    rows = [f"- [{it['role'][2:].lower()}] : '{it['name'][:80]}'" for it in snapshot(app)[:limit]]
    return "\n".join(rows)
