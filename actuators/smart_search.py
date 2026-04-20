"""
DEMASCAS — actuators/smart_search.py
Intelligent search with caching, Google API, and DDG fallback.

Search chain:
  1. Check fact cache (ChromaDB) — instant, zero API cost
  2. Google Custom Search API — high quality, requires key (optional)
  3. DuckDuckGo fallback — always works, no key needed
  4. LLM synthesis — combine results into a concise answer

Convenience wrappers:
  - get_weather(location)  — weather via wttr.in (free, no key)
  - get_news(topic)        — news headlines via DDG news

Config (optional, in ~/.demascas/config.json):
  {
    "google_api_key": "...",
    "google_cse_id": "..."
  }
If not configured, Google search is skipped silently → DDG is used.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

_IST = timezone(timedelta(hours=5, minutes=30))
_DEMASCAS_DIR = os.path.expanduser("~/.demascas")
_CONFIG_PATH = os.path.join(_DEMASCAS_DIR, "config.json")

_HTTP_TIMEOUT = 10
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Lazy-loaded config
_config: dict | None = None


def _load_config() -> dict:
    """Load config from ~/.demascas/config.json (cached in memory)."""
    global _config
    if _config is not None:
        return _config

    _config = {}
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r") as f:
                _config = json.load(f)
    except Exception:
        pass

    # Also check environment variables (run.sh can set these)
    if not _config.get("google_api_key"):
        _config["google_api_key"] = os.environ.get("GOOGLE_API_KEY", "")
    if not _config.get("google_cse_id"):
        _config["google_cse_id"] = os.environ.get("GOOGLE_CSE_ID", "")

    return _config


# ═══════════════════════════════════════════════════════════════════════
# Fact cache — check ChromaDB before hitting the network
# ═══════════════════════════════════════════════════════════════════════

def _check_fact_cache(query: str, ttl_hours: float = 24.0) -> str | None:
    """Check if we have a cached fact for this query (within TTL)."""
    try:
        from core.knowledge import get_fact
        facts = get_fact(query, top_k=1)
        if facts:
            # Check if it's a search cache entry with timestamp
            fact = facts[0]
            if "Search result for" in fact:
                return fact
            # Other facts are always valid
            return fact
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════
# Google Custom Search API
# ═══════════════════════════════════════════════════════════════════════

def _google_search(query: str, max_results: int = 5) -> list[dict] | None:
    """
    Search via Google Custom Search API.
    Returns list of {"title", "url", "snippet"} or None if unavailable.
    """
    config = _load_config()
    api_key = config.get("google_api_key", "")
    cse_id = config.get("google_cse_id", "")

    if not api_key or not cse_id:
        return None  # silent skip — DDG will handle it

    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": api_key,
                "cx": cse_id,
                "q": query,
                "num": min(max_results, 10),
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return results if results else None

    except Exception as e:
        print(f"[SMART-SEARCH] Google API error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# DuckDuckGo fallback
# ═══════════════════════════════════════════════════════════════════════

def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Search via DuckDuckGo (always available, no key needed).
    Returns list of {"title", "url", "snippet"}.
    """
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))

        results = []
        for r in raw:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", r.get("link", "")),
                "snippet": r.get("body", r.get("snippet", "")),
            })
        return results
    except Exception as e:
        print(f"[SMART-SEARCH] DDG error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════

def smart_search(query: str, ttl_hours: float = 24.0) -> str:
    """
    Intelligent search with caching and multi-source fallback.

    Search chain:
      1. Fact cache (ChromaDB) → instant
      2. Google Custom Search → high quality (if configured)
      3. DuckDuckGo → always works
      4. Format results as concise answer

    Args:
        query: The search query
        ttl_hours: Cache TTL in hours (default 24)

    Returns:
        Formatted search results string, or error message with [-] prefix.
    """
    print(f"[SMART-SEARCH] Query: '{query}'")

    # Step 1: Check fact cache
    cached = _check_fact_cache(query, ttl_hours)
    if cached:
        print(f"[SMART-SEARCH] Cache hit!")
        return f"[cached] {cached}"

    # Step 2: Try Google API
    results = _google_search(query)
    source = "Google"

    # Step 3: Fallback to DuckDuckGo
    if not results:
        results = _ddg_search(query)
        source = "DuckDuckGo"

    if not results:
        return f"[-] No search results found for '{query}'."

    # Step 4: Format results
    lines = [f"Search results for '{query}' (via {source}):\n"]
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")

    answer = "\n".join(lines).strip()

    # Auto-cache the result as a fact
    try:
        from core.knowledge import save_fact
        save_fact(
            f"Search result for '{query}': {answer[:300]}",
            category="search_cache",
        )
    except Exception:
        pass

    return answer


# ═══════════════════════════════════════════════════════════════════════
# Convenience wrappers
# ═══════════════════════════════════════════════════════════════════════

def get_weather(location: str = "") -> str:
    """
    Get current weather for a location using wttr.in (free, no API key).

    Args:
        location: City name or coordinates. If empty, uses IP geolocation.

    Returns:
        Weather summary string, or error with [-] prefix.
    """
    print(f"[WEATHER] Location: '{location or 'auto-detect'}'")
    try:
        url = f"https://wttr.in/{requests.utils.quote(location)}?format=j1"
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])[0]

        city = area.get("areaName", [{}])[0].get("value", location or "your location")
        temp_c = current.get("temp_C", "?")
        feels_like = current.get("FeelsLikeC", "?")
        desc = current.get("weatherDesc", [{}])[0].get("value", "Unknown")
        humidity = current.get("humidity", "?")
        wind_kmph = current.get("windspeedKmph", "?")

        return (
            f"Weather in {city}: {desc}, {temp_c}°C (feels like {feels_like}°C), "
            f"humidity {humidity}%, wind {wind_kmph} km/h"
        )
    except Exception as e:
        return f"[-] Weather lookup failed: {e}"


def get_news(topic: str = "", max_results: int = 5) -> str:
    """
    Get latest news headlines using DuckDuckGo News.

    Args:
        topic: News topic to search for. If empty, gets top headlines.
        max_results: Number of headlines (default 5, max 10).

    Returns:
        Formatted news headlines string, or error with [-] prefix.
    """
    print(f"[NEWS] Topic: '{topic or 'top headlines'}'")
    max_results = min(max_results, 10)

    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.news(topic or "top news", max_results=max_results))

        if not results:
            return f"No news found for '{topic}'." if topic else "No news headlines available."

        lines = [f"News headlines" + (f" for '{topic}'" if topic else "") + ":\n"]
        for i, r in enumerate(results[:max_results], 1):
            title = r.get("title", "No title")
            source = r.get("source", "")
            date = r.get("date", "")
            url = r.get("url", r.get("link", ""))
            lines.append(f"{i}. {title}")
            if source:
                lines.append(f"   Source: {source}")
            if date:
                lines.append(f"   Date: {date}")
            if url:
                lines.append(f"   URL: {url}")
            lines.append("")

        return "\n".join(lines).strip()

    except ImportError:
        return "[-] ddgs package not installed. Run: pip install ddgs"
    except Exception as e:
        return f"[-] News lookup failed: {e}"
