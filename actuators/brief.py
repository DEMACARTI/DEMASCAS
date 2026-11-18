"""
DEMASCAS — actuators/brief.py
The morning brief (OpenJarvis's "morning digest", done on-device): agenda,
reminders, unread mail, weather and the topics you watch — collected in
parallel and returned as notes for the brain to turn into a few spoken lines.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor

STATE = os.path.expanduser("~/.demascas/brief.json")


def _home_city() -> str:
    try:
        from core.preferences import get_all_preferences
        p = get_all_preferences()
        for k in ("home_city", "city", "location", "weather_location"):
            if p.get(k):
                return str(p[k])
    except Exception as e:
        __import__("logging").getLogger(__name__).debug("no city preference: %r", e)
    return ""


def _msg(r) -> str:
    if isinstance(r, dict):
        return str(r.get("message", ""))
    return str(r or "")


def briefed_today() -> bool:
    try:
        with open(STATE) as f:
            return json.load(f).get("date") == dt.date.today().isoformat()
    except (OSError, ValueError):
        return False


def mark_briefed() -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"date": dt.date.today().isoformat()}, f)


def morning_brief() -> dict:
    """Today's agenda, reminders, unread mail, weather and watched topics."""
    from actuators import apple
    jobs = {
        "agenda": lambda: apple.list_agenda("today"),
        "reminders": lambda: apple.list_reminders(count=5),
        "mail": lambda: apple.read_mail(count=5, unread_only=True),
    }
    city = _home_city()
    if city:
        from actuators.smart_search import get_weather
        jobs["weather"] = lambda: get_weather(city)
    try:
        from core.web import crawler, store
        topics = list(crawler._state().get("watch", {}))[:3]
        if topics:
            jobs["topics"] = lambda: "\n".join(f"{t}: {store.context_block(t, max_chars=300, k=1)}" for t in topics)
    except Exception as e:
        __import__("logging").getLogger(__name__).debug("no watched topics: %r", e)
    notes = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(f) for k, f in jobs.items()}
        for k, fut in futures.items():
            try:
                notes[k] = _msg(fut.result(timeout=40))
            except Exception as e:
                notes[k] = f"[-] {k} unavailable ({e})"
    mark_briefed()
    today = dt.date.today().strftime("%A %-d %B")
    text = f"Today is {today}.\n" + "\n".join(f"{k.upper()}: {v}" for k, v in notes.items() if v)
    return {"success": True, "message": text, "sections": notes}
