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


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve(heard: str, cutoff: float = 0.72) -> tuple[str, float]:
    """(installed app name, confidence 0..1) or ('', 0)."""
    raw = re.sub(r"^(?:the|my|this|that|a)\s+|\s+app(?:lication)?$", "", (heard or "").strip(),
                 flags=re.I).strip(" .,'\"")
    if not raw:
        return "", 0.0
    apps = installed_apps()
    k = _key(raw)
    if not k:
        return "", 0.0
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
            return a, 0.8
    # Spelling / vowel slips ("discard" → "Discord", "safary" → "Safari").
    best = difflib.get_close_matches(k, list(by_key), n=1, cutoff=cutoff)
    if best:
        score = difflib.SequenceMatcher(None, k, best[0]).ratio()
        return by_key[best[0]], score
    # "this cord" — the article may have been glued on by the recogniser.
    joined = _key(heard)
    if joined != k:
        best = difflib.get_close_matches(joined, list(by_key), n=1, cutoff=cutoff)
        if best:
            return by_key[best[0]], difflib.SequenceMatcher(None, joined, best[0]).ratio()
    return "", 0.0


def suggestions(heard: str, n: int = 3) -> list[str]:
    by_key = {_key(a): a for a in installed_apps()}
    return [by_key[k] for k in difflib.get_close_matches(_key(heard), list(by_key), n=n, cutoff=0.5)]
