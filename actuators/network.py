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


def check_internet() -> dict:
    """
    Is this Mac online, and how good is the connection?  Local and read-only:
    a TCP connect to two public resolvers (latency), one DNS lookup, and the
    Wi-Fi network's name when macOS shares it.  ~0.1–2 s.
    """
    import socket
    import subprocess
    import time

    def connect_ms(host: str, port: int) -> float | None:
        t = time.time()
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return (time.time() - t) * 1000
        except OSError:
            return None

    def dns() -> float | None:
        t = time.time()
        try:
            socket.getaddrinfo("apple.com", 443)
            return (time.time() - t) * 1000
        except OSError:
            return None

    def wifi_name() -> str:
        try:
            out = subprocess.run(["networksetup", "-getairportnetwork", "en0"], capture_output=True, text=True, timeout=2).stdout
            if ":" in out and "not associated" not in out.lower():
                return out.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    # all four at once: the answer takes as long as the slowest (≤ ~2 s), not their sum
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        f1, f2 = ex.submit(connect_ms, "1.1.1.1", 443), ex.submit(connect_ms, "8.8.8.8", 53)
        fd, fw = ex.submit(dns), ex.submit(wifi_name)
        times = [m for m in (f1.result(), f2.result()) if m is not None]
        dns_ms, wifi = fd.result(), fw.result()
    via = f" over Wi-Fi “{wifi}”" if wifi else ""
    if not times:
        msg = ("You're offline — I can't reach the internet" + (f", although Wi-Fi “{wifi}” is connected" if wifi else "") + ".")
        return {"success": True, "online": False, "wifi": wifi, "message": msg}
    best = min(times)
    if dns_ms is None:
        return {"success": True, "online": True, "latency_ms": round(best), "wifi": wifi,
                "message": f"You're connected{via}, but looking up websites is failing, so pages may not load."}
    quality = "fast" if best < 60 else "fine" if best < 150 else "slow"
    return {"success": True, "online": True, "latency_ms": round(best), "dns_ms": round(dns_ms), "wifi": wifi,
            "message": f"You're online{via}. The connection is {quality} — it answers in {round(best)} milliseconds."}
