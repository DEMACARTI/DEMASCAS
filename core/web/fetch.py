"""
DEMASCAS — core/web/fetch.py
One polite page fetcher for everything DEMASCAS reads on the web
(research, app learning, presentations).

Polite: honours robots.txt, waits between requests to the same site,
identifies itself, caps page size.  Safe: http(s) only, never private /
local addresses (the tool server must not be turned into a LAN scanner).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

USER_AGENT = "Mozilla/5.0 (Macintosh) DEMASCAS/1.0 (personal assistant; respects robots.txt)"
MAX_BYTES = 3_000_000
PER_HOST_DELAY = 2.0            # seconds between requests to one site

_robots: dict[str, tuple[float, RobotFileParser | None]] = {}
_last_hit: dict[str, float] = {}
_lock = threading.Lock()


@dataclass
class Page:
    url: str
    ok: bool = False
    status: int = 0
    title: str = ""
    text: str = ""
    tables: list = field(default_factory=list)   # [(header cells, [row cells…]), …]
    published: str = ""
    error: str = ""


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def allowed(url: str) -> tuple[bool, str]:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return False, "only http(s) links"
    host = p.hostname or ""
    if not _public_host(host):
        return False, "private or unknown address"
    now = time.time()
    with _lock:
        cached = _robots.get(host)
    if cached is None or now - cached[0] > 6 * 3600:
        rp = RobotFileParser()
        try:
            import requests
            r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=5,
                             headers={"User-Agent": USER_AGENT})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp = None                       # robots.txt unreachable → treat as allowed
        cached = (now, rp)
        with _lock:
            _robots[host] = cached
    rp = cached[1]
    if rp is not None and not rp.can_fetch(USER_AGENT, url):
        return False, "the site's robots.txt disallows it"
    return True, ""


def _wait_turn(host: str) -> None:
    while True:
        with _lock:
            gap = time.time() - _last_hit.get(host, 0)
            if gap >= PER_HOST_DELAY:
                _last_hit[host] = time.time()
                return
        time.sleep(PER_HOST_DELAY - gap)


def fetch(url: str, timeout: float = 10.0) -> Page:
    """Fetch and extract a page: main text, tables, title, publish date."""
    import requests
    from bs4 import BeautifulSoup
    page = Page(url=url)
    ok, why = allowed(url)
    if not ok:
        page.error = why
        return page
    _wait_turn(urlparse(url).hostname or "")
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, stream=True)
        page.status = r.status_code
        ctype = r.headers.get("content-type", "text/html")
        if r.status_code != 200 or not re.search(r"html|text/plain", ctype):
            page.error = f"HTTP {r.status_code} {ctype.split(';')[0]}"
            return page
        body = r.raw.read(MAX_BYTES, decode_content=True)
        html = body.decode(r.encoding or "utf-8", errors="replace")
    except Exception as e:
        page.error = str(e)[:120]
        return page

    if "text/plain" in ctype:
        page.text, page.ok = html[:60000], True
        return page
    soup = BeautifulSoup(html, "lxml")
    page.title = (soup.title.get_text(strip=True) if soup.title else "")[:200]
    for sel in ('meta[property="article:published_time"]', 'meta[name="date"]',
                'meta[itemprop="datePublished"]', "time[datetime]"):
        tag = soup.select_one(sel)
        if tag is not None:
            page.published = (tag.get("content") or tag.get("datetime") or "")[:25]
            break
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"]):
        t.decompose()
    for table in soup.find_all("table"):
        header, rows = None, []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if header is None and tr.find("th") is not None:
                header = cells
                continue
            if len([c for c in cells if c]) >= 2:
                rows.append(cells)
        page.tables.append((header, rows))
        table.decompose()                  # don't re-read tables as loose lines
    # The real article body: the biggest content-ish container (help-centre
    # themes often keep the article outside <article>, which holds a sidebar).
    cands = soup.find_all(class_=re.compile(r"(article|post|entry|page|main)[-_]?(body|content)", re.I))
    cands += [x for x in (soup.find("main"), soup.find("article")) if x is not None]
    root = max(cands, key=lambda x: len(x.get_text()), default=None) or soup.body
    if root is not None:
        page.text = re.sub(r"\n\s*\n+", "\n", root.get_text("\n"))[:60000]
    # short real pages count too (example.com is ~180 characters — the old 200 cut-off
    # called it "no readable content"); a few real words, not just a heading
    page.ok = (len(page.text.strip()) > 60 and len(page.text.split()) >= 8) or bool(page.tables)
    if not page.ok:
        page.error = "no readable content"
    return page

