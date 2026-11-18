#!/usr/bin/env python3
"""
DEMASCAS — scripts/real_readonly_check.py
The real read-only pass: the actual tools on this Mac, READ ONLY — nothing
is opened, typed, sent, clicked or deleted.  Calendar / Reminders may show
macOS's one-time permission prompt.  Mail is read only if it's already open.

  ../venv/bin/python scripts/real_readonly_check.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def timed(name, fn):
    t = time.time()
    try:
        r = fn()
        ok = not (isinstance(r, dict) and r.get("success") is False) and not (isinstance(r, str) and r.startswith("[-]"))
        return {"check": name, "ok": ok, "seconds": round(time.time() - t, 2), "result": r}
    except Exception as e:
        return {"check": name, "ok": False, "seconds": round(time.time() - t, 2), "result": f"{type(e).__name__}: {e}"}


def short(r, n=160):
    if isinstance(r, dict):
        r = r.get("message", r)
    return str(r).replace("\n", " | ")[:n]


def main() -> int:
    from actuators.file_system import find_file, read_file_text
    from core import axui, verify
    from core.appnames import installed_apps, running_apps
    from actuators import apple

    out = []
    r = timed("find_file(~, 'resume') via Spotlight", lambda: find_file("~", "resume"))
    out.append(r)
    top = (r["result"].get("matches") or [{}])[0].get("path") if isinstance(r["result"], dict) else None
    out.append(timed(f"read_file_text({os.path.basename(top) if top else '-'})", lambda: read_file_text(top) if top else {"success": False, "message": "no file"}))
    out.append(timed("read_screen_text(Finder)", lambda: axui.read_screen_text("Finder")))
    front = axui.frontmost_app()
    out.append(timed(f"axui.snapshot({front}) — the /ui read", lambda: {"success": True, "message": f"{len(axui.snapshot(front))} controls"}))
    out.append(timed("installed + running apps (/apps)", lambda: {"success": True, "message": f"{len(installed_apps())} installed, running: {', '.join(running_apps()[:8])}"}))
    out.append(timed("verify.after(open_application Finder)", lambda: {"success": True, "message": f"verified={verify.after('open_application', {'app_name': 'Finder'}, {}, 'Opened Finder')}"}))
    out.append(timed("list_agenda(today) — EventKit", lambda: apple.list_agenda("today")))
    out.append(timed("list_reminders — EventKit", lambda: apple.list_reminders(count=5)))
    if axui.pid_of("Mail"):
        out.append(timed("read_mail(3) — Mail is open", lambda: apple.read_mail(count=3, unread_only=False)))
    else:
        out.append({"check": "read_mail", "ok": None, "seconds": 0, "result": "skipped — Mail isn't open (reading it would launch it)"})

    for o in out:
        mark = "✓" if o["ok"] else ("–" if o["ok"] is None else "✗")
        print(f"{mark} {o['check']:45s} {o['seconds']:6.2f}s  {short(o['result'])}")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "exam_runs", f"real_readonly_{time.strftime('%Y%m%d-%H%M')}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump([{**o, "result": short(o["result"], 600)} for o in out], f, indent=1, ensure_ascii=False)
    print("saved:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
