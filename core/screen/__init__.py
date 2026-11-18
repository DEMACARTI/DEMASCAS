"""
DEMASCAS — core/screen
What's on the screen, as numbered elements the model can name:

    [12] checkbox "I agree to the terms" (unchecked)
    [13] button "Next"
    [14] textfield "Full name" = ""

Three sources, cheapest that works:
  * the browser page (Brave)      → core/screen/dom.py  (page structure)
  * any other app                 → core/screen/ax.py   (accessibility tree)
  * neither describes it          → core/screen/ocr.py  (Apple Vision text boxes)

The last observation is kept so actions (Phase 3) act on "[12]" — exactly
that element — never on pixels the model guessed.  Everything here READS.
"""

from __future__ import annotations

import difflib
import re
import threading
import time

BROWSERS = ("Brave Browser",)
_LAST: dict = {}
_LOCK = threading.Lock()


def _front_app() -> str:
    from core import axui
    try:
        return axui.app_on_screen() or axui.frontmost_app()
    except Exception:
        return axui.frontmost_app()


def observe(app: str = "") -> dict:
    """Look at the front window (or `app`'s) and remember what's there."""
    t0 = time.perf_counter()
    app = app or _front_app()
    if app in BROWSERS:
        from core.screen import dom
        obs = dom.observe()
        obs["source"], obs["app"] = "dom", app
    else:
        from core.screen import ax
        obs = ax.observe(app)
        obs["source"] = "ax"
        # nothing usable in the tree (canvas app, game, image) → read the pixels
        if obs.get("success") and len(obs.get("elements", [])) < 3:
            from core.screen import ocr
            o2 = ocr.screen_text_elements(_window_box(obs))
            if o2.get("success") and o2["elements"]:
                obs["elements"] = o2["elements"]
                obs["source"] = "ocr"
    obs["ms"] = round((time.perf_counter() - t0) * 1000)
    if obs.get("success"):
        obs["at"] = time.time()
        with _LOCK:
            _LAST.clear()
            _LAST.update(obs)
    return obs


def _window_box(obs: dict) -> list[int] | None:
    boxes = [e["box"] for e in obs.get("elements", []) if e.get("box")]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, max(1, x1 - x0), max(1, y1 - y0)] if x1 - x0 > 200 and y1 - y0 > 150 else None


def last() -> dict:
    with _LOCK:
        return dict(_LAST)


def element(eid: int) -> dict | None:
    for e in last().get("elements", []):
        if e.get("id") == eid:
            return e
    return None


def render(obs: dict, limit: int = 80) -> str:
    """The compact list the model reads (fenced as untrusted by the caller)."""
    if not obs.get("success"):
        return str(obs.get("message", "[-] I couldn't see the screen."))
    head = obs.get("title") or obs.get("window") or ""
    where = f"{obs.get('app', '')}" + (f" — {head}" if head else "") + (f" ({obs['url']})" if obs.get("url") else "")
    lines = [f"Screen: {where}"]
    if obs.get("dialog"):
        lines.append(f"DIALOG OPEN: {obs['dialog']}")
    group = None
    for e in obs.get("elements", [])[:limit]:
        g = e.get("group", "")
        if g and g != group:
            lines.append(f"  · {g}")
            group = g
        state = ""
        if "checked" in e:
            state = " (checked)" if e["checked"] else " (unchecked)"
        if e.get("role") == "password":
            state = " (password — never typed or read by DEMASCAS)"
        elif "value" in e:
            state += f' = "{e["value"][:60]}"'
        if e.get("options"):
            state += f" options: {', '.join(e['options'][:8])}"
        if e.get("disabled"):
            state += " (disabled)"
        if e.get("submits"):
            state += " (submits)"
        lines.append(f"[{e['id']}] {e['role']} \"{e['name']}\"{state}")
    more = len(obs.get("elements", [])) - limit
    if more > 0:
        lines.append(f"…and {more} more")
    if obs.get("texts"):
        lines.append("Text: " + " | ".join(obs["texts"][:15]))
    return "\n".join(lines)


def find(description: str, obs: dict | None = None, n: int = 3) -> list[dict]:
    """Elements whose label matches a description ("the I agree box", "next")."""
    obs = obs or last() or observe()
    q = re.sub(r"\b(the|a|an|button|box|checkbox|field|link|tab|that says|called|labelled|labeled)\b", " ", description.lower())
    q = " ".join(q.split())
    if not q:
        return []
    scored = []
    for e in obs.get("elements", []):
        name = e.get("name", "").lower()
        if not name:
            continue
        s = 1.0 if q == name else (0.9 if q in name or name in q else difflib.SequenceMatcher(None, q, name).ratio())
        if s >= 0.55:
            scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:n]]


# ── tools (READ tier) ────────────────────────────────────────────────

def screen_observe(app_name: str = "") -> dict:
    """Numbered list of what's on screen (buttons, fields, checkboxes…)."""
    obs = observe(app_name)
    if not obs.get("success"):
        return {"success": False, "message": obs.get("message", "[-] I couldn't see the screen.")}
    from core.screen import risk
    why = risk.blocked_reason({k: obs.get(k, "") for k in ("app", "url", "title", "window", "dialog")})
    note = f"[-] {why}\n" if why else ""
    return {"success": True, "source": obs["source"], "count": len(obs.get("elements", [])), "message": note + render(obs)}


def screen_find(description: str, app_name: str = "") -> dict:
    """Which element on screen matches a description — with its number."""
    obs = observe(app_name)
    if not obs.get("success"):
        return {"success": False, "message": obs.get("message", "[-] I couldn't see the screen.")}
    hits = find(description, obs)
    if not hits:
        return {"success": True, "found": False, "message": f"I don't see '{description}' on the screen."}
    return {"success": True, "found": True, "elements": hits,
            "message": "; ".join(f"[{e['id']}] {e['role']} \"{e['name']}\"" for e in hits)}


def form_read(app_name: str = "") -> dict:
    """The form on screen as questions: each question, its fields/options and
    what's filled in already (quizzes, Google Forms, sign-up pages)."""
    obs = observe(app_name)
    if not obs.get("success"):
        return {"success": False, "message": obs.get("message", "[-] I couldn't see the screen.")}
    qs: dict[str, list] = {}
    order: list[str] = []
    for e in obs.get("elements", []):
        if e["role"] not in ("checkbox", "radio", "textfield", "textarea", "select", "password"):
            continue
        g = e.get("group") or e.get("name") or "?"
        if g not in qs:
            qs[g] = []
            order.append(g)
        qs[g].append(e)
    submit = [e for e in obs.get("elements", []) if e.get("submits")]
    lines = []
    for i, g in enumerate(order, 1):
        parts = []
        for e in qs[g]:
            if e["role"] in ("checkbox", "radio"):
                parts.append(f"[{e['id']}] {'☑' if e.get('checked') else '☐'} {e['name']}")
            elif e["role"] == "password":
                parts.append(f"[{e['id']}] password field (not touched)")
            else:
                parts.append(f"[{e['id']}] {e['role']} \"{e['name']}\" = \"{e.get('value', '')}\"" + (" (required)" if e.get("required") else ""))
        lines.append(f"Q{i}. {g}\n    " + "\n    ".join(parts))
    if submit:
        lines.append("Submit: " + ", ".join(f"[{e['id']}] \"{e['name']}\"" for e in submit) + " (needs the user's spoken yes)")
    items = [{"question": g, "fields": [{k: e[k] for k in ("id", "role", "name", "checked", "value", "options", "required") if k in e}
                                        for e in qs[g] if e["role"] != "password"]} for g in order]
    return {"success": True, "questions": len(order), "items": items, "page": obs.get("title") or obs.get("window") or "",
            "submit": [{"id": e["id"], "name": e["name"]} for e in submit],
            "message": (f"Form on {obs.get('title') or obs.get('window') or obs.get('app')}: {len(order)} questions.\n"
                        + "\n".join(lines)) if order else "I don't see a form on the screen."}
