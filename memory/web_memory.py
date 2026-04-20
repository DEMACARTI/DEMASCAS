"""
DEMASCAS — memory/web_memory.py
Per-domain persistent web memory.  Stores known elements, page flows,
site type, DOM fingerprints, and learned selectors for each domain.

Backed by a simple JSON file at ~/.demascas/web_memory.json.
No new pip dependencies.
"""

import json
import os
import time
import pathlib
from typing import Optional

_DIR = pathlib.Path.home() / ".demascas"
_MEM_PATH = _DIR / "web_memory.json"

# ── Internal state ───────────────────────────────────────────────────

_cache: dict | None = None           # lazily loaded


def _ensure_dir():
    _DIR.mkdir(parents=True, exist_ok=True)


def _load() -> dict:
    """Load the web memory JSON file (or return empty dict)."""
    global _cache
    if _cache is not None:
        return _cache
    _ensure_dir()
    if _MEM_PATH.exists():
        try:
            _cache = json.loads(_MEM_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save():
    """Write current cache to disk."""
    _ensure_dir()
    _MEM_PATH.write_text(json.dumps(_cache or {}, indent=2))


def _domain_key(url: str) -> str:
    """Extract the domain portion from a URL for use as memory key."""
    url = url.strip()
    if "://" in url:
        url = url.split("://", 1)[1]
    url = url.split("/")[0].split("?")[0].split("#")[0]
    # Strip www.
    if url.startswith("www."):
        url = url[4:]
    return url.lower()


# ── Public API ───────────────────────────────────────────────────────

def get_site_memory(url: str) -> dict:
    """
    Get the full memory blob for a domain.
    Returns dict with keys: known_elements, page_flows, site_type,
    dom_fingerprint, selectors, visit_count, last_visited, notes.
    """
    mem = _load()
    domain = _domain_key(url)
    default = {
        "domain": domain,
        "site_type": None,              # e.g. "email", "ecommerce", "social", "search"
        "known_elements": {},           # { "compose_button": {"selector": "div.T-I", "label": "Compose"} }
        "page_flows": {},               # { "compose_email": ["click compose", "fill to", "fill subject", ...] }
        "dom_fingerprint": None,        # hash of element structure for change detection
        "selectors": {},                # { "search_box": "input[name=q]" }
        "obstacles": {},                # { "cookie_banner": {"selector": "...", "action": "click dismiss"} }
        "visit_count": 0,
        "last_visited": None,
        "notes": [],                    # free-form observations
    }
    entry = mem.get(domain, {})
    # Merge with defaults (fill missing keys)
    for k, v in default.items():
        if k not in entry:
            entry[k] = v
    return entry


def save_site_memory(url: str, data: dict):
    """
    Persist the entire memory blob for a domain.
    Call after modifying the dict returned by get_site_memory().
    """
    mem = _load()
    domain = _domain_key(url)
    data["domain"] = domain
    data["last_visited"] = time.time()
    mem[domain] = data
    _save()


def record_visit(url: str):
    """Increment visit count and timestamp for a domain."""
    entry = get_site_memory(url)
    entry["visit_count"] = entry.get("visit_count", 0) + 1
    entry["last_visited"] = time.time()
    save_site_memory(url, entry)


def learn_element(url: str, element_key: str,
                  selector: str = "", label: str = "",
                  role: str = "") -> dict:
    """
    Record a learned element for a domain.

    Args:
        url:          Any URL on the domain
        element_key:  Logical key, e.g. "compose_button", "search_box"
        selector:     CSS selector (for JS injection)
        label:        Visible label text
        role:         Accessibility role (e.g. "button", "textfield")

    Returns:
        {"success": True/False, "message": str}
    """
    entry = get_site_memory(url)
    known = entry.get("known_elements", {})
    known[element_key] = {
        "selector": selector,
        "label": label,
        "role": role,
        "learned_at": time.time(),
    }
    entry["known_elements"] = known
    save_site_memory(url, entry)
    return {"success": True, "message": f"Learned element '{element_key}' for {_domain_key(url)}"}


def get_learned_selector(url: str, element_key: str) -> Optional[str]:
    """Return the learned CSS selector for an element, or None."""
    entry = get_site_memory(url)
    known = entry.get("known_elements", {})
    elem = known.get(element_key, {})
    return elem.get("selector") or None


def learn_page_flow(url: str, flow_name: str, steps: list[str]) -> dict:
    """
    Record a sequence of steps for a common flow on this site.

    Args:
        url:        Any URL on the domain
        flow_name:  E.g. "compose_email", "add_to_cart"
        steps:      Ordered list of action descriptions

    Returns:
        {"success": True/False, "message": str}
    """
    entry = get_site_memory(url)
    flows = entry.get("page_flows", {})
    flows[flow_name] = {
        "steps": steps,
        "learned_at": time.time(),
    }
    entry["page_flows"] = flows
    save_site_memory(url, entry)
    return {"success": True, "message": f"Learned flow '{flow_name}' ({len(steps)} steps) for {_domain_key(url)}"}


def learn_obstacle(url: str, obstacle_key: str,
                   selector: str = "", action: str = "") -> dict:
    """
    Record a known obstacle (cookie banner, popup, paywall) for a domain.

    Args:
        url:          Any URL on the domain
        obstacle_key: E.g. "cookie_banner", "login_wall"
        selector:     CSS selector to detect it
        action:       What to do: "click dismiss", "pause", etc.

    Returns:
        {"success": True/False, "message": str}
    """
    entry = get_site_memory(url)
    obstacles = entry.get("obstacles", {})
    obstacles[obstacle_key] = {
        "selector": selector,
        "action": action,
        "learned_at": time.time(),
    }
    entry["obstacles"] = obstacles
    save_site_memory(url, entry)
    return {"success": True, "message": f"Learned obstacle '{obstacle_key}' for {_domain_key(url)}"}


def set_site_type(url: str, site_type: str) -> dict:
    """
    Set the site type for a domain.

    Args:
        url:       Any URL on the domain
        site_type: One of: email, ecommerce, social, search, news, video, docs, other

    Returns:
        {"success": True/False, "message": str}
    """
    entry = get_site_memory(url)
    entry["site_type"] = site_type
    save_site_memory(url, entry)
    return {"success": True, "message": f"Set site type '{site_type}' for {_domain_key(url)}"}


def add_note(url: str, note: str) -> dict:
    """Add a free-form observation note for a domain."""
    entry = get_site_memory(url)
    notes = entry.get("notes", [])
    notes.append({"text": note, "time": time.time()})
    # Keep last 20 notes
    entry["notes"] = notes[-20:]
    save_site_memory(url, entry)
    return {"success": True, "message": f"Added note for {_domain_key(url)}"}


def get_all_domains() -> list[str]:
    """Return all domains we have memory for."""
    mem = _load()
    return sorted(mem.keys())


def clear_site_memory(url: str) -> dict:
    """Clear all memory for a domain."""
    mem = _load()
    domain = _domain_key(url)
    if domain in mem:
        del mem[domain]
        _save()
        return {"success": True, "message": f"Cleared memory for {domain}"}
    return {"success": True, "message": f"No memory found for {domain}"}


def web_memory_summary(url: str) -> str:
    """
    Return a compact text summary of what we know about this domain.
    Suitable for LLM context injection.
    """
    entry = get_site_memory(url)
    domain = _domain_key(url)
    parts = [f"WEB MEMORY for {domain}:"]

    if entry.get("site_type"):
        parts.append(f"  Site type: {entry['site_type']}")
    parts.append(f"  Visits: {entry.get('visit_count', 0)}")

    known = entry.get("known_elements", {})
    if known:
        elems = [f"{k}={v.get('label', v.get('selector', '?'))}" for k, v in known.items()]
        parts.append(f"  Known elements: {', '.join(elems[:10])}")

    flows = entry.get("page_flows", {})
    if flows:
        parts.append(f"  Known flows: {', '.join(flows.keys())}")

    obstacles = entry.get("obstacles", {})
    if obstacles:
        parts.append(f"  Known obstacles: {', '.join(obstacles.keys())}")

    notes = entry.get("notes", [])
    if notes:
        last_note = notes[-1]["text"]
        parts.append(f"  Last note: {last_note[:100]}")

    return "\n".join(parts)
