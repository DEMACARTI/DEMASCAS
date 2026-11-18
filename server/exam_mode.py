"""
DEMASCAS — server/exam_mode.py
Exam mode (DEMASCAS_EXAM=1): every tool call is RECORDED and answered
with a plausible fake result — nothing on the real computer is opened,
typed, clicked, sent or deleted.  The confirmation gates, routing, skills,
agents and the model all run for real, so the exam measures them.

A tiny fake "world" (frontmost app, running apps) makes follow-ups like
"close it" or "send it in Discord" behave as they would live.

Endpoints (exam mode only):
  POST /exam/reset   {"frontmost": "Finder", "running": [...], "stubs": {tool: result}}
  GET  /exam/calls   → {"calls": [{"name", "args", "confirmed", "t"}]}  (and clears them)
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time

from flask import jsonify, request

ENABLED = os.environ.get("DEMASCAS_EXAM", "") not in ("", "0")

# Server-level wrappers that are pure logic around lower-level actuators:
# they run for real (with those actuators faked) so their gates are tested.
_REAL = {"send_chat_message", "message_contact", "confirm_and_execute",
         "screen_observe", "screen_find", "form_read",    # read the FAKE screen (world["screen"])
         "save_memory", "retrieve_memories",   # memory + preferences live in the
         "set_preference", "remove_preference", "save_user_preference"}   # exam's scratch HOME

_lock = threading.Lock()
_calls: list[dict] = []
_world: dict = {}
_DEFAULT_WORLD = {"frontmost": "Finder",
                  "running": ["Finder", "Dock", "Safari", "Discord", "Mail"],
                  "stubs": {}}


def _reset_world(w: dict | None = None) -> None:
    global _world
    base = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in _DEFAULT_WORLD.items()}
    base.update(w or {})
    with _lock:
        _world = base
        _calls.clear()


def _record(name: str, args: dict, confirmed: bool = False) -> None:
    with _lock:
        _calls.append({"name": name, "args": dict(args), "confirmed": confirmed, "t": time.time()})


_DEFAULT_SCREEN = {
    "app": "Brave Browser", "source": "dom", "url": "https://example.com/", "title": "Example", "dialog": "",
    "elements": [{"id": 1, "role": "link", "name": "More information"}, {"id": 2, "role": "button", "name": "Next"}],
}


def _screen_world() -> dict:
    """The fake screen, as core.screen.observe would return it."""
    scr = _world.setdefault("screen", json.loads(json.dumps(_DEFAULT_SCREEN)))
    obs = json.loads(json.dumps(scr))
    obs.update(success=True, at=time.time(), ms=1)
    obs.setdefault("source", "dom")
    return obs


def _fake_ui(name: str, a: dict):
    """A screen action applied to the fake screen — the result reads like the real one."""
    from core import screen
    scr = _world.setdefault("screen", json.loads(json.dumps(_DEFAULT_SCREEN)))
    try:
        eid = int(a.get("element_id"))
    except (TypeError, ValueError):
        eid = -1
    el = next((e for e in scr.get("elements", []) if e.get("id") == eid), None)
    if name == "ui_scroll":
        return {"success": True, "message": "Scrolled.", "screen": screen.render(_screen_world())}
    if name == "ui_undo":
        return {"success": True, "message": "Undone."}
    if el is None:
        return {"success": False, "message": f"[-] There's no [{eid}] on the screen I last saw — call screen_observe again."}
    if el.get("role") == "password":
        return {"success": False, "message": "[-] That's a password field — I never type or read passwords."}
    what = ""
    if name == "ui_type":
        el["value"] = str(a.get("text", ""))
        what = f"Typed \"{el['value'][:60]}\" into [{eid}] \"{el['name']}\""
    elif name == "ui_set_checked":
        want = str(a.get("checked", True)).lower() not in ("false", "0", "no")
        if el.get("role") == "radio" and want:
            for o in scr["elements"]:
                if o.get("role") == "radio" and o.get("group") == el.get("group"):
                    o["checked"] = False
        el["checked"] = want
        what = f"{'Ticked' if want else 'Unticked'} [{eid}] \"{el['name']}\""
    elif name == "ui_select":
        el["value"] = str(a.get("option", ""))
        what = f"Chose \"{el['value']}\" in [{eid}] \"{el['name']}\""
    else:
        if el.get("role") in ("checkbox", "radio") and el.get("checked"):
            return {"success": True, "message": f"\"{el['name']}\" is already checked."}
        if el.get("role") in ("checkbox", "radio"):
            el["checked"] = True
        what = f"Clicked [{eid}] \"{el['name']}\""
        nxt = (el.get("then") or {})
        if nxt:
            scr.update(nxt)
    screen._LAST.clear(); screen._LAST.update(_screen_world())
    changed = f" A box popped up: \"{scr['dialog']}\"" if scr.get("dialog") else ""
    hint = "[A DIALOG OPENED — do nothing else; tell the user what it says and ask what to do.]\n" if scr.get("dialog") else ""
    return {"success": True, "message": what + "." + changed, "screen": hint + screen.render(_screen_world(), limit=40)}


def _fake(name: str, a: dict):
    if name in ("ui_click", "ui_type", "ui_set_checked", "ui_select", "ui_scroll", "ui_undo"):
        return _fake_ui(name, a)
    if name == "find_image_by_content":
        what = str(a.get("what", "")).lower()
        if "qr" in what:
            p = "/Users/exam/Pictures/ERC-ticket-QR.png"
            if a.get("open"):
                return {"success": True, "found": True, "opened": p, "message": "Opening ERC-ticket-QR.png — it has a QR code."}
            return {"success": True, "found": True, "count": 1, "first_match_path": p,
                    "matches": [{"name": "ERC-ticket-QR.png", "path": p, "why": "QR code"}],
                    "message": "Found 1 image(s) in ~/Pictures that look like qr code: ERC-ticket-QR.png."}
        return {"success": True, "found": False, "matches": [], "message": f"None of the images looks like {what}."}
    w = _world
    if name in w.get("slow", {}):             # a tool that takes a while
        time.sleep(float(w["slow"][name]))
    if name in w.get("fail", {}):             # a tool that fails (re-plan / honesty tests)
        return {"success": False, "message": "[-] " + str(w["fail"][name])}
    if name in w.get("stubs", {}):
        return w["stubs"][name]
    app = str(a.get("app_name") or a.get("app") or "").strip()
    if name in ("open_application", "activate_application") and app:
        w["frontmost"] = app
        if app not in w["running"]:
            w["running"].append(app)
        return f"Success: Opened '{app}'."
    if name == "quit_application" and app:
        if app in w["running"]:
            w["running"].remove(app)
        if w["frontmost"] == app:
            w["frontmost"] = "Finder"
        return f"Success: Quit '{app}'."
    if name == "list_running_applications":
        return "Running applications: " + ", ".join(w["running"])
    if name in ("get_active_window_tree", "list_open_windows"):
        return (f"App: {w['frontmost']}\nWindow: {w['frontmost']} — main\n"
                "  AXButton 'Back'\n  AXButton 'Search'\n  AXTextField 'Search'\n"
                "  AXStaticText 'Welcome'")
    if name == "find_file":
        q = str(a.get("partial_name") or "document").strip() or "document"
        ext = str(a.get("extension") or "").lstrip(".")
        if "." in q and not ext:
            q, ext = q.rsplit(".", 1)
        ext = ext or "pdf"
        folder = str(a.get("folder") or "~/Documents")
        fname = f"{q}.{ext}"
        path = os.path.join(os.path.expanduser(folder), fname)
        return {"success": True, "found": True, "count": 1, "first_match_path": path,
                "matches": [{"name": fname, "path": path, "size_mb": 0.4}],
                "message": f"Found 1 file(s) matching '{q}' in {folder}."}
    if name == "list_files_in_folder":
        # same shape as actuators.file_system.list_files_in_folder
        folder = str(a.get("folder_path") or "~/Downloads")
        ext = str(a.get("extension") or "").lower().lstrip(".")
        files = [n for n in ("notes.txt", "report.pdf", "photo.jpg", "budget.xlsx") if not ext or n.endswith("." + ext)]
        if not files:
            return f"Files in {folder} (0 total): no {ext.upper()} files."
        return (f"Files in {folder} ({len(files)} total, showing newest 20):\n"
                + "\n".join(f"  {n} (120 KB)" for n in files))
    if name == "get_clipboard":
        return "Meeting moved to 4 pm."
    if name == "get_weather":
        return "Delhi: 24°C, clear skies, light wind."
    if name in ("get_news", "search_web", "web_search", "smart_search", "web_research",
                "research_web", "read_webpage", "web_read_article", "scrape_webpage"):
        return {"success": True,
                "message": "Top result: (exam stub) the requested information, from example.org."}
    if name == "get_system_info":
        return "macOS 26, Apple M1, 8 GB RAM, battery 76%."
    if name == "morning_brief":
        return {"success": True, "message": "Today is Tuesday 17 November.\nAGENDA: 10 AM — Design review; 3 PM — Call with Aman.\nREMINDERS: call mom; submit the DTI report.\nMAIL: From Aman Sharma: the demo moved to Friday.\nWEATHER: Delhi 24°C, clear."}
    if name == "read_screen_text":
        return w.get("screen_text") or {"success": True, "message": f"On screen in {w['frontmost']}:\nWelcome\nToday's top story: local team wins the final."}
    if name == "read_file_text":
        return {"success": True, "message": f"{os.path.basename(str(a.get('file_path', 'file')))}:\nQ3 project report. Revenue grew 12%. The demo moved to Friday. Risks: hiring delays."}
    if name == "list_agenda":
        return {"success": True, "message": "Tomorrow you have 2 things: 10 AM — Design review (Zoom); 3 PM — Call with Aman."}
    if name == "list_reminders":
        return {"success": True, "message": "You have 2 open reminders: call mom; submit the DTI report."}
    if name == "recent_calls":
        return {"success": True, "message": "Your last 3 calls: Vijval Sharma (missed, 18 Nov 11:02 AM); Mom (outgoing, 17 Nov 9:15 PM); Aman Sharma (incoming, 17 Nov 6:40 PM)."}
    if name == "call_contact":
        return {"success": True, "message": f"Calling {a.get('name') or a.get('number')}."}
    if name == "find_contact":
        return {"success": True, "message": f"{a.get('name', 'Aman')} Sharma: phone +91 98765 43210, email aman@example.com."}
    if name == "read_mail":
        return {"success": True, "message": "From Aman Sharma: Project update — the demo moved to Friday.\nFrom Coursera: Your weekly digest."}
    if name == "read_notes":
        return {"success": True, "message": "Shopping: milk, eggs, bread"}
    if name == "read_chat_messages":
        who = a.get("contact", "them")
        return {"success": True, "contact": who, "messages": [f"{who}: are we still on for tonight?", f"{who}: bring the notes"],
                "message": f"Latest messages in the chat with {who}:\n{who}: are we still on for tonight?\n{who}: bring the notes"}
    if name == "deliver_chat_message":
        return {"success": True, "message": f"Sent '{a.get('text', '')}' to {a.get('contact', '')}."}
    return {"success": True, "message": f"Success: {name} done."}


def verified(name: str, args: dict):
    """Exam: actions are verified unless the item's world says otherwise
    ({"unverified": ["find_and_click"]} — e.g. a click that changes nothing)."""
    if name in _world.get("unverified", []):
        return False
    return None


def record_reply(to: str, text: str) -> None:
    """Exam: an iMessage reply is recorded like a call (nothing is sent)."""
    _record("imessage_reply", {"to": to, "text": text})


def _stub(name: str, fn):
    @functools.wraps(fn)
    def stub(**kw):
        _record(name, kw, confirmed=getattr(_tls, "confirmed", False))
        return _fake(name, kw)
    return stub


_tls = threading.local()


def _recorded(name: str, fn):
    """A real (harmless) tool, still recorded so the exam sees it was called."""
    @functools.wraps(fn)
    def rec(**kw):
        _record(name, kw, confirmed=getattr(_tls, "confirmed", False))
        return fn(**kw)
    return rec


_FAKE_CONTACTS = [
    {"name": "Vijval Sharma", "nickname": "", "phones": ["+91 98765 43210"], "emails": []},
    {"name": "Aman Sharma", "nickname": "", "phones": ["+91 98111 22233"], "emails": ["aman@example.com"]},
    {"name": "Hatim Tai", "nickname": "", "phones": ["+91 90000 00001"], "emails": []},
    {"name": "Mom", "nickname": "Mummy", "phones": ["+91 99999 88888"], "emails": []},
]


def install(app, g: dict) -> None:
    """Wrap every native tool, fake the world readers, add /exam endpoints."""
    if not ENABLED:
        return
    _reset_world()
    tool_map = g["TOOL_MAP"]
    for name, fn in list(tool_map.items()):
        tool_map[name] = _stub(name, fn) if name not in _REAL else _recorded(name, fn)

    # Remember whether a call came through /confirm (approved by the user).
    orig_dispatch = g["_dispatch_native"]

    def dispatch(name, args, *, confirmed=False):
        _tls.confirmed = confirmed
        try:
            return orig_dispatch(name, args, confirmed=confirmed)
        finally:
            _tls.confirmed = False
    g["_dispatch_native"] = dispatch

    # World readers used by the gates.
    import core.axui as axui
    axui.frontmost_app = lambda: _world.get("frontmost", "Finder")
    axui.pid_of = lambda app: 4242 if app in _world.get("running", []) else 0
    g["_app_running"] = lambda app: app in _world.get("running", [])
    import core.appnames as appnames
    appnames.running_apps = lambda: list(_world.get("running", []))

    # The chat flow's real UI steps (open, verify, type) are faked; the
    # read-back + confirmation logic around them runs for real.
    import actuators.messaging as messaging

    def prepare(app_name, contact, text):
        _record("prepare_chat_message", {"app": app_name, "contact": contact, "text": text})
        _world["frontmost"] = app_name
        return {"success": True, "app": app_name, "contact": contact, "text": text}

    def discard(app="", contact="", text=""):
        _record("discard_chat_draft", {"app": app, "contact": contact, "text": text})
        return {"success": True, "message": "Okay, I won't send it."}
    messaging.prepare_chat_message = prepare
    messaging.discard_chat_draft = discard

    # The fake screen: observing reads world["screen"] (the gate and the
    # risk rules run for real on it; nothing on this Mac is looked at)
    import core.screen as screen_mod

    def fake_observe(app: str = ""):
        if "screen" not in _world:      # no page in this item: an app with nothing to see
            obs = {"success": True, "app": _world.get("frontmost", "Finder"), "source": "ax", "elements": [], "at": time.time()}
        else:
            obs = _screen_world()
        with screen_mod._LOCK:
            screen_mod._LAST.clear()
            screen_mod._LAST.update(obs)
        return obs
    screen_mod.observe = fake_observe

    # A fake contact book: the call flow's "who exactly?" step runs for real
    # but never reads the user's Contacts.
    import actuators.apple as apple
    apple._all_contacts = lambda: list(_world.get("contacts") or _FAKE_CONTACTS)

    @app.route("/exam/reset", methods=["POST"])
    def exam_reset():
        _reset_world(request.get_json(silent=True) or {})
        with g["_PENDING_LOCK"]:
            g["_PENDING"].clear()
        return jsonify({"ok": True})

    @app.route("/exam/inbox", methods=["POST"])
    def exam_inbox():
        from server import imessage
        imessage.push_inbox(str((request.get_json(silent=True) or {}).get("text", "")), "owner")
        return jsonify({"ok": True})

    @app.route("/exam/calls", methods=["GET"])
    def exam_calls():
        with _lock:
            out = list(_calls)
            _calls.clear()
        return jsonify({"calls": out, "world": {"frontmost": _world.get("frontmost"),
                                                "running": _world.get("running")}})

    print(f"[EXAM] Exam mode: {len(tool_map) - len(_REAL)} tools stubbed — nothing real is touched.")
