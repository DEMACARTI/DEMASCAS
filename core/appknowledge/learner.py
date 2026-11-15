"""
DEMASCAS — core/appknowledge/learner.py
"Learn <app>" — build a manual for an app DEMASCAS has never used, the way a
person picks up new software:

  1. what the app itself declares   — scripting dictionary (dictionary.py),
                                       URL schemes and file types (Info.plist)
  2. its menu bar                    — every menu command + keyboard shortcut,
                                       read WITHOUT clicking (app must be open)
  3. its official help pages         — keyboard-shortcut lists parsed exactly;
                                       how-to prose summarised by the LLM, and
                                       every summary checked against the page
  4. experience                      — manual entries used in tasks that
                                       worked are marked verified (note_success)

Nothing here is app-specific.  The manual lives in
~/.demascas/appknowledge/<app>.manual.json and agents get the entries
relevant to their goal (hints()).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import logging

import numpy as np

logging.getLogger("primp").setLevel(logging.WARNING)      # per-request INFO spam

from core.appknowledge.dictionary import CACHE_DIR, _slug, find_app, load as load_dictionary

_lock = threading.Lock()
_mem: dict[str, dict] = {}      # app → {"mtime", "entries", "matrix"}

# ── manual storage ───────────────────────────────────────────────────


def manual_path(app: str) -> str:
    return os.path.join(CACHE_DIR, _slug(app) + ".manual.json")


def load_manual(app: str) -> dict:
    try:
        with open(manual_path(app)) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"app": app, "entries": [], "sources": [], "learned_at": 0}


def save_manual(app: str, manual: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = manual_path(app) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manual, f, indent=1)
    os.replace(tmp, manual_path(app))


def _merge(manual: dict, new: list[dict]) -> int:
    """Add entries, de-duplicated by task text.  Returns how many were new."""
    have = {e["task"].lower() for e in manual["entries"]}
    added = 0
    for e in new:
        key = e["task"].lower()
        if key and key not in have:
            e.setdefault("verified", 0)
            e.setdefault("failed", 0)
            manual["entries"].append(e)
            have.add(key)
            added += 1
    return added


# ── 1. what the app declares ─────────────────────────────────────────

def bundle_facts(app: str) -> list[dict]:
    path = find_app(app)
    if not path:
        return []
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except Exception:
        return []
    out = []
    for t in info.get("CFBundleURLTypes") or []:
        for scheme in t.get("CFBundleURLSchemes") or []:
            out.append({"task": f"open {app} pages or actions by link",
                        "how": f"open_url with a {scheme}:// link", "kind": "deeplink",
                        "source": "Info.plist"})
    exts = []
    for t in info.get("CFBundleDocumentTypes") or []:
        exts += [e for e in (t.get("CFBundleTypeExtensions") or []) if e != "*"]
    if exts:
        out.append({"task": f"open files in {app}",
                    "how": "open_file with a ." + ", .".join(sorted(set(exts))[:12]) + " file",
                    "kind": "files", "source": "Info.plist"})
    return out


# ── 2. the menu bar (read-only) ──────────────────────────────────────

_MENU_SCRIPT = r'''
on run argv
  set appName to item 1 of argv
  set out to ""
  tell application "System Events"
    if not (exists process appName) then return "NOT_RUNNING"
    tell process appName
      repeat with mbi in menu bar items of menu bar 1
        set top to name of mbi
        if top is not "Apple" then
          try
            repeat with mi in menu items of menu 1 of mbi
              set n to name of mi
              if n is not missing value then
                set k to ""
                set m to ""
                try
                  set k to value of attribute "AXMenuItemCmdChar" of mi
                end try
                try
                  set m to value of attribute "AXMenuItemCmdModifiers" of mi
                end try
                if k is missing value then set k to ""
                if m is missing value then set m to ""
                set out to out & top & tab & n & tab & k & tab & m & linefeed
              end if
            end repeat
          end try
        end if
      end repeat
    end tell
  end tell
  return out
end run
'''

# AXMenuItemCmdModifiers bitmask: 1 shift, 2 option, 4 control, 8 = NO command key.
def _menu_keys(char: str, mods: str) -> str:
    if not char or not char.strip():
        return ""
    try:
        m = int(mods or 0)
    except ValueError:
        m = 0
    parts = []
    if not m & 8:
        parts.append("cmd")
    if m & 4:
        parts.append("ctrl")
    if m & 2:
        parts.append("option")
    if m & 1:
        parts.append("shift")
    special = {"\x1b": "escape", "\r": "return", "\t": "tab", " ": "space",
               "\x7f": "delete", "\x08": "delete"}
    key = special.get(char, char.strip().lower())
    if not key or not key.isprintable():
        return ""                    # glyph-only key we can't name reliably
    return "+".join(parts + [key])


def read_menus(app: str, timeout: float = 8.0) -> list[dict] | None:
    """Menu commands + shortcuts of a RUNNING app, without clicking anything.
    None if the app isn't running (or this isn't macOS)."""
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(["osascript", "-e", _MENU_SCRIPT, app], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    if p.returncode != 0 or p.stdout.strip() == "NOT_RUNNING":
        return None
    out = []
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[1].strip():
            continue
        top, item = parts[0].strip(), parts[1].strip()
        keys = _menu_keys(parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
        out.append({"task": item, "how": f"click_menu_item {top} > {item}"
                    + (f" (or press {keys})" if keys else ""),
                    "kind": "menu", "menu": f"{top} > {item}", "keys": keys, "source": "menu bar"})
    return out


# ── 3. official help pages ───────────────────────────────────────────

_KEY_TOKEN = (r"(?:cmd|command|ctrl|control|alt|option|opt|shift|fn|esc|escape|enter|return|"
              r"tab|space|spacebar|delete|backspace|home|end|page ?up|page ?down|"
              r"(?:arrow key )?(?:up|down|left|right)(?: arrow(?: key)?)?|f\d{1,2}|[a-z0-9]|[\[\]\\/.,;'`=-])")
_COMBO = re.compile(rf"^\s*{_KEY_TOKEN}(?:\s*\+\s*{_KEY_TOKEN})+(?:\s*(?:&|or|,|/|\|)\s*{_KEY_TOKEN}(?:\s*\+\s*{_KEY_TOKEN})*)*\s*$",
                    re.IGNORECASE)
_OS_TAG = re.compile(r"\((?:windows|win|mac|macos|linux|pc)\)", re.IGNORECASE)
_GLYPHS = {"⌘": "cmd", "⇧": "shift", "⌥": "option", "⌃": "ctrl", "↑": "up", "↓": "down",
           "←": "left", "→": "right", "⏎": "return", "↩": "return", "⎋": "escape", "⌫": "delete",
           "⇥": "tab", "␣": "space"}


def _canon_combo(raw: str) -> str:
    """'⇧⌘M' / '⌘ K' / 'Ctrl/Cmd + K' → '+'-separated text the parser understands."""
    s = (raw or "").strip()
    if any(g in s for g in _GLYPHS):
        toks: list[str] = []
        for ch in s:
            if ch in _GLYPHS:
                toks.append(_GLYPHS[ch])
            elif ch.strip() and ch != "+":
                if toks and toks[-1] not in _GLYPHS.values():
                    toks[-1] += ch                 # multi-char key name, e.g. "F5"
                else:
                    toks.append(ch)
        s = " + ".join(toks)
    # "Ctrl/Cmd + K" → this OS's modifier
    mod = "cmd" if sys.platform == "darwin" else "ctrl"
    s = re.sub(r"\b(?:ctrl|control)\s*/\s*(?:cmd|command)\b", mod, s, flags=re.I)
    s = re.sub(r"\b(?:cmd|command)\s*/\s*(?:ctrl|control)\b", mod, s, flags=re.I)
    return _OS_TAG.sub("", s).strip()


def _is_combo(text: str) -> bool:
    return bool(_COMBO.match(_canon_combo(text)))


def _normalize_keys(combo: str) -> str:
    """'Control + ALT + Arrow Key UP & ...' → 'ctrl+option+up'.  With several
    alternatives ('Ctrl + / (Windows) or Cmd + / (Mac)') pick this OS's."""
    canon = _canon_combo(combo)
    if sys.platform == "darwin":
        # "Ctrl + K (Cmd + K on Mac)" — a Mac combo anywhere in the cell wins.
        m = re.search(r"\b(?:cmd|command)\s*\+[\w\s+/]*?(?=\s*(?:\(|\)|&|\bor\b|,|\||\bon\b|$))", canon, re.I)
        if m and re.search(r"\b(?:ctrl|control)\b", canon, re.I):
            canon = m.group(0)
    alts = [a for a in re.split(r"\s*(?:&|\bor\b|,|\|)\s*", canon, flags=re.I) if a.strip()]
    first = alts[0] if alts else canon
    if sys.platform == "darwin":
        first = next((a for a in alts if re.search(r"\b(cmd|command|option)\b", a, re.I)), first)
    # "Option + Shift + Up/Down" → the first direction
    first = re.sub(r"\b(page ?up|page ?down|up|down|left|right)\s*/\s*\w+", r"\1", first, flags=re.I)
    names = {"cmd": "cmd", "command": "cmd", "ctrl": "ctrl", "control": "ctrl",
             "alt": "option", "option": "option", "opt": "option", "shift": "shift",
             "esc": "escape", "escape": "escape", "enter": "return", "return": "return",
             "spacebar": "space", "page up": "pageup", "page down": "pagedown"}
    out = []
    for tok in first.split("+"):
        t = re.sub(r"\barrow( key)?\b|\bkey\b", "", tok.strip().lower()).strip()
        t = names.get(t, t)
        if t:
            out.append(t)
    return "+".join(out)


_MODS = {"cmd", "ctrl", "option", "shift", "fn"}


def _pair(task: str, combo: str) -> dict | None:
    keys = _normalize_keys(combo)
    if sum(1 for k in keys.split("+") if k not in _MODS) != 1:
        return None                      # "ctrl+up+e", "option+up+/+down" — not one combo
    return {"task": task[:80].strip().rstrip(":"), "how": f"press_keyboard_shortcut {keys}",
            "kind": "shortcut", "keys": keys, "source": "docs"}


def shortcut_pairs(text: str) -> list[dict]:
    """Exact extraction of 'task' / 'shortcut' pairs from a help page: a
    line that is only a key combo belongs to the line before it, and
    'Task: combo' / 'Task — combo' lines are split."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i, ln in enumerate(lines):
        if _is_combo(ln) and i > 0 and not _is_combo(lines[i - 1]) and len(lines[i - 1]) < 90:
            out.append(_pair(lines[i - 1], ln))
            continue
        m = re.match(r"^(.{3,80}?)\s*(?::|—|–|-|\t)\s*(.+)$", ln)
        if m and _is_combo(m.group(2)):
            out.append(_pair(m.group(1), m.group(2)))
    return [e for e in out if e]


def _fetch_page(url: str, timeout: float = 10.0) -> tuple[str, list]:
    """(main text, [(header cells, [row cells, ...]), ...]) — via the shared
    polite fetcher (robots.txt, per-site delay, size cap)."""
    from core.web.fetch import fetch
    page = fetch(url, timeout=timeout)
    if page.ok:
        try:   # app docs are web knowledge too
            from core.web.store import add_page
            add_page(url, page.title, page.text, page.tables, topic=page.title)
        except Exception:
            pass
    return (page.text, page.tables) if page.ok else ("", [])


def _fetch_text(url: str, timeout: float = 10.0) -> str:
    return _fetch_page(url, timeout)[0]


_HEADER_WORDS = {"what it does", "action", "function", "shortcut", "shortcuts", "description",
                 "command", "keys", "windows", "mac", "macos", "task"}


def table_pairs(rows: list[list[str]], header: list[str] | None = None) -> list[dict]:
    """Rows of one table.  If the header names OS columns (Windows | Mac),
    take this OS's column."""
    mac = sys.platform == "darwin"
    want = None
    for i, c in enumerate(x.lower() for x in (header or [])):
        if (mac and re.search(r"mac|⌘", c)) or (not mac and re.search(r"windows|linux|\bpc\b", c)):
            want = i
            break
    out = []
    for cells in rows:
        if want is not None and want < len(cells) and _is_combo(cells[want]):
            combo = cells[want]
            task = next((c for i, c in enumerate(cells) if i != want and c and not _is_combo(c)), "")
        else:
            combos = [c for c in cells if _is_combo(c)]
            texts = [c for c in cells if c and not _is_combo(c)]
            if not combos or not texts:
                continue
            combo, task = combos[0], texts[0]
        if task and task.lower() not in _HEADER_WORDS:
            e = _pair(task, combo)
            if e:
                out.append(e)
    return out


def _platform_page(url: str, text: str) -> str:
    """'mac' | 'windows' | 'linux' | '' — which platform a page is about."""
    # Trust the URL (".../macos/discord", "?os=macos"): page text that merely
    # mentions "Mac" often still lists Windows keys.
    u = url.lower()
    hits = [plat for plat, pat in (("mac", r"mac(os)?\b|osx"), ("windows", r"windows"),
                                   ("linux", r"linux")) if re.search(pat, u)]
    return hits[0] if len(hits) == 1 else ""      # "windows-mac-and-web" = generic


def _keep_for_this_os(pairs: list[dict], page_platform: str) -> list[dict]:
    """On a Mac, Ctrl-only combos from non-Mac pages are Windows keys — drop them."""
    if sys.platform != "darwin" or page_platform == "mac":
        return pairs
    return [e for e in pairs if not (re.search(r"\bctrl\b", e["keys"]) and "cmd" not in e["keys"])]


def _official(url: str, app: str) -> bool:
    host = urlparse(url).netloc.lower()
    slug = re.sub(r"[^a-z0-9]", "", app.lower())
    return bool(slug) and slug in host.replace("-", "").replace(".", "")


def find_docs(app: str, max_pages: int = 6) -> list[str]:
    """Official help pages first (support.<app>.com, help.<app>.com, …)."""
    from ddgs import DDGS
    plat = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
    queries = [f"{app} keyboard shortcuts {plat}", f"{app} hotkeys {plat}",
               f"{app} help center how to use features"]
    # The three searches run in PARALLEL ("auto" falls through rate-limited
    # engines by itself; measured 6.8 s sequential → ~2.5 s).
    from concurrent.futures import ThreadPoolExecutor

    def _search(q: str) -> list[str]:
        try:
            with DDGS(timeout=6) as d:
                return [r.get("href", "") for r in d.text(q, max_results=6)]
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(_search, queries))
    urls: list[str] = []
    for hrefs in results:
        for u in hrefs:
            if u and u not in urls and "youtube.com" not in u:
                urls.append(u)
    if not urls:
        return []
    def rank(u: str) -> tuple:
        on_platform = bool(re.search(rf"\b{plat}(os)?\b", u.lower()))
        return (not _official(u, app), not on_platform)
    return sorted(urls, key=rank)[:max_pages]


_HOWTO_PROMPT = """From this help text about the app "{app}", list concrete things a user can do and exactly how.
Only use facts stated in the text. Skip anything about installing, accounts, billing or troubleshooting.
Text:
\"\"\"{chunk}\"\"\"
Return JSON: {{"entries": [{{"task": "short task", "how": "exact steps from the text"}}]}} (at most 6)"""


def _grounded(how: str, chunk: str) -> bool:
    words = [w for w in re.findall(r"[a-z]{4,}", how.lower())]
    if not words:
        return False
    low = chunk.lower()
    return sum(w in low for w in words) / len(words) >= 0.6     # no invented steps


_JUNK = re.compile(r"password|passcode|\bcode\b|2fa|two-factor|account|log ?in|sign ?(in|up)|"
                   r"install|download|billing|payment|subscri|verify|verification|email", re.I)


def howto_entries(app: str, text: str, max_chunks: int = 2) -> list[dict]:
    from actuators.documents import _llm_json
    paras = [p for p in text.split("\n") if len(p) > 60]
    chunk, chunks = "", []
    for p in paras:
        if len(chunk) + len(p) > 2200:
            chunks.append(chunk)
            chunk = ""
        chunk += p + "\n"
    if chunk:
        chunks.append(chunk)
    out = []
    for c in chunks[:max_chunks]:
        try:
            data = _llm_json(_HOWTO_PROMPT.format(app=app, chunk=c), max_tokens=450) or {}
        except RuntimeError:
            break
        for e in data.get("entries") or []:
            if isinstance(e, dict) and e.get("task") and e.get("how") and _grounded(str(e["how"]), c) \
                    and not _JUNK.search(str(e["task"]) + " " + str(e["how"])):
                out.append({"task": str(e["task"])[:80], "how": str(e["how"])[:200],
                            "kind": "howto", "source": "docs"})
    return out


# ── the "learn <app>" entry point ────────────────────────────────────

RELEARN_AFTER_DAYS = 14


def learn_app(app_name: str, use_docs: bool = True, force: bool = False) -> dict:
    """Study an app: what it declares, its menus (if it's open) and its
    official help.  Safe: reads only — never clicks or types in the app.
    Already learned (< 14 days)? Answers instantly unless force=True."""
    t0 = time.time()
    app = _resolve_app_name(app_name)
    if not app:
        return {"success": False, "message": f"[-] I can't find an app called {app_name}."}
    manual = load_manual(app)
    age_days = (time.time() - manual.get("learned_at", 0)) / 86400
    if not force and manual["entries"] and age_days < RELEARN_AFTER_DAYS:
        refresh_menus_async(app)          # still pick up menus if it's open now
        return {"success": True, "app": app, "cached": True, "total": len(manual["entries"]),
                "message": f"I already know {app} — {len(manual['entries'])} things I can do in it. "
                           f"Say 'learn {app} again' to refresh.",
                "seconds": round(time.time() - t0, 3)}
    found: dict[str, int] = {}

    n_dict = len(load_dictionary(app))
    found["scripting commands"] = n_dict
    found["link/file facts"] = _merge(manual, bundle_facts(app))

    # Menus (AppleScript, ~2 s) and the web search run at the same time.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_menus = pool.submit(read_menus, app)
        f_urls = pool.submit(find_docs, app) if use_docs else None
        menus = f_menus.result()
        urls = f_urls.result() if f_urls else []
    running = menus is not None
    found["menu commands"] = _merge(manual, menus or [])
    if running and menus:
        manual["has_menus"] = True

    pages = []
    if use_docs:
        # Fetch the pages in parallel (was one after another).

        def _get(u):
            try:
                return u, _fetch_page(u)
            except Exception:
                return u, None
        with ThreadPoolExecutor(max_workers=4) as pool:
            fetched = list(pool.map(_get, urls))
        for url, page in fetched:
            if page is None:
                continue
            text, tables = page
            if len(text) < 300 and not tables:
                continue
            from_tables = [e for header, rows in tables for e in table_pairs(rows, header)]
            pairs = _keep_for_this_os(from_tables + shortcut_pairs(text),
                                      _platform_page(url, text))
            for e in pairs:
                e["url"] = url
            got = _merge(manual, pairs)
            if _official(url, app) and len(pairs) < 5:
                how = howto_entries(app, text)
                for e in how:
                    e["url"] = url
                got += _merge(manual, how)
            if got:
                pages.append(url)
            found["from help pages"] = found.get("from help pages", 0) + got

    # Cross-source agreement: a shortcut listed by several independent pages
    # is more trustworthy than one sloppy table row.
    by_keys: dict[str, set] = {}
    for e in manual["entries"]:
        if e.get("keys") and e.get("url"):
            by_keys.setdefault(e["keys"], set()).add(urlparse(e["url"]).netloc)
    for e in manual["entries"]:
        if e.get("keys"):
            e["agree"] = len(by_keys.get(e["keys"], ())) or (1 if e.get("source") == "menu bar" else 0)
    manual["sources"] = sorted(set(manual.get("sources", [])) | set(pages))
    manual["learned_at"] = time.time()
    manual["has_menus"] = bool(manual.get("has_menus")) or (running and bool(menus))
    save_manual(app, manual)
    with _lock:
        _mem.pop(app, None)

    total = len(manual["entries"])
    examples = [e for e in manual["entries"] if e["kind"] in ("shortcut", "menu", "howto")][:3]
    msg = f"I studied {app}: I now know {total} things I can do in it"
    if n_dict:
        msg += f", plus {n_dict} scripting commands"
    msg += "."
    if not running:
        msg += f" Open {app} and ask me to learn it again, and I'll read its menus too."
    return {"success": True, "message": msg, "app": app, "found": found, "total": total,
            "sources": pages, "examples": [f"{e['task']}: {e['how']}" for e in examples],
            "seconds": round(time.time() - t0, 1)}


def _resolve_app_name(name: str) -> str:
    """'discord so you can use it' → 'Discord': the longest leading words
    that name an installed app."""
    try:
        from core.appnames import resolve
        hit, conf = resolve(name)
        if hit and conf >= 0.8:
            return hit
    except Exception:
        pass
    words = re.sub(r"^(the|my)\s+", "", (name or "").strip(), flags=re.I).strip(" .").split()
    for n in range(min(len(words), 4), 0, -1):
        found = _resolve_one(" ".join(words[:n]))
        if found:
            return found
    return ""


def _resolve_one(name: str) -> str:
    name = re.sub(r"\s+app$", "", name, flags=re.I).strip(" .")
    if not name:
        return ""
    path = find_app(name)
    if path:
        # macOS paths are case-insensitive: report the app's real name.
        parent, base = os.path.split(path)
        real = next((f for f in os.listdir(parent) if f.lower() == base.lower()), base)
        return real[:-4]
    if sys.platform != "darwin":
        return name          # docs-only learning elsewhere for now
    import glob
    from core.appknowledge.dictionary import _APP_DIRS
    low = name.lower()
    for d in _APP_DIRS:
        for p in glob.glob(os.path.join(d, "*.app")):
            base = os.path.basename(p)[:-4]
            if re.search(rf"\b{re.escape(low)}\b", base.lower()):
                return base
    return ""


# ── using what was learned ───────────────────────────────────────────

def _index(app: str):
    from core.embeddings import embed
    path = manual_path(app)
    mtime = os.path.getmtime(path) if os.path.exists(path) else 0
    with _lock:
        slot = _mem.get(app)
        if slot and slot["mtime"] == mtime:
            return slot
        entries = load_manual(app)["entries"]
        matrix = (np.array(embed([f"{e['task']}: {e['how']}" for e in entries]), dtype=np.float32)
                  if entries else None)
        slot = _mem[app] = {"mtime": mtime, "entries": entries, "matrix": matrix}
        return slot


def hints(app: str, goal: str, k: int = 6, max_chars: int = 700) -> str:
    """Manual entries relevant to a goal, verified ones first."""
    if not os.path.exists(manual_path(app)):
        return ""
    from core.embeddings import embed
    slot = _index(app)
    if slot["matrix"] is None:
        return ""
    q = np.array(embed([goal])[0], dtype=np.float32)
    bonus = np.array([0.05 * min(e.get("verified", 0), 3) - 0.1 * min(e.get("failed", 0), 3)
                      + 0.03 * min(max(e.get("agree", 1) - 1, 0), 2)
                      + (0.05 if e.get("source") == "menu bar" else 0.0)   # read from the app itself
                      for e in slot["entries"]], dtype=np.float32)
    lines, used = [], 0
    for i in np.argsort(-(slot["matrix"] @ q + bonus))[:k]:
        e = slot["entries"][int(i)]
        ln = f"- {e['task']}: {e['how']}" + (" ✓" if e.get("verified") else "")
        if used + len(ln) > max_chars:
            break
        lines.append(ln)
        used += len(ln)
    return "\n".join(lines)


def known_apps() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    out = []
    for fn in os.listdir(CACHE_DIR):
        if fn.endswith(".manual.json"):
            try:
                with open(os.path.join(CACHE_DIR, fn)) as f:
                    out.append(json.load(f)["app"])
            except Exception:
                pass
    return out


def apps_in_text(text: str) -> list[str]:
    low = (text or "").lower()
    return [a for a in known_apps() if re.search(rf"\b{re.escape(a.lower())}\b", low)]


def note_outcome(app: str, tool_calls: list[dict], success: bool) -> int:
    """Experience: entries whose shortcut / menu was used in a task get
    verified (or failed) counts — the manual improves with use."""
    if not app or not os.path.exists(manual_path(app)):
        return 0
    manual = load_manual(app)
    used_keys = {str((c.get("args") or {}).get("keys", "")).lower() for c in tool_calls
                 if c.get("tool") == "press_keyboard_shortcut"}
    used_menus = {str((c.get("args") or {}).get("menu_path", "")).lower() for c in tool_calls
                  if c.get("tool") == "click_menu_item"}
    n = 0
    for e in manual["entries"]:
        if (e.get("keys") and e["keys"].lower() in used_keys) or \
                (e.get("menu") and e["menu"].lower() in used_menus):
            e["verified" if success else "failed"] = e.get("verified" if success else "failed", 0) + 1
            n += 1
    if n:
        save_manual(app, manual)
    return n


_refreshing: set[str] = set()


def refresh_menus_async(app: str) -> None:
    """If a learned app is open but its menus were never read, read them in
    the background (≈7 s of AppleScript — never on the user's critical path)."""
    if not app or app in _refreshing or not os.path.exists(manual_path(app)):
        return
    if load_manual(app).get("has_menus"):
        return
    _refreshing.add(app)

    def _work():
        try:
            menus = read_menus(app)
            if menus:
                manual = load_manual(app)
                added = _merge(manual, menus)
                manual["has_menus"] = True
                save_manual(app, manual)
                print(f"[APPS] Read {app}'s menus: {added} new commands.")
        finally:
            _refreshing.discard(app)

    threading.Thread(target=_work, name=f"menus-{app}", daemon=True).start()


# ── "Help me use Discord for studying" ───────────────────────────────

_GUIDE_PROMPT = """The user wants to use the app "{app}"{goal_part}.
Give 3 short, practical, spoken tips focused on THAT goal, from what {app} is generally good for
(e.g. for studying with Discord: study servers, voice channels, screen sharing, muting distractions).
Only if one of these learned shortcuts directly helps the goal, mention it:
{hints}
Never suggest settings, updates, help pages or news. No markdown or lists — 3 sentences.
Return JSON: {{"say": "..."}}"""


def guide_app(app_name: str, goal: str = "") -> dict:
    """Open an app and coach the user for a goal using the learned manual
    (learning the app first if DEMASCAS has never seen it)."""
    app = _resolve_app_name(app_name)
    if not app:
        return {"success": False, "message": f"[-] I can't find an app called {app_name}."}
    try:
        from actuators.system_control import open_application
        open_application(app)
    except Exception:
        pass
    learned = learn_app(app)                       # instant if already known
    hint = hints(app, f"{goal} {app}".strip(), k=8, max_chars=900) if learned.get("success") else ""
    if not hint:
        return {"success": True, "message": f"{app} is open. I don't know much about it yet — "
                                            f"tell me what you'd like to do and I'll try."}
    try:
        from actuators.documents import _llm_json
        data = _llm_json(_GUIDE_PROMPT.format(app=app, goal_part=f" for {goal}" if goal else "",
                                              hints=hint), max_tokens=220) or {}
        say = str(data.get("say", "")).strip()
    except RuntimeError:
        say = ""
    if not say:
        top = [ln.lstrip("- ").split(":")[0] for ln in hint.splitlines()[:3]]
        say = f"{app} is open. Useful things I can do there: " + ", ".join(top) + "."
    return {"success": True, "message": say, "app": app}
