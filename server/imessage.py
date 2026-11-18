"""
DEMASCAS — server/imessage.py
Reach DEMASCAS from your phone over iMessage.

  • Inbound: new messages in ~/Library/Messages/chat.db (read-only) from
    YOUR handles only (config.json → "imessage": {"owner": ["+91…", "you@icloud.com"]}).
    Everyone else is ignored.  Texting your own number ("note to self")
    works too: DEMASCAS's replies start with REPLY_PREFIX so they're never
    read back as requests.
  • The Rust brain polls GET /channel/inbox and answers with POST
    /channel/reply — the same brain, rules and confirmations as by voice.
  • Needs Full Disk Access for the app running DEMASCAS (to read chat.db).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time

REPLY_PREFIX = "DEMASCAS: "
DB = os.path.expanduser("~/Library/Messages/chat.db")
STATE = os.path.expanduser("~/.demascas/imessage_state.json")

_lock = threading.Lock()
_inbox: list[dict] = []
_status = {"enabled": False, "error": "", "owner": []}


def settings() -> dict:
    try:
        with open(os.path.expanduser("~/.demascas/config.json")) as f:
            cfg = json.load(f).get("imessage") or {}
    except (OSError, ValueError):
        cfg = {}
    owner = [normalize_handle(h) for h in cfg.get("owner", []) if h]
    return {"enabled": bool(cfg.get("enabled", bool(owner))) and bool(owner), "owner": owner,
            "self_chat": bool(cfg.get("self_chat", True))}


def normalize_handle(h: str) -> str:
    """'+91 98765-43210' → '+919876543210'; emails lowercased."""
    h = (h or "").strip()
    if "@" in h:
        return h.lower()
    digits = re.sub(r"[^\d+]", "", h)
    return digits


def message_text(text, attributed: bytes | None) -> str:
    """Newer macOS keeps the text only in attributedBody (a typedstream blob)."""
    if text:
        return str(text)
    if not attributed:
        return ""
    blob = bytes(attributed)
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    j = blob.find(b"+", i)               # length-prefixed UTF-8 after a '+' marker
    if j < 0 or j + 2 > len(blob):
        return ""
    n = blob[j + 1]
    start = j + 2
    if n == 0x81:                         # 2-byte length
        n = int.from_bytes(blob[j + 2:j + 4], "little")
        start = j + 4
    return blob[start:start + n].decode("utf-8", "ignore")


def _load_state() -> int:
    try:
        with open(STATE) as f:
            return int(json.load(f).get("last_rowid", 0))
    except (OSError, ValueError):
        return 0


def _save_state(rowid: int) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"last_rowid": rowid}, f)


def new_messages(db_path: str, since_rowid: int, owner: list[str], self_chat: bool = True) -> tuple[list[dict], int]:
    """Messages after `since_rowid` that came from the owner.  Returns (messages, newest rowid)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute(
            """SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, IFNULL(h.id, ''), IFNULL(c.chat_identifier, '')
               FROM message m
               LEFT JOIN handle h ON h.ROWID = m.handle_id
               LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
               LEFT JOIN chat c ON c.ROWID = cmj.chat_id
               WHERE m.ROWID > ? ORDER BY m.ROWID LIMIT 50""", (since_rowid,)).fetchall()
    finally:
        con.close()
    out, newest = [], since_rowid
    for rowid, text, attributed, from_me, handle, chat_id in rows:
        newest = max(newest, rowid)
        body = message_text(text, attributed).strip()
        if not body or body.startswith(REPLY_PREFIX.strip()):
            continue
        who = normalize_handle(handle)
        chat = normalize_handle(chat_id)
        from_owner = (not from_me and who in owner)
        note_to_self = (from_me and self_chat and chat in owner)
        if from_owner or note_to_self:
            out.append({"rowid": rowid, "text": body, "from": who or chat})
    return out, newest


def send(handle: str, text: str) -> bool:
    """Reply over iMessage (AppleScript)."""
    msg = REPLY_PREFIX + text
    esc = msg.replace("\\", "\\\\").replace('"', '\\"')
    h = handle.replace('"', "")
    scripts = [
        f'tell application "Messages" to send "{esc}" to participant "{h}" of (1st account whose service type = iMessage)',
        f'tell application "Messages" to send "{esc}" to buddy "{h}" of (1st service whose service type = iMessage)',
    ]
    for s in scripts:
        try:
            if subprocess.run(["osascript", "-e", s], capture_output=True, timeout=15).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            continue
    return False


def _watch() -> None:
    last = _load_state()
    first = True
    while True:
        cfg = settings()
        _status.update(enabled=cfg["enabled"], owner=cfg["owner"])
        if not cfg["enabled"]:
            time.sleep(10)
            continue
        try:
            if first and last == 0:
                # start from now — never answer old history
                con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
                last = con.execute("SELECT IFNULL(MAX(ROWID), 0) FROM message").fetchone()[0]
                con.close()
                _save_state(last)
            first = False
            msgs, newest = new_messages(DB, last, cfg["owner"], cfg["self_chat"])
            if newest != last:
                last = newest
                _save_state(last)
            if msgs:
                with _lock:
                    _inbox.extend(msgs)
            _status["error"] = ""
        except sqlite3.OperationalError as e:
            _status["error"] = (f"Can't read Messages ({e}). Give the app running DEMASCAS Full Disk Access: "
                                "System Settings → Privacy & Security → Full Disk Access.")
            time.sleep(15)
        time.sleep(2)


def start() -> None:
    threading.Thread(target=_watch, name="imessage", daemon=True).start()


def take_inbox() -> list[dict]:
    with _lock:
        out = list(_inbox)
        _inbox.clear()
    return out


def push_inbox(text: str, sender: str = "exam") -> None:
    """Tests / exam: a message as if it arrived."""
    with _lock:
        _inbox.append({"rowid": 0, "text": text, "from": sender})


def status() -> dict:
    return dict(_status)
