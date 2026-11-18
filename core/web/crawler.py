"""
DEMASCAS — core/web/crawler.py
DEMASCAS researches the web on its own and keeps what it reads.

Tools
  research_web(topic)        search, read the best pages, store them, answer
  scrape_webpage(url)        read one page and store it
  watch_topic(topic)         keep a topic fresh in the background
  web_knowledge(query)       what DEMASCAS already knows (no network)
  forget_web(match)          drop stored pages by site / topic / URL

Background researcher (start_background) — "learning while you're away":
  * questions DEMASCAS couldn't answer from what it knew get researched
  * watched topics are refreshed on their schedule
  Runs only after IDLE_SECONDS without a command, stops the moment a
  command arrives, and is capped at MAX_PAGES_PER_HOUR (polite + light).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core.web import fetch, store

STATE_PATH = os.path.join(store.WEB_DIR, "state.json")
IDLE_SECONDS = 90
MAX_PAGES_PER_HOUR = 24
PAGES_PER_TOPIC = 4

_last_activity = time.time()
_abort = threading.Event()
_job_lock = threading.Lock()
_page_log: list[float] = []


# ── state: watchlist + curiosity queue ───────────────────────────────

def _state() -> dict:
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except (FileNotFoundError, ValueError):
        s = {}
    s.setdefault("watch", {})          # topic → {"every_hours", "last"}
    s.setdefault("queue", [])          # topics to research when idle
    s.setdefault("done", {})           # topic → last researched
    return s


def _save_state(s: dict) -> None:
    os.makedirs(store.WEB_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE_PATH)


def settings() -> dict:
    try:
        with open(os.path.join(os.path.expanduser("~"), ".demascas", "config.json")) as f:
            web = (json.load(f).get("web") or {})
    except (FileNotFoundError, ValueError):
        web = {}
    return {"auto_research": bool(web.get("auto_research", True)),
            "max_pages_per_hour": int(web.get("max_pages_per_hour", MAX_PAGES_PER_HOUR))}


def note_activity() -> None:
    """A user command arrived: background research yields immediately."""
    global _last_activity
    _last_activity = time.time()
    if _job_lock.locked():
        _abort.set()


# ── search + read ────────────────────────────────────────────────────

_SKIP = re.compile(r"youtube\.com|youtu\.be|facebook\.com|instagram\.com|tiktok\.com|"
                   r"x\.com|twitter\.com|pinterest\.|linkedin\.com|\.pdf($|\?)", re.I)


def search(query: str, n: int = 8) -> list[str]:
    from ddgs import DDGS
    try:
        with DDGS(timeout=6) as d:
            hits = d.text(query, max_results=n)
    except Exception:
        return []
    urls = []
    for h in hits:
        u = h.get("href", "")
        if u and u not in urls and not _SKIP.search(u):
            urls.append(u)
    return urls


def _budget_left() -> int:
    now = time.time()
    _page_log[:] = [t for t in _page_log if now - t < 3600]
    return settings()["max_pages_per_hour"] - len(_page_log)


def _read_and_store(urls: list[str], topic: str, background: bool) -> tuple[list[str], int]:
    stored, sources = 0, []

    def _one(u):
        if background and (_abort.is_set() or _budget_left() <= 0):
            return u, None
        _page_log.append(time.time())
        return u, fetch.fetch(u)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for u, page in pool.map(_one, urls):
            if page is None or not page.ok:
                continue
            n = store.add_page(u, page.title, page.text, page.tables, topic=topic,
                               published=page.published)
            if n:
                stored += n
                sources.append(page.title or u)
    return sources, stored


_ANSWER_PROMPT = """Answer the question using ONLY these notes from the web.
If the notes don't contain the answer, say what they do say in one sentence.
Two or three short spoken sentences, no markdown, no URLs.
Question: {q}
Notes:
{notes}
Return JSON: {{"say": "..."}}"""


def _answer(question: str) -> str:
    notes = store.context_block(question, max_chars=1400, k=4)
    if not notes:
        return ""
    try:
        from actuators.documents import _llm_json
        data = _llm_json(_ANSWER_PROMPT.format(q=question, notes=notes), max_tokens=180) or {}
        return str(data.get("say", "")).strip()
    except Exception:
        return ""


# ── tools ────────────────────────────────────────────────────────────

def research_web(topic: str, answer: bool = True, fresh_days: float = 7.0,
                 background: bool = False) -> dict:
    """Search the web for a topic, read the best pages, remember them, and
    (optionally) answer from what was read.  Uses stored knowledge when
    it's already fresh — no network at all."""
    topic = (topic or "").strip()
    if not topic:
        return {"success": False, "message": "[-] What should I research?"}
    t0 = time.time()
    have = store.coverage(topic, fresh_days=fresh_days)
    sources, stored = [], 0
    if have < 3:
        urls = search(topic)[:PAGES_PER_TOPIC + 2]
        sources, stored = _read_and_store(urls[:PAGES_PER_TOPIC + 2], topic, background)
        s = _state()
        s["done"][topic.lower()] = time.time()
        s["queue"] = [q for q in s["queue"] if q.lower() != topic.lower()]
        _save_state(s)
    reply = _answer(topic) if answer else ""
    if not reply:
        reply = (f"I read {len(sources)} pages about {topic} and saved what I found."
                 if sources else
                 (f"I already know about {topic} from earlier reading." if have else
                  f"I couldn't find readable pages about {topic}."))
    return {"success": bool(sources) or have > 0, "message": reply, "sources": sources[:5],
            "new_chunks": stored, "from_memory": have >= 3, "seconds": round(time.time() - t0, 1)}


def scrape_webpage(url: str) -> dict:
    """Read one web page, remember it, and return its main text."""
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    page = fetch.fetch(url)
    if not page.ok:
        return {"success": False, "message": f"[-] Couldn't read {url}: {page.error}"}
    n = store.add_page(url, page.title, page.text, page.tables, topic=page.title)
    return {"success": True, "title": page.title, "saved_chunks": n,
            "message": f"Read '{page.title or url}' and saved it.", "text": page.text[:2500]}


def watch_topic(topic: str, every_hours: float = 12.0) -> dict:
    """Keep a topic up to date: DEMASCAS re-reads it in the background."""
    topic = (topic or "").strip()
    if not topic:
        return {"success": False, "message": "[-] Which topic should I keep an eye on?"}
    s = _state()
    s["watch"][topic] = {"every_hours": max(1.0, float(every_hours)), "last": 0}
    _save_state(s)
    return {"success": True, "message": f"I'll keep an eye on {topic} and refresh it every "
                                        f"{int(max(1.0, float(every_hours)))} hours."}


def unwatch_topic(topic: str) -> dict:
    s = _state()
    gone = [t for t in s["watch"] if t.lower() == (topic or "").strip().lower()]
    for t in gone:
        s["watch"].pop(t)
    _save_state(s)
    return {"success": bool(gone), "message": f"Stopped watching {topic}." if gone else
            f"I wasn't watching {topic}."}


def web_knowledge(query: str) -> dict:
    """What DEMASCAS already learned from the web about something (offline)."""
    hits = store.recall(query, k=4)
    st = store.stats()
    if not hits:
        return {"success": True, "message": f"I haven't read anything about {query} yet.",
                "stats": st}
    return {"success": True, "message": store.context_block(query, max_chars=1400, k=4),
            "sources": sorted({h["site"] for h in hits}), "stats": st}


def forget_web(match: str) -> dict:
    n = store.forget(match)
    return {"success": True, "message": f"Forgot {n} saved web notes about {match}." if n
            else f"I had nothing saved about {match}."}


# ── curiosity: questions DEMASCAS couldn't answer well ───────────────

_QUESTION = re.compile(r"^(?:who|what|when|where|why|how|which|is|are|was|were|does|did|can|"
                       r"tell me about|explain|define|latest|news)\b", re.I)
_PERSONAL = re.compile(r"\b(my|me|i|i'm|you|your|yourself|demascas|screen|file|folder|email|"
                       r"clipboard|remember|open|close|send|message|play|display(?:ed|ing)?|"
                       r"showing|shown|window|tab|app|apps|running|battery|wifi|volume|"
                       r"brightness|computer|laptop|mac|desktop|this|that|here)\b", re.I)


# "what is up?" is a greeting, not a research topic (live log 18 Nov)
_SMALL_TALK = re.compile(r"^\W*(hey|hi|hello|so|yo)?\W*(what(?:'?s| is) (?:up|new|going on)|how(?:'?s| is) it going|"
                         r"how (?:are|r) (?:you|u)(?: doing)?|how have you been|how do you do|sup|wassup)\W*$", re.I)


def note_question(command: str) -> bool:
    """Queue a world-knowledge question for background research if the
    store can't answer it yet.  (Personal / device questions never leave
    the machine.)"""
    q = (command or "").strip().rstrip("?.! ")
    if len(q) < 8 or not _QUESTION.search(q) or _PERSONAL.search(q) or _SMALL_TALK.match(q):
        return False
    try:   # only world-knowledge questions — never device / screen ones
        from core.routing import intent
        if intent(q)[0] == "action":
            return False
    except Exception:
        pass
    if store.coverage(q, fresh_days=14) >= 2:
        return False
    s = _state()
    if q.lower() in (x.lower() for x in s["queue"]):
        return False
    s["queue"] = (s["queue"] + [q])[-30:]
    _save_state(s)
    return True


# ── background worker ────────────────────────────────────────────────

def _next_job() -> str:
    s = _state()
    now = time.time()
    for topic, w in s["watch"].items():
        if now - w.get("last", 0) > w.get("every_hours", 12) * 3600:
            w["last"] = now
            _save_state(s)
            return topic
    if s["queue"]:
        return s["queue"][0]
    return ""


def _loop() -> None:
    while True:
        time.sleep(30)
        try:
            store.flush()
            if not settings()["auto_research"]:
                continue
            if time.time() - _last_activity < IDLE_SECONDS or _budget_left() <= 0:
                continue
            topic = _next_job()
            if not topic or not _job_lock.acquire(blocking=False):
                continue
            _abort.clear()
            try:
                r = research_web(topic, answer=False, fresh_days=0.5, background=True)
                if r.get("new_chunks"):
                    print(f"[WEB] Learned about '{topic}' while idle ({r['new_chunks']} notes, "
                          f"{len(r.get('sources', []))} pages).")
            finally:
                if _abort.is_set():           # interrupted: keep it queued
                    s = _state()
                    if topic not in s["queue"] and topic not in s["watch"]:
                        s["queue"].insert(0, topic)
                        _save_state(s)
                _job_lock.release()
        except Exception as e:
            print(f"[WEB] background research skipped: {e}")


def start_background() -> threading.Thread:
    t = threading.Thread(target=_loop, name="web-research", daemon=True)
    t.start()
    return t
