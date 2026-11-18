"""
DEMASCAS — actuators/messaging.py
Send a chat message (Discord, Slack, WhatsApp, Messages, …) the safe way:

  1. open the app and the conversation — click the DM by name when it's
     visible ("Hatem Thai" as heard → "Hatim Tai"), else the app's quick
     switcher learned from its docs (Discord/Slack: Cmd+K)
  2. VERIFY the message box belongs to that person
  3. type the EXACT text (no rewording) — Enter is NOT pressed
  4. the tool server asks "Send 'hi' to Hatim Tai on Discord?"; only a yes
     runs deliver_chat_message (Enter); a no runs discard_chat_draft

The live session that motivated this typed "Hi Hatem Thai, how are you
doing?" into whatever chat happened to be open.
"""

from __future__ import annotations

import difflib
import re
import time

CHAT_APPS = ("Discord", "Slack", "WhatsApp", "Messages", "Telegram", "Signal",
             "Microsoft Teams", "Messenger", "Skype")
_QUICK_SWITCHER = {"Discord": "cmd+k", "Slack": "cmd+k", "Microsoft Teams": "cmd+e"}
_ASR_FIXES = {"high": "hi", "hii": "hi", "hay": "hey", "hallo": "hello"}


def is_chat_app(app: str) -> bool:
    return (app or "").strip().lower() in {a.lower() for a in CHAT_APPS}


# ── understanding the request ────────────────────────────────────────

_PATTERNS = [
    # "send a message to Hatem Thai on Discord saying hi"
    r"(?:send|text|message|dm|ping|tell)\s+(?:a\s+)?(?:message\s+|text\s+|dm\s+)?(?:to\s+)?(?P<contact>.+?)\s+on\s+(?P<app>[A-Za-z][\w ]{1,20}?)\s+(?:saying|that says|that|with|to say|:)\s+(?P<text>.+)$",
    # "send hi to Hatem on Discord" / "say hello to Hatem on Discord"
    r"(?:send|say)\s+(?P<text>[\w' !?.,]+?)\s+to\s+(?P<contact>.+?)\s+on\s+(?P<app>[A-Za-z][\w ]{1,20}?)\s*[.!?]*$",
    # "send a high message to Hatem Thai on Discord(, which is open …)"
    r"send\s+(?:a|an)\s+(?P<text>[\w']+)\s+message\s+to\s+(?P<contact>.+?)\s+on\s+(?P<app>[A-Za-z][\w ]{1,20}?)(?:[,.].*)?$",
    # "message Hatem on Discord" — text asked for later
    r"(?:send\s+(?:a\s+)?message\s+to|message|text|dm)\s+(?P<contact>.+?)\s+on\s+(?P<app>[A-Za-z][\w ]{1,20}?)\s*[.!?]*$",
    # "message Hatem saying hi" (app from context)
    r"(?:send\s+(?:a\s+)?message\s+to|message|text|dm|tell)\s+(?P<contact>.+?)\s+(?:saying|that)\s+(?P<text>.+)$",
]
_FRIEND_INTRO = re.compile(r"^(?:my\s+)?(?:friend|buddy|brother|sister|colleague)\s+(?:who\s+is\s+\w+\s+)?"
                           r"(?:called|named)\s+", re.I)


def parse_request(command: str) -> dict:
    """{'app', 'contact', 'text'} — any may be '' if not said."""
    cmd = re.sub(r"^(?:(?:hey|ok|okay|alright|so|please)[, ]+)*(?:(?:i want you to|i need you to|"
                 r"can you|could you|please)\s+)*", "", (command or "").strip(), flags=re.I)
    out = {"app": "", "contact": "", "text": ""}
    for pat in _PATTERNS:
        m = re.search(pat, cmd, re.I)
        if m:
            g = m.groupdict()
            out.update({k: (g.get(k) or "").strip(" .,!?\"'") for k in ("app", "contact")})
            # the message itself keeps its own punctuation ("…tonight?")
            out["text"] = (g.get("text") or "").strip(" \"'").rstrip(".")
            break
    # "...my friend who is online called Haatem Thay and the message ... is Hai"
    if not out["text"]:
        m = re.search(r"\bthe message\s+(?:you should send\s+)?(?:to (?:him|her|them)\s+)?(?:is|should be|will be)\s+(.+)$", cmd, re.I)
        if m:
            out["text"] = m.group(1).strip(" .\"'")
    if not out["contact"]:
        m = re.search(r"\b(?:called|named)\s+([A-Za-z][\w']*(?:\s+[A-Z][\w']*)?)", cmd)
        if m:
            out["contact"] = m.group(1)
    out["contact"] = _FRIEND_INTRO.sub("", re.sub(r"\s+and\s+the\s+message\b.*$", "", out["contact"], flags=re.I)).strip()
    if out["app"]:
        try:
            from core.appnames import resolve
            hit, conf = resolve(out["app"])
            out["app"] = hit if hit and conf >= 0.72 else out["app"]
        except Exception as _e:  # was silent
            __import__("logging").getLogger(__name__).warning("ignored error: %r", _e)
    t = out["text"]
    if t.lower() in _ASR_FIXES:
        out["text"] = _ASR_FIXES[t.lower()]
    return out


# ── doing it ─────────────────────────────────────────────────────────

def _same_person(a: str, b: str) -> bool:
    """Is chat `b` (as shown by the app) the conversation with `a`?  A group
    ("Glitchy, Krish Mittal") is never the same as one person ("Krish")."""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    if ("," in b) != ("," in a) or re.search(r"\(group message\)|\d+ members", b):
        return False
    if a in b or b in a:
        return True
    ta, tb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    if any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.75 for x in ta for y in tb if len(x) > 2):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def _switcher_keys(app: str) -> str:
    try:
        from core.appknowledge.learner import load_manual
        for e in load_manual(app)["entries"]:
            if e.get("keys") and re.search(r"quick switch|jump to|find (?:or start )?a? ?conversation|"
                                            r"search (?:for )?(?:chats?|conversations?|dms?)", e["task"], re.I):
                return e["keys"]
    except Exception as _e:  # was silent
        __import__("logging").getLogger(__name__).warning("ignored error: %r", _e)
    return _QUICK_SWITCHER.get(app, "")


def open_chat(app: str, contact: str) -> dict:
    """Open the conversation with `contact` and VERIFY it's really theirs.
    {"success", "contact" (as the app shows it), "message"} — nothing is typed."""
    from core import axui
    from actuators.system_control import open_application, press_keyboard_shortcut, type_text
    open_application(app)
    time.sleep(0.8)
    if not axui.available():
        return {"success": False, "message": "[-] I can't read the app's screen on this Mac yet."}

    items = axui.snapshot(app)
    # 1. click the conversation directly when it's on screen
    convo = axui.find(app, contact, roles=("AXLink", "AXButton", "AXRow", "AXCell"), items=items, cutoff=0.6)
    opened = False
    if convo and _same_person(contact, axui._clean(convo["name"])):
        opened = axui.press(convo)
        time.sleep(0.9)
    # 2. else the app's quick switcher (learned from its docs)
    if not opened:
        keys = _switcher_keys(app)
        if not keys:
            return {"success": False, "message": f"[-] I couldn't find {contact} in {app}."}
        press_keyboard_shortcut(keys)
        time.sleep(0.5)
        type_text(contact)
        time.sleep(1.2)
        press_keyboard_shortcut("return")
        time.sleep(1.0)

    # 3. verify the chat really is with that person
    box = axui.message_box(app)
    shown = re.sub(r"(?i)^message\s+@?", "", box["name"]).strip() if box else ""
    if not box or not _same_person(contact, shown):
        return {"success": False,
                "message": f"[-] I couldn't open a chat with {contact} in {app}"
                           + (f" (the open chat is {shown})" if shown else "") + "."}
    return {"success": True, "contact": shown or contact, "box": box, "message": f"Opened the chat with {shown or contact}."}


def prepare_chat_message(app: str, contact: str, text: str) -> dict:
    """Open the conversation, verify it, type the text — do NOT send."""
    from core import axui
    from actuators.system_control import type_text
    if not (app and contact and text):
        missing = [n for n, v in (("app", app), ("who to message", contact), ("what to say", text)) if not v]
        return {"success": False, "message": f"[-] I need {' and '.join(missing)}."}
    opened = open_chat(app, contact)
    if not opened["success"]:
        return {"success": False, "message": opened["message"] + " Nothing was typed."}
    box, shown = opened["box"], opened["contact"]
    axui.focus(box)
    time.sleep(0.2)
    type_text(text)                     # exact text, no Enter
    return {"success": True, "app": app, "contact": shown or contact, "text": text,
            "message": f"Typed '{text}' for {shown or contact} in {app} — waiting for your OK to send."}


_UI_NOISE = re.compile(r"^(today|yesterday|new|edited|reply|more|add reaction|\d{1,2}:\d{2}( ?[ap]m)?|"
                       r"\d{1,2}/\d{1,2}/\d{2,4}.*|jump to present|new messages?|message @.*)$", re.I)


def chat_lines(texts: list[str], contact: str, keep: int = 25) -> list[str]:
    """The conversation part of a chat window's texts: drop UI labels,
    keep the last `keep` lines (newest last)."""
    lines = [t for t in texts if len(t) > 1 and not _UI_NOISE.match(t.strip())]
    # the message list usually starts at the contact's name (header / first message)
    for i in range(len(lines) - 1, -1, -1):
        if _same_person(contact, lines[i]) and i < len(lines) - 1:
            lines = lines[i:]
            break
    return lines[-keep:]


def read_chat_messages(app: str = "", contact: str = "", count: int = 15) -> dict:
    """Open the chat with `contact` in `app` and read the latest messages
    (read-only — nothing is typed or sent)."""
    from core import axui
    app = app or next((a for a in CHAT_APPS if axui.pid_of(a)), "")
    if not app:
        return {"success": False, "message": "[-] Which app — Discord, Slack, WhatsApp?"}
    if not contact:
        return _chat_overview(app)
    opened = open_chat(app, contact)
    if not opened["success"]:
        return {"success": False, "message": opened["message"]}
    time.sleep(0.6)
    lines = chat_lines(axui.texts(app), opened["contact"], keep=max(5, min(int(count or 15), 40)))
    if not lines:
        return {"success": False, "message": f"[-] I opened the chat with {opened['contact']} but couldn't read any messages."}
    return {"success": True, "app": app, "contact": opened["contact"], "messages": lines,
            "message": f"Latest messages in the chat with {opened['contact']} on {app}:\n" + "\n".join(lines)}


_UI_WORDS = {"search", "chats", "chat", "archived", "filter", "all", "unread", "favourites", "favorites", "groups", "calls",
             "status", "settings", "new chat", "communities", "channels", "updates", "starred", "contacts", "whatsapp",
             "discord", "slack", "telegram", "messages", "direct messages", "friends", "home", "servers", "inbox", "today", "yesterday"}


def _chat_overview(app: str) -> dict:
    """"Check my WhatsApp messages" — no one named: open the app and read the
    chat list (names, last messages, unread counts).  Read-only."""
    from core import axui
    from actuators.system_control import open_application
    open_application(app)
    time.sleep(1.2)                      # let the window draw its chat list
    seen, lines = set(), []
    for t in axui.texts(app):
        t = " ".join(str(t).split())
        if len(t) < 2 or t.lower() in _UI_WORDS or t in seen or len(t) > 160:
            continue
        seen.add(t)
        lines.append(t)
        if len(lines) >= 24:
            break
    if not lines:
        return {"success": False, "message": f"[-] I opened {app} but couldn't read its chat list — is Accessibility on for DEMASCAS?"}
    unread = [l for l in lines if "unread" in l.lower()]
    head = f"{len(unread)} chat{'s' if len(unread) != 1 else ''} with unread messages. " if unread else ""
    return {"success": True, "app": app, "messages": lines,
            "message": f"{head}Your recent {app} chats:\n" + "\n".join(lines[:12])}


def deliver_chat_message(app: str, contact: str, text: str) -> dict:
    """Press Enter — only reachable through the user's spoken yes (/confirm)."""
    from core import axui
    from actuators.system_control import press_keyboard_shortcut
    try:
        from actuators.system_control import open_application
        open_application(app)
        time.sleep(0.4)
        box = axui.message_box(app) if axui.available() else None
        shown = re.sub(r"(?i)^message\s+@?", "", box["name"]).strip() if box else ""
        if box and not _same_person(contact, shown):
            return {"success": False, "message": f"[-] The open chat changed to {shown}; I didn't send it."}
        if box:
            axui.focus(box)
    except Exception as _e:  # was silent
        __import__("logging").getLogger(__name__).warning("ignored error: %r", _e)
    press_keyboard_shortcut("return")
    return {"success": True, "message": f"Sent '{text}' to {contact} on {app}."}


def discard_chat_draft(app: str, contact: str = "", text: str = "") -> dict:
    """The user said no: clear what was typed."""
    from actuators.system_control import press_keyboard_shortcut, open_application
    open_application(app)
    time.sleep(0.3)
    press_keyboard_shortcut("cmd+a, delete")
    return {"success": True, "message": "Okay, I didn't send it."}
