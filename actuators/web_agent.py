"""
DEMASCAS — actuators/web_agent.py
Autonomous web agent: DOM intelligence, obstacle detection, web search/research,
goal execution, site-specific agents, and memory-enhanced browsing.

Uses actuators/browser.py for all low-level browser interaction.
Uses memory/web_memory.py for per-domain persistent learning.

Hard constraints:
  - CAPTCHA, paywall, login wall → PAUSE and report (never bypass)
  - E-commerce purchase / social post → require user confirmation
  - Never auto-send emails
  - No new pip dependencies
"""

import json
import time
import re
import urllib.request
import urllib.parse
import urllib.error
import sys
from typing import Optional

if sys.platform.startswith('linux'):
    from actuators.browser_linux import (
        browser_execute_js, browser_get_url, browser_get_title,
        browser_get_page_text, browser_get_links, browser_get_inputs,
        browser_get_dom_summary, browser_click, browser_click_text,
        browser_type, browser_type_by_label, browser_scroll,
        browser_navigate_and_wait, browser_wait_for_page_load,
        browser_element_exists, browser_press_key,
    )
else:
    from actuators.browser import (
        browser_execute_js, browser_get_url, browser_get_title,
        browser_get_page_text, browser_get_links, browser_get_inputs,
        browser_get_dom_summary, browser_click, browser_click_text,
        browser_type, browser_type_by_label, browser_scroll,
        browser_navigate_and_wait, browser_wait_for_page_load,
        browser_element_exists, browser_press_key,
    )
from memory.web_memory import (
    get_site_memory, save_site_memory, record_visit,
    learn_element, get_learned_selector, learn_obstacle,
    set_site_type, web_memory_summary,
)


# ══════════════════════════════════════════════════════════════════════
# DOM Intelligence — analyze page structure
# ══════════════════════════════════════════════════════════════════════

def web_analyze_page() -> dict:
    """
    Perform deep analysis of the current page:
    - Classify site type (email, search, ecommerce, social, etc.)
    - Extract interactive elements
    - Check for obstacles (popups, banners, paywalls)
    - Record visit in web memory

    Returns:
        {"success": bool, "message": str} — message is the analysis report
    """
    url_info = browser_get_url()
    url = url_info.get("url", "")
    title_info = browser_get_title()
    title = title_info.get("title", "")

    # Record visit
    if url:
        record_visit(url)

    # Get DOM summary
    dom = browser_get_dom_summary()
    dom_text = dom.get("message", "")

    # Get inputs
    inputs_result = browser_get_inputs()
    inputs_text = inputs_result.get("message", "")

    # Classify site type
    site_type = _classify_site(url, title, dom_text)
    if url:
        set_site_type(url, site_type)

    # Check for obstacles
    obstacles = _detect_obstacles_js()

    # Build report
    parts = [
        f"PAGE ANALYSIS:",
        f"  URL: {url}",
        f"  Title: {title}",
        f"  Type: {site_type}",
    ]

    if obstacles:
        parts.append(f"  OBSTACLES: {', '.join(obstacles)}")

    if dom_text:
        parts.append(f"  Interactive elements:\n{dom_text[:2000]}")

    if inputs_text:
        try:
            inputs_data = json.loads(inputs_text)
            if inputs_data:
                inp_summary = [f"    {i.get('tag','')}[{i.get('type','')}] name={i.get('name','')} placeholder={i.get('placeholder','')}"
                               for i in inputs_data[:10]]
                parts.append("  Input fields:\n" + "\n".join(inp_summary))
        except (json.JSONDecodeError, TypeError):
            pass

    # Add memory context
    if url:
        mem = web_memory_summary(url)
        if mem:
            parts.append(f"\n{mem}")

    return {"success": True, "message": "\n".join(parts)}


def _classify_site(url: str, title: str, dom_text: str) -> str:
    """Heuristic site type classification."""
    url_lower = url.lower()
    title_lower = title.lower()
    combined = url_lower + " " + title_lower

    if any(x in combined for x in ["mail.google", "gmail", "outlook.com", "yahoo.com/mail"]):
        return "email"
    if any(x in combined for x in ["youtube.com", "vimeo.com", "twitch.tv"]):
        return "video"
    if any(x in combined for x in ["amazon.com", "flipkart.com", "ebay.com", "shopping", "cart", "checkout"]):
        return "ecommerce"
    if any(x in combined for x in ["twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com", "reddit.com"]):
        return "social"
    if any(x in combined for x in ["google.com/search", "bing.com/search", "duckduckgo.com"]):
        return "search"
    if any(x in combined for x in ["docs.google.com", "notion.so", "confluence"]):
        return "docs"
    if any(x in combined for x in ["news", "article", "blog", "medium.com", "substack"]):
        return "news"
    if any(x in combined for x in ["github.com", "gitlab.com", "stackoverflow.com"]):
        return "developer"
    return "other"


def _detect_obstacles_js() -> list[str]:
    """Detect common web obstacles via JS DOM inspection."""
    obstacles = []

    # Check for cookie consent banners
    js_cookie = '''
    (function() {
        var selectors = [
            '[class*="cookie"]', '[id*="cookie"]', '[class*="consent"]',
            '[id*="consent"]', '[class*="gdpr"]', '[id*="gdpr"]',
            '[class*="CookieConsent"]', '#onetrust-banner-sdk'
        ];
        for (var i = 0; i < selectors.length; i++) {
            var el = document.querySelector(selectors[i]);
            if (el && el.offsetHeight > 0) return 'FOUND';
        }
        return 'NONE';
    })()
    '''
    result = browser_execute_js(js_cookie)
    if result.get("value") == "FOUND":
        obstacles.append("cookie_banner")

    # Check for login wall
    js_login = '''
    (function() {
        var selectors = [
            '[class*="login-wall"]', '[class*="loginWall"]',
            '[class*="signin-wall"]', '[class*="auth-wall"]',
            '[class*="paywall"]', '[id*="paywall"]',
        ];
        for (var i = 0; i < selectors.length; i++) {
            var el = document.querySelector(selectors[i]);
            if (el && el.offsetHeight > 0) return 'FOUND';
        }
        // Also check if a modal covers most of the viewport
        var modals = document.querySelectorAll('[role=dialog],[class*=modal],[class*=overlay]');
        for (var j = 0; j < modals.length; j++) {
            var m = modals[j];
            if (m.offsetHeight > window.innerHeight * 0.5 && m.offsetWidth > window.innerWidth * 0.5) {
                return 'MODAL';
            }
        }
        return 'NONE';
    })()
    '''
    result = browser_execute_js(js_login)
    val = result.get("value", "")
    if val == "FOUND":
        obstacles.append("login_wall")
    elif val == "MODAL":
        obstacles.append("popup_modal")

    # Check for CAPTCHA
    js_captcha = '''
    (function() {
        if (document.querySelector('[class*="captcha"]') ||
            document.querySelector('[id*="captcha"]') ||
            document.querySelector('iframe[src*="recaptcha"]') ||
            document.querySelector('iframe[src*="hcaptcha"]') ||
            document.querySelector('[class*="cf-turnstile"]')) {
            return 'FOUND';
        }
        return 'NONE';
    })()
    '''
    result = browser_execute_js(js_captcha)
    if result.get("value") == "FOUND":
        obstacles.append("captcha")

    return obstacles


# ══════════════════════════════════════════════════════════════════════
# Obstacle handling
# ══════════════════════════════════════════════════════════════════════

def web_scan_for_obstacles() -> dict:
    """
    Scan the current page for obstacles (cookie banners, login walls,
    CAPTCHAs, popups, paywalls).

    Returns:
        {"success": bool, "message": str, "obstacles": list[str]}
    """
    obstacles = _detect_obstacles_js()
    if not obstacles:
        return {"success": True, "message": "No obstacles detected", "obstacles": []}

    return {
        "success": True,
        "message": f"Obstacles detected: {', '.join(obstacles)}",
        "obstacles": obstacles,
    }


def web_handle_obstacle(obstacle_type: str = "") -> dict:
    """
    Attempt to handle a detected obstacle.

    - cookie_banner → try to click dismiss/accept
    - popup_modal → try to close
    - captcha → PAUSE and report (cannot bypass)
    - login_wall → PAUSE and report
    - paywall → PAUSE and report

    Args:
        obstacle_type: Type of obstacle to handle. If empty, auto-detects.

    Returns:
        {"success": bool, "message": str}
    """
    if not obstacle_type:
        scan = web_scan_for_obstacles()
        obstacles = scan.get("obstacles", [])
        if not obstacles:
            return {"success": True, "message": "No obstacles to handle"}
        obstacle_type = obstacles[0]

    obstacle_type = obstacle_type.lower().strip()

    if obstacle_type in ("captcha", "recaptcha", "hcaptcha"):
        return {
            "success": False,
            "message": "CAPTCHA detected. I cannot bypass CAPTCHAs. "
                       "Please solve it manually and tell me when you're done."
        }

    if obstacle_type in ("login_wall", "signin_wall", "auth_wall"):
        return {
            "success": False,
            "message": "Login wall detected. This site requires you to sign in. "
                       "Please log in manually and tell me when done."
        }

    if obstacle_type == "paywall":
        return {
            "success": False,
            "message": "Paywall detected. This content is behind a paywall. "
                       "I cannot bypass paywalls."
        }

    if obstacle_type == "cookie_banner":
        # Try common accept/dismiss selectors
        dismiss_selectors = [
            '[class*="accept"]', '[class*="Accept"]',
            '[id*="accept"]', 'button[class*="consent"]',
            '[class*="agree"]', '#onetrust-accept-btn-handler',
            '[class*="cookie"] button', '[class*="dismiss"]',
            'button[aria-label*="Accept"]', 'button[aria-label*="accept"]',
        ]
        for sel in dismiss_selectors:
            result = browser_element_exists(sel)
            if result.get("message") == "exists":
                click_result = browser_click(sel)
                if click_result.get("success"):
                    url = browser_get_url().get("url", "")
                    if url:
                        learn_obstacle(url, "cookie_banner", selector=sel, action="click dismiss")
                    return {"success": True, "message": "Cookie banner dismissed"}

        # Fallback: try clicking text
        for text in ["Accept", "Accept all", "I agree", "OK", "Got it", "Allow all"]:
            result = browser_click_text(text)
            if result.get("success"):
                return {"success": True, "message": f"Cookie banner dismissed (clicked '{text}')"}

        return {"success": False, "message": "Could not dismiss cookie banner automatically"}

    if obstacle_type == "popup_modal":
        # Try common close selectors
        close_selectors = [
            'button[aria-label="Close"]', 'button[aria-label="close"]',
            '[class*="close"]', '[class*="dismiss"]',
            'button.close', '.modal button:first-child',
        ]
        for sel in close_selectors:
            result = browser_element_exists(sel)
            if result.get("message") == "exists":
                click_result = browser_click(sel)
                if click_result.get("success"):
                    return {"success": True, "message": "Popup dismissed"}

        # Try pressing Escape
        browser_press_key("Escape")
        time.sleep(0.5)
        return {"success": True, "message": "Attempted to close popup with Escape key"}

    return {"success": False, "message": f"Unknown obstacle type: {obstacle_type}"}


# ══════════════════════════════════════════════════════════════════════
# Web search & research (scraping via Brave, not requests)
# ══════════════════════════════════════════════════════════════════════

def web_search(query: str) -> dict:
    """
    Perform a Google search in Brave Browser and extract results.

    Args:
        query: Search query.

    Returns:
        {"success": bool, "message": str} — message contains search results
    """
    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}"

    nav = browser_navigate_and_wait(url)
    if not nav["success"]:
        return nav

    time.sleep(1.5)

    # Handle cookie banner if on Google
    web_handle_obstacle("cookie_banner")

    # Extract search results via JS
    js = '''
    (function() {
        var results = [];
        var items = document.querySelectorAll('#search .g, #rso .g');
        for (var i = 0; i < Math.min(items.length, 8); i++) {
            var titleEl = items[i].querySelector('h3');
            var linkEl = items[i].querySelector('a[href]');
            var snippetEl = items[i].querySelector('.VwiC3b, .IsZvec, [data-sncf]');
            if (titleEl && linkEl) {
                results.push({
                    title: titleEl.innerText,
                    url: linkEl.href,
                    snippet: snippetEl ? snippetEl.innerText.substring(0, 200) : ''
                });
            }
        }
        return JSON.stringify(results);
    })()
    '''
    result = browser_execute_js(js)
    if not result["success"]:
        return result

    try:
        data = json.loads(result.get("value", "[]"))
        if not data:
            return {"success": True, "message": "No search results found. Google may be showing a different layout."}
        lines = []
        for i, r in enumerate(data, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r.get('snippet', '')}")
        return {"success": True, "message": "\n".join(lines)}
    except json.JSONDecodeError:
        return {"success": True, "message": result.get("value", "Could not parse results")}


def web_read_article(url: str = "") -> dict:
    """
    Read the main text content of a web article/page.
    If no URL given, reads the current page.

    Args:
        url: Optional URL to navigate to first. If empty, reads current page.

    Returns:
        {"success": bool, "message": str} — message is the extracted text
    """
    if url:
        nav = browser_navigate_and_wait(url)
        if not nav["success"]:
            return nav
        time.sleep(1)
        web_handle_obstacle("cookie_banner")

    # Extract article text
    text = browser_get_page_text(max_chars=5000)
    title_info = browser_get_title()
    title = title_info.get("title", "")

    content = text.get("message", "")
    if not content or len(content) < 50:
        return {"success": False, "message": "[-] Could not extract meaningful text from page"}

    return {"success": True, "message": f"[{title}]\n\n{content}"}


def web_research(topic: str, num_sources: int = 3) -> dict:
    """
    Research a topic using multiple web sources in Brave Browser.
    Searches, reads top results, and synthesizes findings.

    Args:
        topic:       Research topic.
        num_sources: Number of sources to read (default 3, max 5).

    Returns:
        {"success": bool, "message": str} — synthesized research findings
    """
    num_sources = min(max(1, num_sources), 5)

    # Step 1: Search
    search_result = web_search(topic)
    if not search_result["success"]:
        return search_result

    # Parse search results to get URLs
    urls = []
    for line in search_result["message"].split("\n"):
        line = line.strip()
        if line.startswith("http"):
            urls.append(line)
    urls = urls[:num_sources]

    if not urls:
        return {"success": True, "message": f"Search results:\n{search_result['message']}"}

    # Step 2: Read each source
    findings = [f"RESEARCH: {topic}\n"]
    findings.append(f"Sources searched: {len(urls)}\n")

    for i, url in enumerate(urls, 1):
        article = web_read_article(url)
        if article["success"]:
            content = article["message"][:1500]
            findings.append(f"\n--- Source {i}: {url} ---\n{content}")
        time.sleep(0.5)

    return {"success": True, "message": "\n".join(findings)}


# ══════════════════════════════════════════════════════════════════════
# Goal execution — multi-step autonomous browsing
# ══════════════════════════════════════════════════════════════════════

def web_execute_goal(goal: str) -> dict:
    """
    Execute a high-level web browsing goal autonomously.
    Breaks the goal into steps, handles obstacles, and learns from results.

    This is the main entry point for autonomous web actions.

    Args:
        goal: Natural language description of what to accomplish.
              E.g. "Search for Python tutorials on YouTube"
              E.g. "Find the cheapest flight from Delhi to Mumbai on Google Flights"

    Returns:
        {"success": bool, "message": str}
    """
    # Step 1: Analyze current page
    analysis = web_analyze_page()

    # Step 2: Check for obstacles first
    scan = web_scan_for_obstacles()
    obstacles = scan.get("obstacles", [])
    if obstacles:
        for obs in obstacles:
            handle_result = web_handle_obstacle(obs)
            if not handle_result["success"] and obs in ("captcha", "login_wall", "paywall"):
                return handle_result
        time.sleep(0.5)

    # Step 3: Classify goal type and execute
    goal_lower = goal.lower()

    # Search goals
    if any(kw in goal_lower for kw in ["search for", "google", "look up", "find"]):
        # Extract search query from goal
        query = goal
        for prefix in ["search for", "google", "look up", "find", "search"]:
            if goal_lower.startswith(prefix):
                query = goal[len(prefix):].strip()
                break
        return web_search(query)

    # Read/research goals
    if any(kw in goal_lower for kw in ["read", "article", "research", "learn about"]):
        topic = goal
        for prefix in ["read about", "research", "learn about", "read"]:
            if goal_lower.startswith(prefix):
                topic = goal[len(prefix):].strip()
                break
        return web_research(topic)

    # Navigation goals
    if any(kw in goal_lower for kw in ["go to", "navigate to", "open"]):
        # Extract URL
        words = goal.split()
        for i, w in enumerate(words):
            if "." in w and len(w) > 3:
                return browser_navigate_and_wait(w)
        return {"success": False, "message": "Could not determine URL from goal"}

    # Generic — analyze page and describe what's available
    return {
        "success": True,
        "message": f"Page analyzed. Goal: {goal}\n\n{analysis['message']}\n\n"
                   "I can see the page elements. Tell me the specific action to take."
    }


# ══════════════════════════════════════════════════════════════════════
# Smart click — memory-enhanced element clicking
# ══════════════════════════════════════════════════════════════════════

def web_smart_click(element_description: str) -> dict:
    """
    Smart click that uses web memory learned selectors, then falls back to
    text search, then CSS selector search.

    Args:
        element_description: Description of what to click (e.g. "Compose", "Search",
                            "Add to cart", or a CSS selector like "#submit").

    Returns:
        {"success": bool, "message": str}
    """
    url_info = browser_get_url()
    url = url_info.get("url", "")

    # 1) Check web memory for a learned selector
    if url:
        key = element_description.lower().replace(" ", "_")
        learned_sel = get_learned_selector(url, key)
        if learned_sel:
            result = browser_click(learned_sel)
            if result.get("success"):
                return {"success": True, "message": f"Clicked '{element_description}' (learned selector)"}

    # 2) Try as CSS selector if it looks like one
    if any(c in element_description for c in [".", "#", "[", ">"]):
        result = browser_click(element_description)
        if result.get("success"):
            return result

    # 3) Try clicking by visible text
    result = browser_click_text(element_description)
    if result.get("success"):
        # Learn this for next time
        if url:
            _learn_clicked_element(url, element_description)
        return result

    # 4) Try partial text match via DOM search
    js = f'''
    (function() {{
        var target = '{element_description.replace(chr(39), chr(92)+chr(39))}'.toLowerCase();
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            var text = (el.innerText || el.getAttribute('aria-label') || el.title || '').toLowerCase();
            if (text.includes(target) && el.offsetHeight > 0 && el.offsetWidth > 0) {{
                el.click();
                return 'clicked: ' + el.tagName + ' ' + text.substring(0, 50);
            }}
        }}
        return 'NOT_FOUND';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value", "").startswith("clicked"):
        if url:
            _learn_clicked_element(url, element_description)
        return {"success": True, "message": result["value"]}

    return {"success": False, "message": f"[-] Could not find element: '{element_description}'"}


def _learn_clicked_element(url: str, description: str):
    """After a successful click, try to learn the selector for future use."""
    js = '''
    (function() {
        var el = document.activeElement;
        if (!el || el === document.body) return '';
        var selector = '';
        if (el.id) selector = '#' + el.id;
        else if (el.className) {
            var cls = el.className.toString().trim().split(/\\s+/).slice(0, 3).join('.');
            selector = el.tagName.toLowerCase() + '.' + cls;
        }
        return selector;
    })()
    '''
    result = browser_execute_js(js)
    sel = result.get("value", "")
    if sel:
        key = description.lower().replace(" ", "_")
        learn_element(url, key, selector=sel, label=description)


# ══════════════════════════════════════════════════════════════════════
# Site-specific agents
# ══════════════════════════════════════════════════════════════════════

def web_youtube_search(query: str) -> dict:
    """
    Search YouTube for a query.

    Args:
        query: What to search for on YouTube.

    Returns:
        {"success": bool, "message": str}
    """
    encoded = urllib.parse.quote_plus(query)
    return browser_navigate_and_wait(f"https://www.youtube.com/results?search_query={encoded}")


def web_youtube_play_first() -> dict:
    """Play the first video from YouTube search results."""
    js = '''
    (function() {
        var vid = document.querySelector('#contents ytd-video-renderer a#video-title, a#video-title');
        if (vid) { vid.click(); return 'playing: ' + (vid.title || vid.innerText).substring(0, 60); }
        return 'NOT_FOUND';
    })()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": "[-] No video found in search results"}
    return {"success": True, "message": result.get("value", "")}


def web_ecommerce_search(product: str, site: str = "amazon.com") -> dict:
    """
    Search for a product on an e-commerce site.
    Navigate to the site and search. Does NOT purchase.

    Args:
        product: Product to search for.
        site:    E-commerce site (default: amazon.com).

    Returns:
        {"success": bool, "message": str}
    """
    encoded = urllib.parse.quote_plus(product)
    if "amazon" in site.lower():
        url = f"https://www.amazon.com/s?k={encoded}"
    elif "flipkart" in site.lower():
        url = f"https://www.flipkart.com/search?q={encoded}"
    else:
        url = f"https://{site}/search?q={encoded}"

    return browser_navigate_and_wait(url)
