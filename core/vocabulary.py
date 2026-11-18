"""
DEMASCAS — core/vocabulary.py
Words DEMASCAS must hear right: names of apps it learned, words the user
taught ("it's spelled DISCORD"), and corrections for what speech
recognition keeps getting wrong ("this cord" → "Discord").

Stored in ~/.demascas/vocabulary.json:
  {"terms": ["Discord", ...], "corrections": {"this cord": "Discord", ...}}

Used by
  * the tool server's command normaliser   (apply_corrections)
  * the daemon's whisper prompt             (GET /vocabulary → terms)
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading

VOCAB_PATH = os.path.join(os.path.expanduser("~"), ".demascas", "vocabulary.json")
_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime = 0.0

# DEMASCAS's own name is constantly heard as the city.  The user chose:
# assume DEMASCAS unless the city is clearly meant.
_SELF_NAME = re.compile(r"\b(damascus|demascus|the mask is|demas\s*,?\s*cas|dhamaskas|da mascus)\b",
                        re.IGNORECASE)
_CITY_CONTEXT = re.compile(r"\b(syria|syrian|city|capital|old city|umayyad|steel|levant|middle east)\b",
                           re.IGNORECASE)


def _load() -> dict:
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(VOCAB_PATH)
    except OSError:
        mtime = 0.0
    with _lock:
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        data = {"terms": [], "corrections": {}}
        if mtime:
            try:
                with open(VOCAB_PATH) as f:
                    raw = json.load(f)
                data["terms"] = [str(t) for t in raw.get("terms", []) if str(t).strip()]
                data["corrections"] = {str(k).lower(): str(v) for k, v in (raw.get("corrections") or {}).items()}
            except (ValueError, OSError):
                pass
        _cache, _cache_mtime = data, mtime
        return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(VOCAB_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(VOCAB_PATH), prefix=".vocab.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, VOCAB_PATH)


def terms() -> list[str]:
    """Words to bias speech recognition towards (taught words + learned apps)."""
    out = ["DEMASCAS"] + _load()["terms"]
    try:
        from core.appknowledge.learner import known_apps
        out += known_apps()
    except Exception:
        pass
    return list(dict.fromkeys(t for t in out if t))


def add_term(term: str) -> None:
    term = term.strip()
    if not term:
        return
    data = dict(_load())
    if term.lower() not in {t.lower() for t in data["terms"]}:
        data["terms"] = data["terms"] + [term]
        _save(data)


def add_correction(heard: str, meant: str) -> None:
    heard, meant = heard.strip().lower(), meant.strip()
    if not heard or not meant or heard == meant.lower():
        return
    data = dict(_load())
    data["corrections"] = {**data["corrections"], heard: meant}
    _save(data)
    add_term(meant)


def apply_corrections(text: str) -> str:
    """'open this cord' → 'open Discord'; 'presentation about Damascus' →
    '… DEMASCAS' (unless the city is clearly meant)."""
    if not text:
        return text
    out = text
    for heard, meant in sorted(_load()["corrections"].items(), key=lambda kv: -len(kv[0])):
        # "brave" → "Brave Browser" must leave an already-complete
        # "Brave Browser" alone (live log: "Brave Browser Browser")
        def _fix(m: re.Match, meant: str = meant, src: str = out) -> str:
            return m.group(0) if src[m.start():m.start() + len(meant)].lower() == meant.lower() else meant
        out = re.sub(rf"\b{re.escape(heard)}\b", _fix, out, flags=re.IGNORECASE)
    # Taught terms: fix case ("discord" → "Discord") so names reach tools intact.
    for t in terms():
        if len(t) > 3:
            out = re.sub(rf"\b{re.escape(t)}\b", t, out, flags=re.IGNORECASE)
    if _SELF_NAME.search(out) and not _CITY_CONTEXT.search(out):
        out = _SELF_NAME.sub("DEMASCAS", out)
    # "the discard app" → "the Discord app": only before "app", and only for
    # apps DEMASCAS has learned ("discard" is also a normal word).
    def _app_word(m: re.Match) -> str:
        word = m.group(1)
        try:
            from core.appnames import resolve
            hit, conf = resolve(word)
        except Exception:
            return m.group(0)
        learned = {t.lower() for t in terms()}
        return f"{hit} {m.group(2)}" if hit and conf >= 0.8 and hit.lower() in learned else m.group(0)
    out = re.sub(r"\b([A-Za-z]{4,})\s+(app|application)\b", _app_word, out, flags=re.IGNORECASE)
    # "DEMASCAS, DEMASCAS" (both spellings were heard) → one
    out = re.sub(r"\b(\w+)(?:\s*,\s*\1\b)+", r"\1", out)
    return out


# ── "It's spelled DISCORD" / "I meant Discord" ───────────────────────

_TEACH = re.compile(
    r"(?:\bspell(?:ed|t)?\s+(?:it\s+|as\s+|like\s+)*|\bit'?s\s+(?:spelled|spelt|called)\s+|"
    r"\bi\s+meant\s+|\bit'?s\s+not\s+\w+[, ]+it'?s\s+)"
    r"([A-Za-z][A-Za-z .'-]{1,30}?)(?=[,.!?]|\s+and\b|\s+i\b|\s+for\b|\s+remember|$)",
    re.IGNORECASE)


def parse_teaching(command: str) -> str | None:
    """The word being taught, if the command teaches a spelling/name."""
    m = _TEACH.search(command or "")
    if not m:
        return None
    word = m.group(1).strip(" .'")
    letters = word.replace(" ", "")
    # "D I S C O R D" or "DISCORD" → "Discord"
    if len(word) > 2 and (word.isupper() or re.fullmatch(r"(?:[A-Za-z]\s+){2,}[A-Za-z]", word)):
        word = letters.capitalize()
    return word or None
