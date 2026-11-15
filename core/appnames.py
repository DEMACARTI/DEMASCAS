"""
DEMASCAS — core/appnames.py
Resolve a heard app name to an installed app: "cord" / "this cord" /
"discard" → "Discord".  Speech recognition mangles app names constantly; a
failed open used to make the model guess (it once called Terminal "cord").
"""

from __future__ import annotations

import difflib
import glob
import os
import re
import sys
import time

_APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
             os.path.expanduser("~/Applications")]
_cache: tuple[float, list[str]] = (0.0, [])


def installed_apps() -> list[str]:
    global _cache
    if time.time() - _cache[0] < 300:
        return _cache[1]
    names: list[str] = []
    if sys.platform == "darwin":
        for d in _APP_DIRS:
            names += [os.path.basename(p)[:-4] for p in glob.glob(os.path.join(d, "*.app"))]
    _cache = (time.time(), sorted(set(names)))
    return _cache[1]


_HELPER = re.compile(r"\b(handler|helper|uninstaller|updater|agent|launcher|installer)\b", re.I)


_running_cache: tuple[float, list[str]] = (0.0, [])


def running_apps() -> list[str]:
    """Names of running app processes (incl. Dock, Finder, SystemUIServer)."""
    global _running_cache
    if time.time() - _running_cache[0] < 10:
        return _running_cache[1]
    names: list[str] = []
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of '
                                  'every application process'], capture_output=True, text=True, timeout=3).stdout
            names = [n.strip() for n in out.split(",") if n.strip()]
        except Exception:
            names = []
    _running_cache = (time.time(), names)
    return names


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve(heard: str, cutoff: float = 0.72) -> tuple[str, float]:
    """(installed app name, confidence 0..1) or ('', 0)."""
    raw = re.sub(r"^(?:the|my|this|that|a)\s+|\s+app(?:lication)?$", "", (heard or "").strip(),
                 flags=re.I).strip(" .,'\"")
    if not raw:
        return "", 0.0
    k = _key(raw)
    if not k:
        return "", 0.0
    # A running process with exactly this name wins ("Dock" is running —
    # it used to fuzzy-match the installed "Docker" instead).
    for r in running_apps():
        if _key(r) == k:
            return r, 1.0
    apps = installed_apps()
    by_key = {_key(a): a for a in apps}
    if k in by_key:
        return by_key[k], 1.0
    # A whole word of a multi-word app name ("word" → "Microsoft Word");
    # prefer the shortest real app, never helper bundles.
    word_hits = [a for a in apps if len(k) >= 4 and k in {_key(w) for w in a.split()}
                 and not _HELPER.search(a)]
    if word_hits:
        return min(word_hits, key=len), 0.9
    # Suffix / prefix of a one-word name ("cord" → "Discord", "photo" → "Photos").
    for a in apps:
        ak = _key(a)
        if len(k) >= 4 and (ak.endswith(k) or ak.startswith(k)) and len(k) / len(ak) >= 0.5:
            return a, 0.75          # below the 0.8 most callers require
    # Spelling / vowel slips ("discard" → "Discord", "safary" → "Safari").
    # Same first letter required: "chrome" is 0.8 similar to "home".
    best = difflib.get_close_matches(k, [x for x in by_key if x[:1] == k[:1]], n=1, cutoff=cutoff)
    if best:
        score = difflib.SequenceMatcher(None, k, best[0]).ratio()
        return by_key[best[0]], score
    # "this cord" — the article may have been glued on by the recogniser.
    joined = _key(heard)
    if joined != k:
        best = difflib.get_close_matches(joined, [x for x in by_key if x[:1] == joined[:1]],
                                         n=1, cutoff=cutoff)
        if best:
            return by_key[best[0]], difflib.SequenceMatcher(None, joined, best[0]).ratio()
    return "", 0.0


def suggestions(heard: str, n: int = 3) -> list[str]:
    by_key = {_key(a): a for a in installed_apps()}
    return [by_key[k] for k in difflib.get_close_matches(_key(heard), list(by_key), n=n, cutoff=0.5)]
