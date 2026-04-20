"""
DEMASCAS — actuators/browser_linux.py
Linux browser automation via Selenium/Playwright.

Replaces macOS AppleScript-based browser control with cross-platform solution.
Uses Playwright for modern browser automation (Chromium, Firefox).

Install dependencies:
  pip install playwright
  playwright install chromium

Usage:
  Same function signatures as browser.py (macOS version)
"""

import os
import time
from typing import Optional, Dict, Any, List

# Lazy import playwright
_playwright = None
_browser = None
_context = None
_page = None


def _get_playwright():
    """Lazy import and initialize Playwright."""
    global _playwright, _browser, _context, _page

    if _playwright is None:
        try:
            from playwright.sync_api import sync_playwright
            _playwright = sync_playwright().start()

            # Launch browser (headless=False for visible automation)
            _browser = _playwright.chromium.launch(headless=False)
            _context = _browser.new_context()
            _page = _context.new_page()
            _page.set_default_timeout(10000)  # 10 second timeout
        except Exception as e:
            print(f"[!] Playwright not available: {e}")
            print("    Install: pip install playwright && playwright install chromium")
            return None

    return _playwright


def _ensure_page():
    """Ensure we have an active page."""
    global _page, _context

    if _get_playwright() is None:
        return None

    if _page is None or _page.is_closed():
        _context = _browser.new_context()
        _page = _context.new_page()

    return _page


# ── Navigation ───────────────────────────────────────────────────────

def browser_navigate(url: str) -> str:
    """Navigate to a URL."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if not url.startswith("http"):
            url = "https://" + url
        page.goto(url)
        return f"[+] Navigated to {url}"
    except Exception as e:
        return f"[-] Navigation failed: {e}"


def browser_navigate_and_wait(url: str, wait_seconds: float = 3.0) -> str:
    """Navigate and wait for page load."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if not url.startswith("http"):
            url = "https://" + url
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(int(wait_seconds * 1000))
        return f"[+] Navigated to {url} and waited {wait_seconds}s"
    except Exception as e:
        return f"[-] Navigation failed: {e}"


def browser_new_tab(url: Optional[str] = None) -> str:
    """Open a new tab."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        new_page = _context.new_page()
        if url:
            if not url.startswith("http"):
                url = "https://" + url
            new_page.goto(url)
        return "[+] Opened new tab"
    except Exception as e:
        return f"[-] Failed to open new tab: {e}"


def browser_close_tab() -> str:
    """Close the current tab."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.close()
        # Switch to another tab if available
        pages = _context.pages
        if pages:
            _page = pages[-1]
        return "[+] Closed tab"
    except Exception as e:
        return f"[-] Failed to close tab: {e}"


def browser_list_tabs() -> str:
    """List all open tabs."""
    if _context is None:
        return "[-] Browser not available"

    try:
        pages = _context.pages
        if not pages:
            return "No open tabs"

        result = []
        for i, p in enumerate(pages):
            title = p.title()[:50] if p.title() else "Untitled"
            url = p.url[:60]
            result.append(f"  [{i + 1}] {title} - {url}")
        return "\n".join(result)
    except Exception as e:
        return f"[-] Error listing tabs: {e}"


def browser_switch_to_tab(tab_index: int) -> str:
    """Switch to a tab by index (1-based)."""
    if _context is None:
        return "[-] Browser not available"

    try:
        pages = _context.pages
        if tab_index < 1 or tab_index > len(pages):
            return f"[-] Invalid tab index: {tab_index} (max: {len(pages)})"

        _page = pages[tab_index - 1]
        _page.bring_to_front()
        return f"[+] Switched to tab {tab_index}"
    except Exception as e:
        return f"[-] Failed to switch tab: {e}"


def browser_back() -> str:
    """Navigate back in history."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.go_back()
        return "[+] Navigated back"
    except Exception as e:
        return f"[-] Go back failed: {e}"


def browser_forward() -> str:
    """Navigate forward in history."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.go_forward()
        return "[+] Navigated forward"
    except Exception as e:
        return f"[-] Go forward failed: {e}"


def browser_reload() -> str:
    """Reload the current page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.reload()
        return "[+] Reloaded page"
    except Exception as e:
        return f"[-] Reload failed: {e}"


# ── Content Extraction ───────────────────────────────────────────────

def browser_get_url() -> str:
    """Get the current URL."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    return page.url


def browser_get_title() -> str:
    """Get the current page title."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    return page.title() or "Untitled"


def browser_get_page_text(max_chars: int = 5000) -> str:
    """Extract visible text from the page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        text = page.content_text()
        return text[:max_chars]
    except Exception as e:
        return f"[-] Failed to get page text: {e}"


def browser_get_page_html(max_chars: int = 10000) -> str:
    """Get the page HTML."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        html = page.content()
        return html[:max_chars]
    except Exception as e:
        return f"[-] Failed to get HTML: {e}"


def browser_get_links(max_links: int = 20) -> str:
    """Get all links on the page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        links = page.query_selector_all("a[href]")
        result = []
        for i, link in enumerate(links[:max_links]):
            text = link.text_content()[:50].strip() or "[no text]"
            href = link.get_attribute("href")[:100]
            result.append(f"  [{i + 1}] {text} -> {href}")
        return "\n".join(result)
    except Exception as e:
        return f"[-] Failed to get links: {e}"


def browser_get_inputs() -> str:
    """List all form inputs on the page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        inputs = page.query_selector_all("input, textarea, select")
        result = []
        for i, inp in enumerate(inputs[:20]):
            tag = inp.tag_name.lower()
            inp_type = inp.get_attribute("type") or "text"
            name = inp.get_attribute("name") or ""
            placeholder = inp.get_attribute("placeholder") or ""
            label = f"{tag}:{inp_type}"
            if name:
                label += f" (name={name})"
            if placeholder:
                label += f" [{placeholder}]"
            result.append(f"  [{i + 1}] {label}")
        return "\n".join(result)
    except Exception as e:
        return f"[-] Failed to get inputs: {e}"


def browser_get_dom_summary(max_elements: int = 50) -> str:
    """Get a summary of interactive DOM elements."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        elements = page.query_selector_all(
            "button, a[href], input, textarea, select, [role='button'], [onclick]"
        )

        result = []
        for i, el in enumerate(elements[:max_elements]):
            tag = el.tag_name.lower()
            text = el.text_content()[:30].strip() if el.text_content() else ""
            role = el.get_attribute("role") or ""

            desc = f"[{i + 1}] <{tag}>"
            if text:
                desc += f" '{text}'"
            if role:
                desc += f" [role={role}]"
            result.append(desc)

        return "Interactive elements:\n" + "\n".join(result)
    except Exception as e:
        return f"[-] Failed to get DOM summary: {e}"


# ── Interaction ──────────────────────────────────────────────────────

def browser_click(selector: str) -> str:
    """Click an element by CSS selector."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        # If selector doesn't look like CSS, try text search
        if not any(c in selector for c in "#.>[]"):
            return browser_click_text(selector)

        page.click(selector)
        return f"[+] Clicked '{selector}'"
    except Exception as e:
        # Fallback to text search
        return browser_click_text(selector)


def browser_click_text(text: str) -> str:
    """Click an element by its visible text."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        # Try multiple strategies
        # 1. Button with text
        locator = page.get_by_text(text, exact=False)
        if locator.count() > 0:
            locator.first.click()
            return f"[+] Clicked text '{text}'"

        # 2. Link with text
        locator = page.get_by_role("link", name=text)
        if locator.count() > 0:
            locator.first.click()
            return f"[+] Clicked link '{text}'"

        # 3. Button with role
        locator = page.get_by_role("button", name=text)
        if locator.count() > 0:
            locator.first.click()
            return f"[+] Clicked button '{text}'"

        return f"[-] Element with text '{text}' not found"
    except Exception as e:
        return f"[-] Click failed: {e}"


def browser_click_xpath(xpath: str) -> str:
    """Click an element by XPath."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        element = page.query_selector(f"xpath={xpath}")
        if element:
            element.click()
            return f"[+] Clicked XPath '{xpath}'"
        return f"[-] Element not found for XPath '{xpath}'"
    except Exception as e:
        return f"[-] XPath click failed: {e}"


def browser_type(selector: str, text: str, clear_first: bool = True) -> str:
    """Type into an input field by CSS selector."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        locator = page.locator(selector)
        if clear_first:
            locator.fill("")
        locator.fill(text)
        return f"[+] Typed '{text[:30]}{'...' if len(text) > 30 else ''}'"
    except Exception as e:
        return f"[-] Type failed: {e}"


def browser_type_by_label(label_or_placeholder: str, text: str,
                          clear_first: bool = True) -> str:
    """Type into a field by its label or placeholder."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        # Try placeholder first
        locator = page.get_by_placeholder(label_or_placeholder)
        if locator.count() == 0:
            # Try label
            locator = page.get_by_label(label_or_placeholder)

        if locator.count() > 0:
            if clear_first:
                locator.first.fill("")
            locator.first.fill(text)
            return f"[+] Typed into '{label_or_placeholder}'"

        return f"[-] Field '{label_or_placeholder}' not found"
    except Exception as e:
        return f"[-] Type by label failed: {e}"


def browser_press_key(key: str, selector: Optional[str] = None) -> str:
    """Press a keyboard key."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if selector:
            page.locator(selector).press(key)
        else:
            page.keyboard.press(key)
        return f"[+] Pressed key '{key}'"
    except Exception as e:
        return f"[-] Key press failed: {e}"


def browser_scroll(direction: str, pixels: int = 500) -> str:
    """Scroll the page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        direction = direction.lower()
        if direction == "down":
            page.evaluate(f"window.scrollBy(0, {pixels})")
        elif direction == "up":
            page.evaluate(f"window.scrollBy(0, -{pixels})")
        elif direction == "left":
            page.evaluate(f"window.scrollBy(-{pixels}, 0)")
        elif direction == "right":
            page.evaluate(f"window.scrollBy({pixels}, 0)")
        elif direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            return f"[-] Invalid direction: {direction}"

        return f"[+] Scrolled {direction}"
    except Exception as e:
        return f"[-] Scroll failed: {e}"


def browser_scroll_to_element(selector: str) -> str:
    """Scroll to a specific element."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        element = page.locator(selector).first
        if element.count() > 0:
            element.scroll_into_view_if_needed()
            return f"[+] Scrolled to '{selector}'"
        return f"[-] Element '{selector}' not found"
    except Exception as e:
        return f"[-] Scroll to element failed: {e}"


# ── Form Handling ────────────────────────────────────────────────────

def browser_fill_form(field_values: str) -> str:
    """Fill multiple form fields. field_values is JSON string."""
    import json
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        values = json.loads(field_values)
        for selector, value in values.items():
            locator = page.locator(selector)
            if locator.count() > 0:
                locator.first.fill(str(value))
        return f"[+] Filled {len(values)} fields"
    except Exception as e:
        return f"[-] Fill form failed: {e}"


def browser_select_option(selector: str, value: str) -> str:
    """Select an option in a dropdown."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.select_option(selector, value)
        return f"[+] Selected '{value}'"
    except Exception as e:
        return f"[-] Select option failed: {e}"


def browser_check_checkbox(selector: str) -> str:
    """Check or uncheck a checkbox."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        locator = page.locator(selector)
        if locator.count() > 0:
            is_checked = locator.first.is_checked()
            if is_checked:
                locator.first.uncheck()
                return "[+] Unchecked"
            else:
                locator.first.check()
                return "[+] Checked"
        return f"[-] Checkbox '{selector}' not found"
    except Exception as e:
        return f"[-] Checkbox failed: {e}"


def browser_submit_form() -> str:
    """Submit the current form."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        # Try to find and click submit button
        submit = page.query_selector("button[type='submit'], input[type='submit']")
        if submit:
            submit.click()
            return "[+] Submitted form"

        # Try Enter key
        page.keyboard.press("Enter")
        return "[+] Submitted form (Enter key)"
    except Exception as e:
        return f"[-] Submit failed: {e}"


# ── Wait Helpers ─────────────────────────────────────────────────────

def browser_wait_for_element(selector: str, timeout: int = 10) -> str:
    """Wait for an element to appear."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.wait_for_selector(selector, timeout=int(timeout * 1000))
        return f"[+] Element '{selector}' found"
    except Exception as e:
        return f"[-] Element '{selector}' not found within {timeout}s"


def browser_wait_for_page_load(wait_seconds: float = 3.0) -> str:
    """Wait for page to fully load."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(int(wait_seconds * 1000))
        return "[+] Page loaded"
    except Exception as e:
        return f"[-] Wait failed: {e}"


def browser_element_exists(selector: str) -> str:
    """Check if an element exists."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        element = page.query_selector(selector)
        if element:
            return f"[+] Element '{selector}' exists"
        return f"[-] Element '{selector}' not found"
    except Exception as e:
        return f"[-] Check failed: {e}"


# ── JavaScript Execution ─────────────────────────────────────────────

def browser_execute_js(js_code: str) -> str:
    """Execute JavaScript code."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        result = page.evaluate(js_code)
        return f"[+] JS result: {result}"
    except Exception as e:
        return f"[-] JS execution failed: {e}"


# ── Gmail Specific ───────────────────────────────────────────────────

def gmail_compose_draft(to: str = "", subject: str = "", body: str = "") -> str:
    """Compose a Gmail draft."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        # Navigate to Gmail compose
        if "mail.google.com" not in page.url:
            page.goto("https://mail.google.com/mail/?view=cm#fs")
            page.wait_for_timeout(2000)

        # Fill fields
        if to:
            page.get_by_placeholder("To").fill(to)
        if subject:
            page.get_by_placeholder("Subject").fill(subject)
        if body:
            page.get_by_placeholder("Message").fill(body)

        return f"[+] Composed Gmail draft (To: {to or 'empty'}, Subject: {subject or 'empty'})"
    except Exception as e:
        return f"[-] Gmail compose failed: {e}"


def gmail_read_email() -> str:
    """Read the current Gmail email."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if "mail.google.com" not in page.url:
            return "[-] Not on Gmail"

        # Extract email content
        sender = page.query_selector(".ha .gD")
        subject = page.query_selector(".hP")
        body = page.query_selector(".a3s")

        result = []
        if sender:
            result.append(f"From: {sender.text_content()}")
        if subject:
            result.append(f"Subject: {subject.text_content()}")
        if body:
            result.append(f"Body: {body.text_content()[:500]}")

        return "\n".join(result) if result else "[-] Could not read email"
    except Exception as e:
        return f"[-] Gmail read failed: {e}"


# ── Web Agent Helpers ────────────────────────────────────────────────

def web_analyze_page() -> str:
    """Analyze the current page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        url = page.url
        title = page.title()

        # Count interactive elements
        buttons = page.query_selector_all("button, [role='button']")
        links = page.query_selector_all("a[href]")
        inputs = page.query_selector_all("input, textarea")

        return (
            f"Page Analysis:\n"
            f"  URL: {url}\n"
            f"  Title: {title}\n"
            f"  Buttons: {len(buttons)}\n"
            f"  Links: {len(links)}\n"
            f"  Inputs: {len(inputs)}\n"
            f"  Site type: {classify_site_type(url)}"
        )
    except Exception as e:
        return f"[-] Analysis failed: {e}"


def classify_site_type(url: str) -> str:
    """Classify the type of website."""
    url_lower = url.lower()

    if "mail.google.com" in url_lower or "outlook" in url_lower:
        return "email"
    if "youtube.com" in url_lower:
        return "video"
    if "amazon.com" in url_lower or "ebay" in url_lower:
        return "ecommerce"
    if "google.com" in url_lower or "search" in url_lower:
        return "search"
    if "wikipedia.org" in url_lower:
        return "docs"
    if "twitter.com" in url_lower or "facebook" in url_lower or "instagram" in url_lower:
        return "social"

    return "unknown"


def web_scan_for_obstacles() -> str:
    """Scan for cookie banners, popups, etc."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        obstacles = []

        # Check for cookie banners
        cookie_selectors = [
            "[class*='cookie']", "[id*='cookie']", "[class*='consent']",
            "[id*='consent']", "button[class*='accept']",
        ]
        for sel in cookie_selectors:
            if page.query_selector(sel):
                obstacles.append("cookie_banner")
                break

        # Check for login walls
        login_selectors = [
            "[class*='login']", "[id*='login']", "[class*='signin']",
            "button[class*='sign in']",
        ]
        for sel in login_selectors:
            if page.query_selector(sel):
                obstacles.append("login_wall")
                break

        if not obstacles:
            return "[+] No obstacles detected"

        return "Obstacles: " + ", ".join(obstacles)
    except Exception as e:
        return f"[-] Scan failed: {e}"


def web_handle_obstacle(obstacle_type: str = "") -> str:
    """Handle a detected obstacle."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if obstacle_type == "cookie_banner" or not obstacle_type:
            # Try to click accept cookies
            accept_buttons = [
                "button[class*='accept']", "button[class*='agree']",
                "[class*='cookie-accept']", "button:has-text('Accept')",
                "button:has-text('Agree')",
            ]
            for sel in accept_buttons:
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        page.wait_for_timeout(500)
                        return "[+] Dismissed cookie banner"
                except:
                    pass

        if obstacle_type == "popup_modal":
            # Try to close popup
            close_buttons = [
                "[class*='close']", "[class*='dismiss']", "button:has-text('Close')",
                "[aria-label*='close']",
            ]
            for sel in close_buttons:
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        btn.click()
                        return "[+] Dismissed popup"
                except:
                    pass

        return f"[-] Could not handle obstacle: {obstacle_type or 'unknown'}"
    except Exception as e:
        return f"[-] Handle obstacle failed: {e}"


def web_search(query: str) -> str:
    """Search Google and return results."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.goto(f"https://www.google.com/search?q={query.replace(' ', '+')}")
        page.wait_for_timeout(2000)

        # Extract results
        results = page.query_selector_all("#search .g")
        output = []
        for i, r in enumerate(results[:5]):
            title_el = r.query_selector("h3")
            link_el = r.query_selector("a")
            title = title_el.text_content()[:60] if title_el else "[no title]"
            link = link_el.get_attribute("href")[:80] if link_el else "[no link]"
            output.append(f"  [{i + 1}] {title}\n      {link}")

        return "Search results:\n" + "\n".join(output)
    except Exception as e:
        return f"[-] Search failed: {e}"


def web_read_article(url: str = "") -> str:
    """Read an article from URL or current page."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if url:
            if not url.startswith("http"):
                url = "https://" + url
            page.goto(url)
            page.wait_for_timeout(2000)

        # Extract main content (simplified)
        body = page.query_selector("article, main, .content, .post, .article")
        if body:
            text = body.text_content()[:2000]
            # Clean up whitespace
            text = " ".join(text.split())
            return f"Article content:\n{text}"

        # Fallback to body text
        text = page.query_selector("body").text_content()[:2000]
        return f"Page content:\n{text}"
    except Exception as e:
        return f"[-] Read article failed: {e}"


def web_research(topic: str, num_sources: int = 3) -> str:
    """Research a topic using multiple sources."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        # Search
        page.goto(f"https://www.google.com/search?q={topic.replace(' ', '+')}")
        page.wait_for_timeout(2000)

        results = page.query_selector_all("#search .g")
        summaries = []

        for i, r in enumerate(results[:num_sources]):
            link_el = r.query_selector("a")
            if link_el:
                url = link_el.get_attribute("href")
                # Visit and extract
                page.goto(url)
                page.wait_for_timeout(1500)

                content = page.query_selector("article, main, .content, p")
                if content:
                    text = content.text_content()[:500]
                    text = " ".join(text.split())
                    summaries.append(f"Source {i + 1} ({url[:50]}...):\n{text}")

                page.go_back()
                page.wait_for_timeout(1000)

        return "Research summary:\n\n" + "\n\n".join(summaries)
    except Exception as e:
        return f"[-] Research failed: {e}"


def web_execute_goal(goal: str) -> str:
    """Execute a high-level browsing goal."""
    return f"[-] Autonomous goal execution not fully implemented. Goal: {goal}"


def web_smart_click(element_description: str) -> str:
    """Smart click with multiple strategies."""
    # Try text first
    result = browser_click_text(element_description)
    if "[+]" in result:
        return result

    # Try CSS selector
    result = browser_click(element_description)
    if "[+]" in result:
        return result

    return f"[-] Could not click '{element_description}'"


def web_youtube_search(query: str) -> str:
    """Search YouTube."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        page.goto(f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}")
        page.wait_for_timeout(2000)

        videos = page.query_selector_all("ytd-video-renderer")
        output = []
        for i, v in enumerate(videos[:5]):
            title_el = v.query_selector("#video-title")
            title = title_el.get_attribute("title")[:60] if title_el else "[no title]"
            output.append(f"  [{i + 1}] {title}")

        return "YouTube results:\n" + "\n".join(output)
    except Exception as e:
        return f"[-] YouTube search failed: {e}"


def web_youtube_play_first() -> str:
    """Play the first YouTube video."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if "youtube.com" not in page.url:
            return "[-] Not on YouTube"

        first_video = page.query_selector("ytd-video-renderer #video-title")
        if first_video:
            first_video.click()
            page.wait_for_timeout(2000)
            return "[+] Playing first video"

        return "[-] No videos found"
    except Exception as e:
        return f"[-] Play video failed: {e}"


def web_ecommerce_search(product: str, site: str = "amazon") -> str:
    """Search for a product on an e-commerce site."""
    page = _ensure_page()
    if not page:
        return "[-] Browser not available"

    try:
        if site == "amazon":
            url = f"https://www.amazon.com/s?k={product.replace(' ', '+')}"
        elif site == "ebay":
            url = f"https://www.ebay.com/sch/i.html?_nkw={product.replace(' ', '+')}"
        else:
            url = f"https://www.google.com/search?q={site}+{product.replace(' ', '+')}"

        page.goto(url)
        page.wait_for_timeout(2000)

        # Extract product listings (simplified)
        products = page.query_selector_all("[data-component-type='s-search-result'], .s-item")
        output = []
        for i, p in enumerate(products[:5]):
            title_el = p.query_selector("h2, .s-item__title")
            title = title_el.text_content()[:60] if title_el else "[no title]"
            output.append(f"  [{i + 1}] {title}")

        return f"{site.capitalize()} results:\n" + "\n".join(output)
    except Exception as e:
        return f"[-] E-commerce search failed: {e}"


# ── Cleanup ──────────────────────────────────────────────────────────

def browser_cleanup():
    """Close browser and cleanup."""
    global _browser, _context, _page, _playwright

    if _browser:
        _browser.close()
    if _playwright:
        _playwright.stop()

    _browser = None
    _context = None
    _page = None
    _playwright = None
