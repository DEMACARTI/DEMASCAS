"""
DEMASCAS — actuators/network.py
Web search and page reading capabilities.

  - search_web(query)   → DuckDuckGo instant search (no API key)
  - read_webpage(url)   → Fetches & extracts readable text from any URL

OPTIMIZATIONS:
  - 10-second timeout on all HTTP requests.
  - Text truncated to 3 000 chars to stay within LLM context budget.
  - User-Agent spoofing to avoid bot-blocks on common sites.
"""

import re

import requests
from bs4 import BeautifulSoup

# Suppress SSL warnings for local assistant use
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HTTP_TIMEOUT = 10  # seconds
_MAX_TEXT_CHARS = 3000  # keep LLM context lean
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def search_web(query: str, max_results: int = 5) -> str:
    """
    Searches the web using DuckDuckGo and returns the top results.

    Args:
        query:        The search query string.
        max_results:  Number of results to return (default 5, max 10).

    Returns:
        A formatted string of search results with titles, URLs, and snippets.
    """
    print(f"[*] DEMASCAS is searching the web for: '{query}'...")
    max_results = min(max_results, 10)

    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return f"No results found for '{query}'."

        lines = [f"Search results for '{query}':\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("href", r.get("link", ""))
            snippet = r.get("body", r.get("snippet", ""))
            lines.append(f"{i}. {title}")
            if url:
                lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")

        return "\n".join(lines).strip()

    except ImportError:
        return "[-] ddgs package not installed. Run: pip install ddgs"
    except Exception as e:
        return f"[-] Web search failed: {e}"


def read_webpage(url: str) -> str:
    """
    Fetches a webpage and extracts the main readable text content.
    Strips HTML tags, scripts, styles, and navigation clutter.

    Args:
        url: The full URL to read (e.g. 'https://en.wikipedia.org/wiki/Python').

    Returns:
        The extracted text content (truncated to ~3 000 chars), or an error message.
    """
    print(f"[*] DEMASCAS is reading webpage: '{url}'...")

    # Ensure the URL has a scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT, verify=False)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()

        # Extract text
        text = soup.get_text(separator="\n", strip=True)

        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)

        if not text.strip():
            return f"[-] No readable text content found at {url}."

        # Truncate for LLM context budget
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "\n\n[...truncated — page has more content]"

        return f"Content from {url}:\n\n{text}"

    except requests.exceptions.Timeout:
        return f"[-] Timeout: {url} took too long to respond."
    except requests.exceptions.HTTPError as e:
        return f"[-] HTTP Error: {e}"
    except Exception as e:
        return f"[-] Failed to read webpage: {e}"


def ping_service(url: str) -> str:
    """
    Checks if a local service is reachable.

    Args:
        url: The URL to ping (e.g. 'http://localhost:11434').

    Returns:
        A status message string.
    """
    try:
        resp = requests.get(url, timeout=5)
        return f"Service at {url} is UP (HTTP {resp.status_code})."
    except Exception as e:
        return f"Service at {url} is DOWN: {e}"
