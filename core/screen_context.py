"""
DEMASCAS — core/screen_context.py
Screen Context Pre-Processor — makes the 3B model dramatically smarter
by pre-digesting the screen state BEFORE the LLM sees it.

Instead of dumping 1000+ tokens of raw accessibility tree, this module:
  1. Captures the screen (VLM screenshot) + accessibility tree
  2. Uses the VLM to create a structured digest: {app, page_type, key_elements}
  3. Returns a compact ~100 token context block for system prompt injection

This gives the 3B text model "sight" without needing to process images directly.

RAM BUDGET: 0 MB extra — reuses the existing VLM server and
  the accessibility tree from sensors/vision.py.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from sensors.vision import (
    get_active_window_tree,
    compress_tree,
    take_screenshot_base64,
    vision_query,
)


# ── Caching — avoid re-scanning if screen hasn't changed ─────────

_last_digest: dict | None = None
_last_digest_time: float = 0.0
_DIGEST_TTL: float = 3.0  # seconds — screen context valid for 3s


def _is_cache_valid() -> bool:
    """Check if cached digest is still fresh."""
    if _last_digest is None:
        return False
    return (time.time() - _last_digest_time) < _DIGEST_TTL


# ── Core screen digest ──────────────────────────────────────────

def get_screen_digest(force: bool = False) -> dict:
    """
    Create a structured screen digest combining accessibility tree + VLM.

    Returns dict with:
      - app: str — frontmost application name
      - page_type: str — what kind of page/view (e.g. "email inbox", "search results")
      - url: str | None — current URL if browser
      - key_elements: list[str] — important interactive elements
      - summary: str — 1-sentence description of what's on screen
      - raw_tree_compact: str — compressed accessibility tree (for tool context)

    Cached for _DIGEST_TTL seconds to avoid repeated VLM calls.
    """
    global _last_digest, _last_digest_time

    if not force and _is_cache_valid():
        return _last_digest

    digest = {
        "app": "Unknown",
        "page_type": "unknown",
        "url": None,
        "key_elements": [],
        "summary": "",
        "raw_tree_compact": "",
    }

    # ── Step 1: Get accessibility tree (fast, ~200ms) ────────────
    try:
        raw_tree = get_active_window_tree()
        compact_tree = compress_tree(raw_tree)
        digest["raw_tree_compact"] = compact_tree

        # Extract app name from tree header
        for line in raw_tree.split("\n"):
            if line.startswith("ACTIVE APP:"):
                digest["app"] = line.replace("ACTIVE APP:", "").strip()
                break

        # Extract key interactive elements from compact tree
        elements = []
        for line in compact_tree.split("\n"):
            if line.startswith("- ["):
                # Extract label: "- [button] : 'Submit'" → "Submit (button)"
                bracket_end = line.find("]")
                colon_pos = line.find(":", bracket_end)
                if bracket_end > 0 and colon_pos > 0:
                    elem_type = line[3:bracket_end].strip()
                    label = line[colon_pos + 1:].strip().strip("'\"")
                    if label and len(label) < 60:
                        elements.append(f"{label} ({elem_type})")

        digest["key_elements"] = elements[:15]  # cap at 15 elements

    except Exception as e:
        digest["raw_tree_compact"] = f"(tree unavailable: {e})"

    # ── Step 2: VLM scene understanding (slower, ~1-2s) ──────────
    try:
        screenshot = take_screenshot_base64()
        if not screenshot.startswith("[-]"):
            vlm_prompt = (
                "Describe this screen in ONE JSON object. Be precise and brief:\n"
                '{"page_type":"<inbox|search_results|compose|settings|document|chat|media|terminal|code_editor|file_browser|other>",'
                '"url":"<visible URL or null>",'
                '"summary":"<1 sentence: what is on screen>"}\n'
                "Output ONLY the JSON."
            )
            vlm_resp = vision_query(vlm_prompt, screenshot)

            # Parse VLM response
            first_brace = vlm_resp.find("{")
            last_brace = vlm_resp.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                parsed = json.loads(vlm_resp[first_brace:last_brace + 1])
                digest["page_type"] = parsed.get("page_type", "unknown")
                digest["url"] = parsed.get("url") or None
                digest["summary"] = parsed.get("summary", "")

    except Exception as e:
        # VLM failure is non-fatal — we still have the tree
        print(f"[SCREEN] VLM digest failed (non-fatal): {e}")

    _last_digest = digest
    _last_digest_time = time.time()
    return digest


def get_screen_context_block(force: bool = False) -> str:
    """
    Build a compact text block for injection into the LLM system prompt.
    ~50-100 tokens — gives the text model awareness of what's on screen.

    Returns empty string if screen can't be read.
    """
    try:
        d = get_screen_digest(force=force)
    except Exception:
        return ""

    parts = []

    if d["app"] != "Unknown":
        parts.append(f"Active app: {d['app']}")
    if d["page_type"] != "unknown":
        parts.append(f"Page type: {d['page_type']}")
    if d["url"]:
        parts.append(f"URL: {d['url']}")
    if d["summary"]:
        parts.append(f"Screen: {d['summary']}")
    if d["key_elements"]:
        # Show top 8 elements
        elems = ", ".join(d["key_elements"][:8])
        parts.append(f"Key elements: {elems}")

    if not parts:
        return ""

    return "[SCREEN CONTEXT]\n" + "\n".join(parts)


# ── Fast-path: tree-only digest (no VLM, ~200ms) ────────────────

def get_tree_only_context() -> str:
    """
    Lightweight screen context using ONLY the accessibility tree.
    No VLM call — much faster (~200ms vs ~2s).

    Use this for PATH 3 (ReAct tool loop) where speed matters.
    The VLM-enriched version is for PATH 4 (deep intent) where
    quality matters more.
    """
    try:
        raw_tree = get_active_window_tree()
        compact = compress_tree(raw_tree)

        # Extract app name
        app = "Unknown"
        for line in raw_tree.split("\n"):
            if line.startswith("ACTIVE APP:"):
                app = line.replace("ACTIVE APP:", "").strip()
                break

        # Extract key elements
        elements = []
        for line in compact.split("\n"):
            if line.startswith("- ["):
                bracket_end = line.find("]")
                colon_pos = line.find(":", bracket_end)
                if bracket_end > 0 and colon_pos > 0:
                    label = line[colon_pos + 1:].strip().strip("'\"")
                    if label and len(label) < 60:
                        elements.append(label)

        if not elements:
            return f"[SCREEN] Active app: {app}"

        elems = ", ".join(elements[:10])
        return f"[SCREEN] Active app: {app} | Elements: {elems}"

    except Exception:
        return ""


# ── Intent-aware digest (for PATH 4 deep intent) ────────────────

def get_intent_aware_digest(user_command: str) -> dict:
    """
    Screen digest enriched with intent awareness.
    The VLM is asked to identify elements relevant to the user's command.

    Args:
        user_command: The user's spoken command (e.g. "reply to that email")

    Returns:
        Extended digest with 'relevant_elements' and 'suggested_action' fields.
    """
    digest = get_screen_digest(force=True)

    try:
        screenshot = take_screenshot_base64()
        if screenshot.startswith("[-]"):
            return digest

        vlm_prompt = (
            f'The user said: "{user_command}"\n'
            "Look at the screen and respond with ONLY a JSON object:\n"
            '{"relevant_elements":["<element 1>","<element 2>"],'
            '"suggested_action":"<what to click/type/do first>",'
            '"needs_navigation":false}\n'
            "Output ONLY the JSON."
        )
        vlm_resp = vision_query(vlm_prompt, screenshot)

        first_brace = vlm_resp.find("{")
        last_brace = vlm_resp.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            parsed = json.loads(vlm_resp[first_brace:last_brace + 1])
            digest["relevant_elements"] = parsed.get("relevant_elements", [])
            digest["suggested_action"] = parsed.get("suggested_action", "")
            digest["needs_navigation"] = parsed.get("needs_navigation", False)

    except Exception as e:
        print(f"[SCREEN] Intent-aware digest failed (non-fatal): {e}")

    return digest
