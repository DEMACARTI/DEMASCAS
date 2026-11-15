"""
DEMASCAS — core/appknowledge/dictionary.py
"Import how an app works" from the app itself.

Scriptable macOS apps ship a scripting dictionary (sdef) describing every
command and object they support — PowerPoint: 134 commands / 121 classes.
DEMASCAS reads it once (~50 ms), caches it, and when an agent works inside
that app it gets the handful of capabilities relevant to the task (ranked
with the cached embedder, ~2 ms) instead of guessing at menus.

Windows (COM type libraries) and Linux (D-Bus) are future work: on other
platforms this returns nothing and agents fall back to UI automation.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET

import numpy as np

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".demascas", "appknowledge")
_APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
             os.path.expanduser("~/Applications")]
_lock = threading.Lock()
_mem: dict[str, dict] = {}          # app → {"entries": [...], "matrix": np.ndarray | None}


def find_app(app: str) -> str:
    if sys.platform != "darwin" or not app:
        return ""
    name = app if app.endswith(".app") else app + ".app"
    for d in _APP_DIRS:
        p = os.path.join(d, name)
        if os.path.isdir(p):
            return p
    low = name.lower()
    for d in _APP_DIRS:
        for p in glob.glob(os.path.join(d, "*.app")):
            if os.path.basename(p).lower() == low:
                return p
    return ""


def _slug(app: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", app.lower()).strip("_")


def _parse(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    for cmd in root.iter("command"):
        params = [p.get("name") for p in cmd.findall("parameter") if p.get("name")]
        direct = cmd.find("direct-parameter")
        entries.append({
            "kind": "command", "name": cmd.get("name", ""),
            "text": (cmd.get("description") or "").strip(),
            "params": params, "direct": direct.get("type", "") if direct is not None else "",
        })
    for cls in root.iter("class"):
        props = [p.get("name") for p in cls.findall("property") if p.get("name")]
        elems = [e.get("type") for e in cls.findall("element") if e.get("type")]
        entries.append({
            "kind": "class", "name": cls.get("name", ""),
            "text": (cls.get("description") or "").strip(),
            "props": props[:25], "elements": elems[:12],
        })
    entries = [e for e in entries if e["name"]]
    if entries and not any(e["kind"] == "command" and e["name"] == "make" for e in entries):
        entries.extend(_STANDARD)     # Standard Suite pulled in by reference (xi:include)
    return entries


# AppleScript's standard verbs — most real work is these applied to the
# app's own objects ("make new slide at end of active presentation").
_STANDARD = [
    {"kind": "command", "name": "make", "direct": "", "params": ["new", "at", "with properties"],
     "text": "Create a new object, e.g. make new <class> at end of <container> with properties {...}"},
    {"kind": "command", "name": "set", "direct": "", "params": ["to"],
     "text": "Change a property, e.g. set <property> of <object> to <value>"},
    {"kind": "command", "name": "get", "direct": "", "params": [],
     "text": "Read a property or object, e.g. get name of <object>"},
    {"kind": "command", "name": "count", "direct": "", "params": [],
     "text": "Count objects, e.g. count slides of active presentation"},
    {"kind": "command", "name": "delete", "direct": "", "params": [],
     "text": "Delete an object (asks the user first)"},
    {"kind": "command", "name": "open", "direct": "file", "params": [], "text": "Open a document file"},
    {"kind": "command", "name": "save", "direct": "", "params": ["in", "as"],
     "text": "Save a document (asks the user first)"},
]


def load(app: str) -> list[dict]:
    """Dictionary entries for an app ([] if not scriptable / not macOS)."""
    path = find_app(app)
    if not path:
        return []
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, _slug(app) + ".json")
    mtime = os.path.getmtime(path)
    try:
        with open(cache) as f:
            data = json.load(f)
        if data.get("mtime") == mtime:
            return data["entries"]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    try:
        xml_text = subprocess.run(["sdef", path], capture_output=True, text=True,
                                  timeout=10).stdout
        entries = _parse(xml_text) if xml_text.strip() else []
    except Exception:
        entries = []
    with open(cache, "w") as f:
        json.dump({"app": app, "path": path, "mtime": mtime, "entries": entries}, f)
    return entries


def _line(e: dict) -> str:
    if e["kind"] == "command":
        args = ", ".join(e.get("params") or [])
        target = f" <{e['direct']}>" if e.get("direct") else ""
        return f"command `{e['name']}{target}`{' (' + args + ')' if args else ''}: {e['text'][:90]}"
    props = ", ".join((e.get("props") or [])[:8])
    elems = ", ".join((e.get("elements") or [])[:5])
    return (f"object `{e['name']}`: {e['text'][:70]}"
            + (f" | properties: {props}" if props else "")
            + (f" | contains: {elems}" if elems else ""))


def _embed_text(e: dict) -> str:
    extra = " ".join((e.get("props") or [])[:10] + (e.get("params") or []))
    return f"{e['kind']} {e['name']}: {e['text']} {extra}".strip()


def _matrix(app: str, entries: list[dict]) -> np.ndarray:
    """Entry embeddings, cached on disk (1.5 s to compute for PowerPoint)."""
    from core.embeddings import embed
    path = os.path.join(CACHE_DIR, _slug(app) + ".npy")
    try:
        m = np.load(path)
        if m.shape[0] == len(entries):
            return m
    except (FileNotFoundError, ValueError, OSError):
        pass
    m = np.array(embed([_embed_text(e) for e in entries]), dtype=np.float32)
    try:
        np.save(path, m)
    except OSError:
        pass
    return m


def capabilities(app: str, query: str, k: int = 8, max_chars: int = 1100) -> str:
    """Top-k dictionary entries relevant to `query`, as a compact prompt block.
    Score = embedding similarity + a bonus when the entry's name appears in
    the request ("slide", "font", "make") — many entries have no description,
    so names carry most of the signal."""
    from core.embeddings import embed
    with _lock:
        slot = _mem.get(app)
        if slot is None:
            entries = load(app)
            slot = _mem[app] = {"entries": entries,
                                "matrix": _matrix(app, entries) if entries else None}
    if slot["matrix"] is None:
        return ""
    q = np.array(embed([query])[0], dtype=np.float32)
    words = set(re.findall(r"[a-z]+", query.lower()))
    words |= {w[:-1] for w in words if w.endswith("s")}
    verbs = {"add": "make", "create": "make", "new": "make", "insert": "make", "change": "set",
             "rename": "set", "remove": "delete", "how": "count", "number": "count"}
    words |= {verbs[w] for w in words if w in verbs}
    def _bonus(e: dict) -> float:
        name = e["name"].split()
        covered = len(set(name) & words) / len(name)     # "make" 1.0, "slide show window" 0.33
        return 0.3 * covered + 0.05 * len(set(e.get("props") or []) & words)
    bonus = np.array([_bonus(e) for e in slot["entries"]], dtype=np.float32)
    order = np.argsort(-(slot["matrix"] @ q + bonus))[:k]
    lines, used = [], 0
    for i in order:
        ln = "- " + _line(slot["entries"][int(i)])[:150]
        if used + len(ln) > max_chars:
            break
        lines.append(ln)
        used += len(ln)
    return "\n".join(lines)
