"""
DEMASCAS — actuators/apple.py
The Apple apps on this Mac, read and written on-device (no account login,
nothing leaves the machine):

  Calendar   list_agenda, add_event          (EventKit)
  Reminders  remind_me, list_reminders       (EventKit — syncs to your iPhone)
  Contacts   find_contact                    (Contacts framework, fuzzy names)
  Calls      call_contact, recent_calls      (Phone app via iPhone / FaceTime; history read-only)
  Notes      read_notes, add_note            (Notes via AppleScript)
  Mail       read_mail, draft_mail           (Mail via AppleScript — never sends)

macOS asks once for permission the first time each is used.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import threading
import time

# ── time words → datetimes ───────────────────────────────────────────

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "noon": 12, "midnight": 0}


def parse_day(text: str, now: dt.datetime | None = None) -> dt.date:
    """'today' / 'tomorrow' / 'monday' / '2026-11-20' / 'next friday' → a date."""
    now = now or dt.datetime.now()
    t = (text or "today").lower()
    m = re.search(r"\d{4}-\d{2}-\d{2}", t)
    if m:
        return dt.date.fromisoformat(m.group(0))
    if "day after tomorrow" in t:
        return now.date() + dt.timedelta(days=2)
    if "tomorrow" in t:
        return now.date() + dt.timedelta(days=1)
    for i, d in enumerate(_WEEKDAYS):
        if d in t:
            ahead = (i - now.weekday()) % 7 or (7 if "next" in t or "today" not in t else 0)
            return now.date() + dt.timedelta(days=ahead)
    return now.date()


def parse_when(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """'tomorrow at 3 pm' / 'friday 10:30' / 'at 6' / 'in 2 hours' → a datetime."""
    now = now or dt.datetime.now()
    t = (text or "").lower().strip()
    if not t:
        return None
    m = re.search(r"in (\d+|an?|one|two|three|half an?) ?(minute|min|hour|hr)s?", t)
    if m:
        n = m.group(1)
        n = 0.5 if n.startswith("half") else (1 if n in ("a", "an", "one") else _NUM.get(n, None) or int(n))
        return now + (dt.timedelta(hours=n) if m.group(2).startswith("h") else dt.timedelta(minutes=n))
    day = parse_day(t, now)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", t) or None
    word = next((w for w in _NUM if re.search(rf"\b{w}\b", t)), None)
    if m and (m.group(2) or m.group(3) or re.search(r"\bat\s+\d", t)):
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        ap = (m.group(3) or "").replace(".", "")
    elif word:
        h, mi, ap = _NUM[word], 0, ""
        ap = "pm" if word == "noon" else ap
    else:
        return dt.datetime.combine(day, dt.time(9, 0)) if day != now.date() else None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 7:
        h += 12                          # "at 3" → 3 PM (nobody books 3 AM)
    when = dt.datetime.combine(day, dt.time(h % 24, mi))
    if when < now and day == now.date() and "today" not in t:
        when += dt.timedelta(days=1)
    return when


def spoken_time(d: dt.datetime) -> str:
    return d.strftime("%-I:%M %p").replace(":00 ", " ")


# ── EventKit (Calendar + Reminders) ─────────────────────────────────

_store = None
_access: dict[int, bool] = {}


def _event_store():
    global _store
    if _store is None:
        import EventKit
        _store = EventKit.EKEventStore.alloc().init()
    return _store


def _ensure_access(entity: int) -> bool:
    """entity 0 = events, 1 = reminders.  Asks macOS once (a system prompt)."""
    if _access.get(entity):
        return True
    import EventKit
    store = _event_store()
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(entity)
    if status in (3,):                    # full access
        _access[entity] = True
        return True
    if status in (1, 2):                  # restricted / denied
        return False
    done, ok = threading.Event(), [False]

    def cb(granted, _err):
        ok[0] = bool(granted)
        done.set()
    if entity == 0 and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(cb)
    elif entity == 1 and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
        store.requestFullAccessToRemindersWithCompletion_(cb)
    else:
        store.requestAccessToEntityType_completion_(entity, cb)
    done.wait(60)
    _access[entity] = ok[0]
    return ok[0]


def _nsdate(d: dt.datetime):
    import Foundation
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(d.timestamp())


def _pydt(nsdate) -> dt.datetime:
    return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970())


_NO_CAL = ("[-] I don't have access to your Calendar. Allow it in System Settings → Privacy & Security → "
           "Calendars (for the app running DEMASCAS).")
_NO_REM = ("[-] I don't have access to your Reminders. Allow it in System Settings → Privacy & Security → "
           "Reminders.")


def list_agenda(day: str = "today", days: int = 1) -> dict:
    """What's on your calendar: 'today', 'tomorrow', a weekday or a date."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] The calendar needs macOS."}
    if not _ensure_access(0):
        return {"success": False, "message": _NO_CAL}
    start_day = parse_day(day)
    start = dt.datetime.combine(start_day, dt.time.min)
    end = start + dt.timedelta(days=max(1, min(int(days or 1), 14)))
    store = _event_store()
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(_nsdate(start), _nsdate(end), None)
    events = sorted(store.eventsMatchingPredicate_(pred) or [], key=lambda e: e.startDate().timeIntervalSince1970())
    label = "today" if start_day == dt.date.today() else ("tomorrow" if start_day == dt.date.today() + dt.timedelta(days=1)
                                                         else start_day.strftime("%A %-d %B"))
    if not events:
        return {"success": True, "events": [], "message": f"Nothing on your calendar {label}."}
    rows = []
    for e in events[:15]:
        title = str(e.title() or "Untitled")
        loc = str(e.location() or "")
        when = "All day" if e.isAllDay() else spoken_time(_pydt(e.startDate()))
        rows.append({"time": when, "title": title, "location": loc, "calendar": str(e.calendar().title())})
    lines = [f"{r['time']} — {r['title']}" + (f" ({r['location']})" if r["location"] else "") for r in rows]
    return {"success": True, "events": rows,
            "message": f"{label.capitalize()} you have {len(rows)} thing{'s' if len(rows) != 1 else ''}: " + "; ".join(lines) + "."}


def add_event(title: str, when: str, duration_minutes: int = 60, location: str = "") -> dict:
    """Add an event to your default calendar (asks first)."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] The calendar needs macOS."}
    start = parse_when(when)
    if not title or not start:
        return {"success": False, "message": "[-] What's the event, and when?"}
    if not _ensure_access(0):
        return {"success": False, "message": _NO_CAL}
    import EventKit
    store = _event_store()
    ev = EventKit.EKEvent.eventWithEventStore_(store)
    ev.setTitle_(title.strip()[:1].upper() + title.strip()[1:])
    ev.setStartDate_(_nsdate(start))
    ev.setEndDate_(_nsdate(start + dt.timedelta(minutes=max(5, int(duration_minutes or 60)))))
    if location:
        ev.setLocation_(location)
    ev.setCalendar_(store.defaultCalendarForNewEvents())
    ok, err = store.saveEvent_span_error_(ev, 0, None)
    if not ok:
        return {"success": False, "message": f"[-] Calendar didn't save it: {err}"}
    return {"success": True, "message": f"Added {ev.title()} on {start.strftime('%A')} at {spoken_time(start)}."}


def remind_me(title: str, when: str = "", due_epoch: float = 0) -> dict:
    """A reminder in the Reminders app (syncs to your iPhone)."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Reminders need macOS."}
    due = dt.datetime.fromtimestamp(due_epoch) if due_epoch else parse_when(when)
    if not title:
        return {"success": False, "message": "[-] What should I remind you about?"}
    if not _ensure_access(1):
        return {"success": False, "message": _NO_REM}
    import EventKit
    import Foundation
    store = _event_store()
    r = EventKit.EKReminder.reminderWithEventStore_(store)
    r.setTitle_(title)
    r.setCalendar_(store.defaultCalendarForNewReminders())
    if due:
        cal = Foundation.NSCalendar.currentCalendar()
        comps = cal.components_fromDate_(0x2 | 0x4 | 0x8 | 0x20 | 0x40, _nsdate(due))   # Y M D h m
        r.setDueDateComponents_(comps)
        r.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(_nsdate(due)))
    ok, err = store.saveReminder_commit_error_(r, True, None)
    if not ok:
        return {"success": False, "message": f"[-] Reminders didn't save it: {err}"}
    return {"success": True, "message": f"Added to Reminders: {title}" + (f", {due.strftime('%A')} at {spoken_time(due)}." if due else ".")}


def list_reminders(scope: str = "open", count: int = 10) -> dict:
    """Your open reminders (soonest first)."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Reminders need macOS."}
    if not _ensure_access(1):
        return {"success": False, "message": _NO_REM}
    store = _event_store()
    pred = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, None)
    done, found = threading.Event(), []

    def cb(items):
        found.extend(items or [])
        done.set()
    store.fetchRemindersMatchingPredicate_completion_(pred, cb)
    done.wait(15)

    def due(r):
        c = r.dueDateComponents()
        return c.date().timeIntervalSince1970() if c is not None and c.date() is not None else 9e18
    items = sorted(found, key=due)[:max(1, min(int(count or 10), 30))]
    if not items:
        return {"success": True, "reminders": [], "message": "You have no open reminders."}
    names = [str(r.title()) for r in items]
    return {"success": True, "reminders": names, "message": f"You have {len(found)} open reminder{'s' if len(found) != 1 else ''}: " + "; ".join(names) + "."}


# ── Contacts ─────────────────────────────────────────────────────────

_NO_CONTACTS = "[-] I don't have access to your Contacts. Allow it in System Settings → Privacy & Security → Contacts."


def _contact_store():
    """The Contacts store, asking for access once.  None = no access."""
    import Contacts
    store = Contacts.CNContactStore.alloc().init()
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(0)
    if status == 0:
        done, ok = threading.Event(), [False]
        def granted(g, _e):
            # must return None: PyObjC aborts the WHOLE process (the tool
            # server) if a void completion block returns a value
            ok[0] = bool(g)
            done.set()
        store.requestAccessForEntityType_completionHandler_(0, granted)
        done.wait(60)
        return store if ok[0] else None
    return None if status in (1, 2) else store


_CONTACTS_CACHE: dict = {"at": 0.0, "people": []}


def _all_contacts() -> list[dict] | None:
    """Every contact's name, phones and emails — read once, kept 10 minutes
    (fuzzy matching needs them all: "Vijwal" must still find "Vijval")."""
    if time.time() - _CONTACTS_CACHE["at"] < 600 and _CONTACTS_CACHE["people"]:
        return _CONTACTS_CACHE["people"]
    store = _contact_store()
    if store is None:
        return None
    import Contacts
    keys = [Contacts.CNContactGivenNameKey, Contacts.CNContactFamilyNameKey, Contacts.CNContactNicknameKey,
            Contacts.CNContactPhoneNumbersKey, Contacts.CNContactEmailAddressesKey]
    req = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    people: list[dict] = []

    def each(c, _stop):
        full = f"{c.givenName()} {c.familyName()}".strip() or str(c.nickname() or "")
        if full:
            people.append({"name": full, "nickname": str(c.nickname() or ""),
                           "phones": [str(p.value().stringValue()) for p in (c.phoneNumbers() or [])],
                           "emails": [str(e.value()) for e in (c.emailAddresses() or [])]})
        return None
    store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, each)
    _CONTACTS_CACHE.update(at=time.time(), people=people)
    return people


def match_contacts(name: str, people: list[dict] | None = None) -> list[dict]:
    """Contacts whose name sounds like `name`, best first (heard names are
    often misspelt: "Hatem Thai" → "Hatim Tai")."""
    from actuators.file_system import name_score
    people = people if people is not None else (_all_contacts() or [])
    q = (name or "").strip()
    if not q:
        return []
    scored = []
    for p in people:
        first = p["name"].split()[0] if p["name"] else ""
        sc = max(name_score(q, p["name"]), name_score(q, p["nickname"]) if p["nickname"] else 0.0,
                 name_score(q, first) if first else 0.0,
                 # "Wij Val" (heard as two words) vs "Vijval"
                 name_score(q.replace(" ", ""), first) if " " in q and first else 0.0)
        if sc >= 0.6:
            scored.append((sc, p))
    scored.sort(key=lambda x: -x[0])
    # a clear winner hides weak look-alikes
    if len(scored) > 1 and scored[0][0] >= 0.95 and scored[1][0] < 0.95:
        scored = scored[:1]
    return [p for _, p in scored[:5]]


def find_contact(name: str) -> dict:
    """Phone numbers and email addresses for a person in your Contacts."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Contacts need macOS."}
    if not name or name.strip().lower() in ("contacts", "contact", "my contacts", "everyone", "all"):
        return {"success": False, "message": "[-] Who should I look up?"}
    people_all = _all_contacts()
    if people_all is None:
        return {"success": False, "message": _NO_CONTACTS}
    people = match_contacts(name, people_all)
    if not people:
        return {"success": True, "people": [], "message": f"I couldn't find {name} in your contacts."}
    p = people[0]
    bits = ([f"phone {p['phones'][0]}"] if p["phones"] else []) + ([f"email {p['emails'][0]}"] if p["emails"] else [])
    return {"success": True, "people": people, "message": f"{p['name']}: " + (", ".join(bits) or "no number or email saved") + "."}


# ── Calls (Phone app via iPhone, or FaceTime) ────────────────────────

_VIA = {"phone": "tel://", "call": "tel://", "facetime_audio": "facetime-audio://", "facetime": "facetime://"}


def _dial_digits(number: str) -> str:
    return re.sub(r"[^\d+]", "", number or "")


def resolve_callee(name: str = "", number: str = "") -> dict:
    """Who exactly would be called — BEFORE asking "Call X on Y?".
    {success, number, display} or {success: False, ask/message}."""
    if number and len(_dial_digits(number)) >= 5:
        return {"success": True, "number": _dial_digits(number), "display": name or number}
    if not name or name.strip().lower() in ("contacts", "someone", "them", "him", "her"):
        return {"success": False, "message": "Who should I call?"}
    people_all = _all_contacts()
    if people_all is None:
        return {"success": False, "message": _NO_CONTACTS}
    people = [p for p in match_contacts(name, people_all) if p["phones"]]
    if not people:
        return {"success": False, "message": f"I couldn't find {name} with a phone number in your contacts. Who should I call?"}
    if len(people) > 1:
        names = ", ".join(p["name"] for p in people[:3])
        return {"success": False, "message": f"Which one: {names}?", "people": people[:3]}
    p = people[0]
    return {"success": True, "number": _dial_digits(p["phones"][0]), "display": p["name"], "spoken_number": p["phones"][0]}


def call_contact(name: str = "", number: str = "", via: str = "phone") -> dict:
    """Place a call (Phone app through your iPhone, or FaceTime).  Only ever
    runs after a spoken yes — the tool server asks "Call X on Y?" first."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Calling needs macOS."}
    who = resolve_callee(name, number)
    if not who.get("success"):
        return who
    url = _VIA.get((via or "phone").lower().replace("-", "_").replace(" ", "_"), "tel://") + who["number"]
    r = subprocess.run(["open", url], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"success": False, "message": f"[-] I couldn't start the call: {(r.stderr or '').strip()[:120]}"}
    return {"success": True, "message": f"Calling {who['display']}."}


_CALL_DB = os.path.expanduser("~/Library/Application Support/CallHistoryDB/CallHistory.storedata")
_NO_CALL_HISTORY = ("[-] I can't read your call history yet. Give the app running DEMASCAS (Terminal) Full Disk "
                    "Access in System Settings → Privacy & Security → Full Disk Access, then restart DEMASCAS.")


def _last_digits(n: str) -> str:
    d = re.sub(r"\D", "", n or "")
    return d[-10:]


def recent_calls(count: int = 5, missed_only: bool = False) -> dict:
    """The latest calls (from your iPhone / FaceTime history), newest first,
    with numbers turned into contact names.  Read-only."""
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{_CALL_DB}?mode=ro", uri=True, timeout=2)
        cols = {r[1] for r in db.execute("PRAGMA table_info(ZCALLRECORD)")}
    except sqlite3.Error:
        return {"success": False, "message": _NO_CALL_HISTORY}
    if not cols:
        return {"success": False, "message": _NO_CALL_HISTORY}
    want = [c for c in ("ZADDRESS", "ZNAME", "ZDATE", "ZORIGINATED", "ZANSWERED", "ZDURATION") if c in cols]
    where = " WHERE ZORIGINATED = 0 AND ZANSWERED = 0" if missed_only and {"ZORIGINATED", "ZANSWERED"} <= cols else ""
    rows = db.execute(f"SELECT {', '.join(want)} FROM ZCALLRECORD{where} ORDER BY ZDATE DESC LIMIT ?",
                      (max(1, min(int(count or 5), 20)),)).fetchall()
    db.close()
    book = {}
    for p in (_all_contacts() or []):
        for ph in p["phones"]:
            book[_last_digits(ph)] = p["name"]
    calls, lines = [], []
    epoch = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)   # Core Data dates
    for r in rows:
        rec = dict(zip(want, r))
        addr = str(rec.get("ZADDRESS") or "")
        who = book.get(_last_digits(addr)) or (rec.get("ZNAME") or "") or addr or "unknown"
        when = (epoch + dt.timedelta(seconds=float(rec.get("ZDATE") or 0))).astimezone()
        kind = ("outgoing" if rec.get("ZORIGINATED") else ("incoming" if rec.get("ZANSWERED") else "missed"))
        calls.append({"name": who, "number": addr, "when": when.isoformat(timespec="minutes"), "kind": kind,
                      "seconds": int(rec.get("ZDURATION") or 0)})
        lines.append(f"{who} ({kind}, {when.strftime('%-d %b %-I:%M %p')})")
    if not calls:
        return {"success": True, "calls": [], "message": "No missed calls." if missed_only else "Your call history is empty."}
    head = "Missed calls" if missed_only else f"Your last {len(calls)} calls"
    return {"success": True, "calls": calls, "message": f"{head}: " + "; ".join(lines) + "."}


# ── Notes and Mail (AppleScript) ────────────────────────────────────

def _osa(script: str, timeout: int = 20) -> tuple[bool, str]:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "timed out"


def _q(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def read_notes(query: str = "", count: int = 5) -> dict:
    """Your notes whose title or text mention `query` (newest first)."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Notes need macOS."}
    n = max(1, min(int(count or 5), 10))
    filt = f'whose name contains "{_q(query)}" or plaintext contains "{_q(query)}"' if query else ""
    ok, out = _osa(f'''tell application "Notes"
        set out to ""
        set ns to (notes {filt})
        repeat with i from 1 to (count of ns)
            if i > {n} then exit repeat
            set x to item i of ns
            set out to out & (name of x) & " ::: " & (text 1 thru (min(300, length of (plaintext of x))) of (plaintext of x)) & linefeed & "@@" & linefeed
        end repeat
        return out
    end tell'''.replace("min(300, length of (plaintext of x))", "(my minOf(300, length of (plaintext of x)))") + '''
on minOf(a, b)
    if a < b then return a
    return b
end minOf''')
    if not ok:
        return {"success": False, "message": f"[-] Notes didn't answer: {out[:120]}"}
    notes = []
    for block in out.split("@@"):
        if ":::" in block:
            title, body = block.split(":::", 1)
            notes.append({"title": title.strip(), "text": body.strip()})
    if not notes:
        return {"success": True, "notes": [], "message": f"No notes{' about ' + query if query else ''}."}
    return {"success": True, "notes": notes,
            "message": "\n".join(f"{x['title']}: {x['text'][:200]}" for x in notes)}


def add_note(text: str, title: str = "") -> dict:
    """Append to the note called `title` (created if missing), or make a new note."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Notes need macOS."}
    if not text:
        return {"success": False, "message": "[-] What should the note say?"}
    title = title.strip() or text.split(".")[0][:40]
    ok, out = _osa(f'''tell application "Notes"
        set found to (notes whose name is "{_q(title)}")
        if (count of found) > 0 then
            set x to item 1 of found
            set body of x to (body of x) & "<div>{_q(text)}</div>"
            return "appended"
        else
            make new note with properties {{name:"{_q(title)}", body:"<div><b>{_q(title)}</b></div><div>{_q(text)}</div>"}}
            return "created"
        end if
    end tell''')
    if not ok:
        return {"success": False, "message": f"[-] Notes didn't save it: {out[:120]}"}
    return {"success": True, "message": (f"Added to your {title} note." if out == "appended" else f"Made a new note: {title}.")}


NO_MAIL_ACCOUNT = ("The Mail app on this Mac has no email account yet, so I can't read your mail. "
                   "Add your Gmail once in System Settings, Internet Accounts, and then I can.")

_MAIL_PREF = os.path.join(os.path.expanduser("~"), ".demascas", "mail_source")


def _mail_source_pref() -> str:
    try:
        with open(_MAIL_PREF) as f:
            return f.read().strip()
    except OSError:
        return ""


def _remember_mail_source(src: str) -> None:
    try:
        os.makedirs(os.path.dirname(_MAIL_PREF), exist_ok=True)
        with open(_MAIL_PREF, "w") as f:
            f.write(src)
    except OSError:
        pass


def _mail_running() -> bool:
    try:
        from core.axui import pid_of
        return bool(pid_of("Mail"))
    except Exception:
        return subprocess.run(["pgrep", "-x", "Mail"], capture_output=True).returncode == 0


def _read_apple_mail(n: int, unread_only: bool, sender: str) -> dict:
    """Mail.app's inbox.  Launched hidden if it isn't running; -609 ("connection
    is invalid" while Mail is still starting) is retried once."""
    if not _mail_running():
        subprocess.run(["open", "-gj", "-a", "Mail"], capture_output=True)
        for _ in range(16):                     # ≤ 8 s for Mail to come up
            time.sleep(0.5)
            if _mail_running():
                break
        time.sleep(1.0)
    cond = []
    if unread_only:
        cond.append("read status is false")
    if sender:
        cond.append(f'sender contains "{_q(sender)}"')
    filt = ("whose " + " and ".join(cond)) if cond else ""
    script = f'''tell application "Mail"
        if (count of accounts) is 0 then return "@@NO_ACCOUNTS@@"
        set out to ""
        set ms to (messages of inbox {filt})
        repeat with i from 1 to (count of ms)
            if i > {n} then exit repeat
            set m to item i of ms
            set c to content of m
            if (length of c) > 300 then set c to text 1 thru 300 of c
            set out to out & (sender of m) & " ::: " & (subject of m) & " ::: " & c & linefeed & "@@" & linefeed
        end repeat
        return out
    end tell'''
    ok, out = _osa(script, timeout=12)
    if not ok and ("-609" in out or "-600" in out or "Connection is invalid" in out):
        time.sleep(2.0)
        ok, out = _osa(script, timeout=10)
    if not ok:
        why = "it took too long to answer" if out == "timed out" else "it isn't responding"
        return {"success": False, "message": f"I couldn't read Mail — {why}."}
    if out.strip() == "@@NO_ACCOUNTS@@":
        return {"success": False, "no_accounts": True, "message": NO_MAIL_ACCOUNT}
    mails = []
    for block in out.split("@@"):
        parts = block.split(":::")
        if len(parts) >= 3:
            mails.append({"from": parts[0].strip(), "subject": parts[1].strip(), "text": " ".join(parts[2].split())[:300]})
    if not mails:
        return {"success": True, "mails": [], "source": "mail",
                "message": "No new mail." if unread_only else "Your inbox is empty."}
    return {"success": True, "mails": mails, "source": "mail",
            "message": "\n".join(f"From {m['from']}: {m['subject']} — {m['text'][:160]}" for m in mails)}


def read_mail(count: int = 5, unread_only: bool = True, sender: str = "", source: str = "") -> dict:
    """Latest emails (subject, sender, the start of the text).  source="gmail"
    reads Gmail in the browser; otherwise Mail.app, falling back to Gmail when
    Mail has no account.  Whichever works is remembered."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Mail needs macOS."}
    n = max(1, min(int(count or 5), 15))
    src = (source or _mail_source_pref() or "").lower()
    if src != "gmail":
        r = _read_apple_mail(n, unread_only, sender)
        if not r.get("no_accounts"):
            if r.get("success"):
                _remember_mail_source("mail")
            return r
    from actuators.browser import gmail_read_inbox
    g = gmail_read_inbox(count=n, unread_only=unread_only, sender=sender)
    if g.get("success"):
        _remember_mail_source("gmail")
    return g


def draft_mail(to: str, subject: str = "", body: str = "") -> dict:
    """Open a new email draft in Mail (it is NOT sent)."""
    if sys.platform != "darwin":
        return {"success": False, "message": "[-] Mail needs macOS."}
    if not to:
        return {"success": False, "message": "[-] Who is the email to?"}
    ok, out = _osa(f'''tell application "Mail"
        set m to make new outgoing message with properties {{visible:true, subject:"{_q(subject)}", content:"{_q(body)}"}}
        tell m to make new to recipient at end of to recipients with properties {{address:"{_q(to)}"}}
        activate
        return "ok"
    end tell''')
    if not ok:
        return {"success": False, "message": f"[-] Mail didn't open the draft: {out[:120]}"}
    return {"success": True, "message": f"I've opened a draft to {to} in Mail — it's not sent; check it and press send when ready."}
