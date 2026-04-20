"""
DEMASCAS — actuators/browser.py
Complete Brave Browser automation via JavaScript injection through AppleScript.

PRIMARY: JS injection via `tell application "Brave Browser" to execute ... javascript`
SECONDARY (fallback): macOS accessibility tree via System Events

Hard constraints:
  - App name is always "Brave Browser" (never just "Brave")
  - All functions return {"success": bool, "message": str} or simple strings
  - JS must dispatch both 'input' AND 'change' events for React compatibility
  - contenteditable elements use execCommand, not value setter
  - Never auto-send emails — always draft-only
"""

import subprocess
import json
import time
import re

_TIMEOUT = 10
_APP_NAME = "Brave Browser"


# ══════════════════════════════════════════════════════════════════════
# Low-level JS execution
# ══════════════════════════════════════════════════════════════════════

def _sanitize_js(js: str) -> str:
    """Escape JS for embedding inside AppleScript double-quoted string."""
    return js.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def browser_execute_js(js_code: str) -> dict:
    """
    Execute arbitrary JavaScript in the active Brave Browser tab.
    Returns the JS return value as a string.

    Args:
        js_code: JavaScript code to execute. Must return a value.

    Returns:
        {"success": bool, "message": str, "value": str}
    """
    safe_js = _sanitize_js(js_code)
    applescript = f'''
    tell application "{_APP_NAME}"
        set jsResult to execute active tab of window 1 javascript "{safe_js}"
        return jsResult
    end tell
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True,
            timeout=_TIMEOUT,
        )
        output = result.stdout.strip()
        return {"success": True, "message": output, "value": output}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "[-] JS execution timed out"}
    except subprocess.CalledProcessError as e:
        return {"success": False, "message": f"[-] JS error: {e.stderr.strip()}"}


# ══════════════════════════════════════════════════════════════════════
# Tab & navigation management
# ══════════════════════════════════════════════════════════════════════

def browser_get_url() -> dict:
    """Get the URL of the active tab."""
    applescript = f'''
    tell application "{_APP_NAME}"
        return URL of active tab of window 1
    end tell
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        url = result.stdout.strip()
        return {"success": True, "message": url, "url": url}
    except Exception as e:
        return {"success": False, "message": f"[-] Could not get URL: {e}"}


def browser_get_title() -> dict:
    """Get the title of the active tab."""
    applescript = f'''
    tell application "{_APP_NAME}"
        return title of active tab of window 1
    end tell
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        title = result.stdout.strip()
        return {"success": True, "message": title, "title": title}
    except Exception as e:
        return {"success": False, "message": f"[-] Could not get title: {e}"}


def browser_navigate(url: str) -> dict:
    """
    Navigate the active tab to a URL.

    Args:
        url: The URL to navigate to. https:// is added if missing.

    Returns:
        {"success": bool, "message": str}
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    applescript = f'''
    tell application "{_APP_NAME}"
        tell active tab of window 1
            set URL to "{url}"
        end tell
    end tell
    '''
    try:
        subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        time.sleep(1.5)  # wait for navigation to initiate
        return {"success": True, "message": f"Navigated to {url}"}
    except Exception as e:
        return {"success": False, "message": f"[-] Navigation failed: {e}"}


def browser_navigate_and_wait(url: str, wait_seconds: float = 3.0) -> dict:
    """
    Navigate to URL and wait for page load.

    Args:
        url: The URL to navigate to.
        wait_seconds: Seconds to wait for page to load (default 3.0).

    Returns:
        {"success": bool, "message": str}
    """
    result = browser_navigate(url)
    if not result["success"]:
        return result

    # Wait for document.readyState == "complete"
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        check = browser_execute_js("document.readyState")
        if check.get("value") == "complete":
            return {"success": True, "message": f"Navigated to {url} (page loaded)"}
        time.sleep(0.5)

    return {"success": True, "message": f"Navigated to {url} (waited {wait_seconds}s)"}


def browser_new_tab(url: str = "") -> dict:
    """
    Open a new tab, optionally navigating to a URL.

    Args:
        url: Optional URL to open in the new tab.

    Returns:
        {"success": bool, "message": str}
    """
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url

    if url:
        applescript = f'''
        tell application "{_APP_NAME}"
            tell window 1
                set newTab to make new tab with properties {{URL:"{url}"}}
                set active tab index to (count of tabs)
            end tell
        end tell
        '''
    else:
        applescript = f'''
        tell application "{_APP_NAME}"
            tell window 1
                set newTab to make new tab
                set active tab index to (count of tabs)
            end tell
        end tell
        '''
    try:
        subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        return {"success": True, "message": f"Opened new tab{' to ' + url if url else ''}"}
    except Exception as e:
        return {"success": False, "message": f"[-] New tab failed: {e}"}


def browser_close_tab() -> dict:
    """Close the active tab in Brave Browser."""
    applescript = f'''
    tell application "{_APP_NAME}"
        tell window 1
            close active tab
        end tell
    end tell
    '''
    try:
        subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        return {"success": True, "message": "Closed active tab"}
    except Exception as e:
        return {"success": False, "message": f"[-] Close tab failed: {e}"}


def browser_list_tabs() -> dict:
    """List all open tabs in the current window with their titles and URLs."""
    applescript = f'''
    tell application "{_APP_NAME}"
        set tabList to ""
        tell window 1
            set tabCount to count of tabs
            repeat with i from 1 to tabCount
                set t to tab i
                set tabList to tabList & i & ". " & (title of t) & " | " & (URL of t) & "\\n"
            end repeat
        end tell
        return tabList
    end tell
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        tabs = result.stdout.strip()
        return {"success": True, "message": tabs}
    except Exception as e:
        return {"success": False, "message": f"[-] List tabs failed: {e}"}


def browser_switch_to_tab(tab_index: int) -> dict:
    """
    Switch to a specific tab by index (1-based).

    Args:
        tab_index: 1-based index of the tab to switch to.

    Returns:
        {"success": bool, "message": str}
    """
    applescript = f'''
    tell application "{_APP_NAME}"
        tell window 1
            set active tab index to {tab_index}
        end tell
    end tell
    '''
    try:
        subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, check=True, timeout=_TIMEOUT,
        )
        return {"success": True, "message": f"Switched to tab {tab_index}"}
    except Exception as e:
        return {"success": False, "message": f"[-] Switch tab failed: {e}"}


def browser_back() -> dict:
    """Navigate back in browser history."""
    return browser_execute_js("history.back(); 'went back'")


def browser_forward() -> dict:
    """Navigate forward in browser history."""
    return browser_execute_js("history.forward(); 'went forward'")


def browser_reload() -> dict:
    """Reload the current page."""
    return browser_execute_js("location.reload(); 'reloading'")


# ══════════════════════════════════════════════════════════════════════
# Page content extraction
# ══════════════════════════════════════════════════════════════════════

def browser_get_page_text(max_chars: int = 5000) -> dict:
    """
    Extract the visible text content of the current page.
    Strips scripts, styles, and navigation chrome.

    Args:
        max_chars: Maximum characters to return (default 5000).

    Returns:
        {"success": bool, "message": str}
    """
    js = f'''
    (function() {{
        var body = document.body.cloneNode(true);
        var remove = body.querySelectorAll('script,style,nav,header,footer,noscript,iframe');
        remove.forEach(function(el) {{ el.remove(); }});
        var text = body.innerText || body.textContent || '';
        text = text.replace(/\\s+/g, ' ').trim();
        return text.substring(0, {max_chars});
    }})()
    '''
    return browser_execute_js(js)


def browser_get_page_html(max_chars: int = 10000) -> dict:
    """
    Get the raw HTML of the current page (truncated).

    Args:
        max_chars: Maximum characters to return.

    Returns:
        {"success": bool, "message": str}
    """
    js = f"document.documentElement.outerHTML.substring(0, {max_chars})"
    return browser_execute_js(js)


def browser_get_links(max_links: int = 20) -> dict:
    """
    Get all links on the current page.

    Args:
        max_links: Maximum number of links to return.

    Returns:
        {"success": bool, "message": str} — message is JSON array of {text, href}
    """
    js = f'''
    (function() {{
        var links = Array.from(document.querySelectorAll('a[href]'));
        var result = links.slice(0, {max_links}).map(function(a) {{
            return {{ text: (a.innerText || '').trim().substring(0, 80), href: a.href }};
        }}).filter(function(l) {{ return l.text.length > 0; }});
        return JSON.stringify(result);
    }})()
    '''
    return browser_execute_js(js)


def browser_get_inputs() -> dict:
    """
    List all input fields, textareas, and selects on the page.
    Returns JSON with name, type, placeholder, value, and a stable index.
    """
    js = '''
    (function() {
        var inputs = Array.from(document.querySelectorAll('input,textarea,select,[contenteditable=true]'));
        var result = inputs.slice(0, 30).map(function(el, i) {
            return {
                index: i,
                tag: el.tagName.toLowerCase(),
                type: el.type || el.getAttribute('contenteditable') || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                value: (el.value || el.innerText || '').substring(0, 100),
                ariaLabel: el.getAttribute('aria-label') || ''
            };
        });
        return JSON.stringify(result);
    })()
    '''
    return browser_execute_js(js)


# ══════════════════════════════════════════════════════════════════════
# Clicking elements (JS primary, accessibility fallback)
# ══════════════════════════════════════════════════════════════════════

def browser_click(selector: str = None) -> dict:
    """
    Click an element by CSS selector using JavaScript.
    Dispatches focus, mousedown, mouseup, and click events for maximum
    compatibility with modern frameworks (React, Angular, Vue).

    If the selector doesn't look like CSS (no . # [ > : chars), it is
    treated as visible text and auto-redirected to browser_click_text.

    Args:
        selector: CSS selector for the element to click, or visible text.

    Returns:
        {"success": bool, "message": str}
    """
    # ── Null guard ───────────────────────────────────────────────
    if not selector:
        return {
            "success": False,
            "message": "[-] No selector provided. Use browser_click_text for "
                       "clicking by visible text, or provide a CSS selector "
                       "like '#submit' or '.btn-primary'."
        }

    # ── Smart redirect: non-CSS text → browser_click_text ────────
    _CSS_CHARS = set('.#[]>:~+')
    if not any(c in selector for c in _CSS_CHARS):
        # Strip common LLM-generated suffixes: compose_button → compose
        clean = re.sub(
            r'[_-](button|btn|field|bar|input|link|tab|icon)$',
            '', selector, flags=re.IGNORECASE
        )
        clean = clean.replace('_', ' ').replace('-', ' ').strip()
        if clean:
            return browser_click_text(clean)

    safe_sel = selector.replace("'", "\\'")
    js = f'''
    (function() {{
        var el = document.querySelector('{safe_sel}');
        if (!el) return 'NOT_FOUND';
        el.focus();
        el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
        el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
        el.click();
        return 'clicked: ' + (el.innerText || el.tagName).substring(0, 50);
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] Element not found: {selector}"}
    return result


def browser_click_text(text: str = None) -> dict:
    """
    Click an element by its visible text content.
    Searches buttons, links, and clickable elements.

    Args:
        text: The visible text of the element to click.

    Returns:
        {"success": bool, "message": str}
    """
    # ── Null guard ───────────────────────────────────────────────
    if not text:
        return {
            "success": False,
            "message": "[-] No text provided. Specify the visible text of the "
                       "element to click (e.g. 'Compose', 'Sign In', 'Submit')."
        }

    # Strip common LLM-generated suffixes: compose_button → compose
    text = re.sub(
        r'[_-](button|btn|field|bar|input|link|tab|icon)$',
        '', text, flags=re.IGNORECASE
    ).replace('_', ' ').replace('-', ' ').strip() or text

    safe_text = text.replace("'", "\\'").replace('"', '\\"')
    js = f'''
    (function() {{
        var target = '{safe_text}'.toLowerCase();
        var candidates = document.querySelectorAll('button,a,[role=button],[onclick],input[type=button],input[type=submit]');
        for (var i = 0; i < candidates.length; i++) {{
            var el = candidates[i];
            var elText = (el.innerText || el.value || el.getAttribute('aria-label') || '').toLowerCase().trim();
            if (elText === target || elText.includes(target)) {{
                el.focus();
                el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                el.click();
                return 'clicked: ' + (el.innerText || el.tagName).substring(0, 50);
            }}
        }}
        return 'NOT_FOUND';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] No clickable element with text '{text}' found"}
    return result


def browser_click_xpath(xpath: str) -> dict:
    """
    Click an element by XPath expression.

    Args:
        xpath: XPath expression to locate the element.

    Returns:
        {"success": bool, "message": str}
    """
    safe_xpath = xpath.replace("'", "\\'").replace('"', '\\"')
    js = f'''
    (function() {{
        var result = document.evaluate('{safe_xpath}', document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        var el = result.singleNodeValue;
        if (!el) return 'NOT_FOUND';
        el.focus();
        el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
        el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
        el.click();
        return 'clicked: ' + (el.innerText || el.tagName).substring(0, 50);
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] XPath not found: {xpath}"}
    return result


# ══════════════════════════════════════════════════════════════════════
# Typing into elements (React-compatible with event dispatch)
# ══════════════════════════════════════════════════════════════════════

def browser_type(selector: str = None, text: str = None, clear_first: bool = True) -> dict:
    """
    Type text into an element by CSS selector.
    Dispatches 'input' AND 'change' events for React/Angular compatibility.
    Handles contenteditable elements using execCommand.

    Args:
        selector:    CSS selector for the input element.
        text:        Text to type.
        clear_first: Clear existing content before typing (default True).

    Returns:
        {"success": bool, "message": str}
    """
    # ── Null guards ──────────────────────────────────────────────
    if not selector:
        return {
            "success": False,
            "message": "[-] No selector provided. Use a CSS selector like "
                       "'input[name=q]' or '#search-box', or use "
                       "browser_type_by_label to type by visible label."
        }
    if text is None:
        return {
            "success": False,
            "message": "[-] No text provided. Specify the text to type."
        }

    safe_sel = selector.replace("'", "\\'")
    safe_text = text.replace("'", "\\'").replace("\\", "\\\\")
    js = f'''
    (function() {{
        var el = document.querySelector('{safe_sel}');
        if (!el) return 'NOT_FOUND';
        el.focus();
        if (el.getAttribute('contenteditable') === 'true' || el.isContentEditable) {{
            if ({str(clear_first).lower()}) {{
                el.innerHTML = '';
            }}
            document.execCommand('insertText', false, '{safe_text}');
        }} else {{
            if ({str(clear_first).lower()}) {{
                el.value = '';
            }}
            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            );
            if (!nativeInputValueSetter) {{
                nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                );
            }}
            if (nativeInputValueSetter) {{
                nativeInputValueSetter.set.call(el, '{safe_text}');
            }} else {{
                el.value = '{safe_text}';
            }}
        }}
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'typed into: ' + (el.name || el.id || el.tagName);
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] Input element not found: {selector}"}
    return result


def browser_type_by_label(label_or_placeholder: str, text: str,
                          clear_first: bool = True) -> dict:
    """
    Type into an input field by its visible label, placeholder, or aria-label.

    Args:
        label_or_placeholder: The visible label, placeholder, or aria-label text.
        text:                 Text to type.
        clear_first:          Clear existing content first (default True).

    Returns:
        {"success": bool, "message": str}
    """
    safe_label = label_or_placeholder.replace("'", "\\'")
    safe_text = text.replace("'", "\\'").replace("\\", "\\\\")
    js = f'''
    (function() {{
        var target = '{safe_label}'.toLowerCase();
        var inputs = document.querySelectorAll('input,textarea,select,[contenteditable=true]');
        for (var i = 0; i < inputs.length; i++) {{
            var el = inputs[i];
            var ph = (el.placeholder || '').toLowerCase();
            var aria = (el.getAttribute('aria-label') || '').toLowerCase();
            var nm = (el.name || '').toLowerCase();
            var id = (el.id || '').toLowerCase();
            if (ph.includes(target) || aria.includes(target) || nm === target || id === target) {{
                el.focus();
                if (el.isContentEditable) {{
                    if ({str(clear_first).lower()}) el.innerHTML = '';
                    document.execCommand('insertText', false, '{safe_text}');
                }} else {{
                    if ({str(clear_first).lower()}) el.value = '';
                    var setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ) || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    );
                    if (setter) setter.set.call(el, '{safe_text}');
                    else el.value = '{safe_text}';
                }}
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return 'typed into: ' + (el.name || el.placeholder || el.tagName);
            }}
        }}
        // Also check labels for associated inputs
        var labels = document.querySelectorAll('label');
        for (var j = 0; j < labels.length; j++) {{
            if ((labels[j].innerText || '').toLowerCase().includes(target)) {{
                var forAttr = labels[j].getAttribute('for');
                if (forAttr) {{
                    var el = document.getElementById(forAttr);
                    if (el) {{
                        el.focus();
                        if ({str(clear_first).lower()}) el.value = '';
                        el.value = '{safe_text}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return 'typed into: ' + (el.name || el.id || el.tagName);
                    }}
                }}
            }}
        }}
        return 'NOT_FOUND';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] Input with label '{label_or_placeholder}' not found"}
    return result


def browser_press_key(key: str, selector: str = "") -> dict:
    """
    Dispatch a keyboard event in the browser.

    Args:
        key:      Key name (e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown').
        selector: Optional CSS selector to target. If empty, targets active element.

    Returns:
        {"success": bool, "message": str}
    """
    safe_key = key.replace("'", "\\'")
    if selector:
        safe_sel = selector.replace("'", "\\'")
        target_code = f"document.querySelector('{safe_sel}')"
    else:
        target_code = "document.activeElement"

    js = f'''
    (function() {{
        var el = {target_code};
        if (!el) return 'NO_TARGET';
        el.dispatchEvent(new KeyboardEvent('keydown', {{key: '{safe_key}', bubbles: true}}));
        el.dispatchEvent(new KeyboardEvent('keypress', {{key: '{safe_key}', bubbles: true}}));
        el.dispatchEvent(new KeyboardEvent('keyup', {{key: '{safe_key}', bubbles: true}}));
        return 'pressed: {safe_key}';
    }})()
    '''
    return browser_execute_js(js)


# ══════════════════════════════════════════════════════════════════════
# Scrolling
# ══════════════════════════════════════════════════════════════════════

def browser_scroll(direction: str = "down", pixels: int = 500) -> dict:
    """
    Scroll the page in a direction.

    Args:
        direction: One of 'up', 'down', 'left', 'right', 'top', 'bottom'.
        pixels:    Pixels to scroll (default 500).

    Returns:
        {"success": bool, "message": str}
    """
    direction = direction.lower().strip()
    if direction == "down":
        js = f"window.scrollBy(0, {pixels}); 'scrolled down {pixels}px'"
    elif direction == "up":
        js = f"window.scrollBy(0, -{pixels}); 'scrolled up {pixels}px'"
    elif direction == "right":
        js = f"window.scrollBy({pixels}, 0); 'scrolled right {pixels}px'"
    elif direction == "left":
        js = f"window.scrollBy(-{pixels}, 0); 'scrolled left {pixels}px'"
    elif direction == "top":
        js = "window.scrollTo(0, 0); 'scrolled to top'"
    elif direction == "bottom":
        js = "window.scrollTo(0, document.body.scrollHeight); 'scrolled to bottom'"
    else:
        return {"success": False, "message": f"[-] Invalid direction: {direction}"}

    return browser_execute_js(js)


def browser_scroll_to_element(selector: str) -> dict:
    """
    Scroll an element into view.

    Args:
        selector: CSS selector of the element to scroll to.

    Returns:
        {"success": bool, "message": str}
    """
    safe_sel = selector.replace("'", "\\'")
    js = f'''
    (function() {{
        var el = document.querySelector('{safe_sel}');
        if (!el) return 'NOT_FOUND';
        el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
        return 'scrolled to: ' + el.tagName;
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] Element not found: {selector}"}
    return result


# ══════════════════════════════════════════════════════════════════════
# Form interaction
# ══════════════════════════════════════════════════════════════════════

def browser_fill_form(field_values: str) -> dict:
    """
    Fill multiple form fields at once.

    Args:
        field_values: JSON string mapping selector → value, e.g.
                      '{"#email": "user@example.com", "#password": "secret"}'

    Returns:
        {"success": bool, "message": str}
    """
    try:
        fields = json.loads(field_values) if isinstance(field_values, str) else field_values
    except json.JSONDecodeError:
        return {"success": False, "message": "[-] Invalid JSON for field_values"}

    results = []
    for selector, value in fields.items():
        r = browser_type(selector, str(value))
        results.append(f"{selector}: {'OK' if r['success'] else 'FAIL'}")

    return {"success": True, "message": "Form fill results: " + "; ".join(results)}


def browser_select_option(selector: str, value: str) -> dict:
    """
    Select an option in a <select> dropdown.

    Args:
        selector: CSS selector for the select element.
        value:    The option value or visible text to select.

    Returns:
        {"success": bool, "message": str}
    """
    safe_sel = selector.replace("'", "\\'")
    safe_val = value.replace("'", "\\'")
    js = f'''
    (function() {{
        var sel = document.querySelector('{safe_sel}');
        if (!sel) return 'NOT_FOUND';
        // Try by value first
        for (var i = 0; i < sel.options.length; i++) {{
            if (sel.options[i].value === '{safe_val}' || sel.options[i].text === '{safe_val}') {{
                sel.selectedIndex = i;
                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                return 'selected: ' + sel.options[i].text;
            }}
        }}
        return 'OPTION_NOT_FOUND';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") in ("NOT_FOUND", "OPTION_NOT_FOUND"):
        return {"success": False, "message": f"[-] Select or option not found: {selector} / {value}"}
    return result


def browser_check_checkbox(selector: str, check: bool = True) -> dict:
    """
    Check or uncheck a checkbox.

    Args:
        selector: CSS selector of the checkbox.
        check:    True to check, False to uncheck.

    Returns:
        {"success": bool, "message": str}
    """
    safe_sel = selector.replace("'", "\\'")
    js = f'''
    (function() {{
        var el = document.querySelector('{safe_sel}');
        if (!el) return 'NOT_FOUND';
        if (el.checked !== {str(check).lower()}) {{
            el.click();
        }}
        return 'checkbox ' + ({str(check).lower()} ? 'checked' : 'unchecked');
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] Checkbox not found: {selector}"}
    return result


def browser_submit_form(selector: str = "form") -> dict:
    """
    Submit a form element.

    Args:
        selector: CSS selector of the form (default: first form).

    Returns:
        {"success": bool, "message": str}
    """
    safe_sel = selector.replace("'", "\\'")
    js = f'''
    (function() {{
        var form = document.querySelector('{safe_sel}');
        if (!form) return 'NOT_FOUND';
        form.submit();
        return 'submitted form';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": f"[-] Form not found: {selector}"}
    return result


# ══════════════════════════════════════════════════════════════════════
# Wait / detection utilities
# ══════════════════════════════════════════════════════════════════════

def browser_wait_for_element(selector: str, timeout: int = 10) -> dict:
    """
    Wait for an element to appear on the page (polling).

    Args:
        selector: CSS selector to wait for.
        timeout:  Max seconds to wait.

    Returns:
        {"success": bool, "message": str}
    """
    safe_sel = selector.replace("'", "\\'")
    deadline = time.time() + timeout
    while time.time() < deadline:
        js = f"document.querySelector('{safe_sel}') ? 'FOUND' : 'WAITING'"
        result = browser_execute_js(js)
        if result.get("value") == "FOUND":
            return {"success": True, "message": f"Element found: {selector}"}
        time.sleep(0.5)

    return {"success": False, "message": f"[-] Timeout: Element '{selector}' not found after {timeout}s"}


def browser_wait_for_page_load(timeout: float = 10.0) -> dict:
    """
    Wait for the current page to finish loading.

    Args:
        timeout: Max seconds to wait.

    Returns:
        {"success": bool, "message": str}
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = browser_execute_js("document.readyState")
        if result.get("value") == "complete":
            return {"success": True, "message": "Page loaded"}
        time.sleep(0.5)

    return {"success": False, "message": f"[-] Page not fully loaded after {timeout}s"}


def browser_element_exists(selector: str) -> dict:
    """
    Check if an element exists on the page.

    Args:
        selector: CSS selector to check.

    Returns:
        {"success": True, "message": "exists" or "not_found"}
    """
    safe_sel = selector.replace("'", "\\'")
    result = browser_execute_js(f"document.querySelector('{safe_sel}') ? 'exists' : 'not_found'")
    return {"success": True, "message": result.get("value", "error")}


# ══════════════════════════════════════════════════════════════════════
# Screenshot & DOM intelligence
# ══════════════════════════════════════════════════════════════════════

def browser_get_dom_summary(max_elements: int = 50) -> dict:
    """
    Get a structured summary of interactive elements on the page.
    Returns buttons, links, inputs, and other actionable elements.
    Filters hidden/off-screen elements. Output emphasizes visible text
    usable by browser_click_text.

    Args:
        max_elements: Maximum elements to include.

    Returns:
        {"success": bool, "message": str} — message is the DOM summary text
    """
    js = f'''
    (function() {{
        var result = [];
        var selectors = 'button,a[href],input,textarea,select,[role=button],[role=link],[role=tab],[role=menuitem],[contenteditable=true]';
        var elements = document.querySelectorAll(selectors);
        var count = 0;
        for (var i = 0; i < elements.length && count < {max_elements}; i++) {{
            var el = elements[i];
            // Skip hidden/off-screen elements
            if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;
            var style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
            var tag = el.tagName.toLowerCase();
            var text = (el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || '').trim().substring(0, 60);
            var type = el.type || el.getAttribute('role') || '';
            var id = el.id || '';
            var name = el.name || '';
            if (text || id || name) {{
                // Format: tag[type]: "visible text" (id/name)
                var line = tag + (type ? '[' + type + ']' : '') + ': ';
                if (text) line += '"' + text + '"';
                if (id) line += ' id=' + id;
                if (name && name !== id) line += ' name=' + name;
                result.push(line);
                count++;
            }}
        }}
        return 'Interactive elements (' + count + '):\\n' + result.join('\\n');
    }})()
    '''
    return browser_execute_js(js)


# ══════════════════════════════════════════════════════════════════════
# Gmail-specific helpers (draft-only, never auto-send)
# ══════════════════════════════════════════════════════════════════════

def gmail_compose() -> dict:
    """Open the Gmail compose window by clicking the Compose button."""
    # Try JS first (more reliable in Gmail's complex DOM)
    js = '''
    (function() {
        // Gmail compose button has class T-I T-I-KE L3
        var btn = document.querySelector('div.T-I.T-I-KE.L3');
        if (btn) { btn.click(); return 'compose opened'; }
        // Fallback: search by aria-label
        btn = document.querySelector('[aria-label*="Compose"]');
        if (btn) { btn.click(); return 'compose opened'; }
        // Fallback: search by text
        var divs = document.querySelectorAll('div[role=button]');
        for (var i = 0; i < divs.length; i++) {
            if ((divs[i].innerText || '').trim() === 'Compose') {
                divs[i].click();
                return 'compose opened';
            }
        }
        return 'NOT_FOUND';
    })()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": "[-] Could not find Gmail compose button"}
    return {"success": True, "message": "Gmail compose window opened"}


def gmail_fill_to(recipient: str) -> dict:
    """
    Fill the To field in a Gmail compose window.
    DOES NOT SEND — only fills the recipient.

    Args:
        recipient: Email address or name.

    Returns:
        {"success": bool, "message": str}
    """
    safe_recip = recipient.replace("'", "\\'")
    js = f'''
    (function() {{
        // Gmail To field is an input inside a div with aria-label "To recipients"
        var toField = document.querySelector('input[aria-label="To recipients"]')
            || document.querySelector('input[name="to"]')
            || document.querySelector('textarea[name="to"]');
        if (!toField) return 'NOT_FOUND';
        toField.focus();
        toField.value = '{safe_recip}';
        toField.dispatchEvent(new Event('input', {{bubbles: true}}));
        toField.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'filled to: {safe_recip}';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": "[-] Gmail To field not found — is compose open?"}
    return {"success": True, "message": f"Filled To: {recipient}"}


def gmail_fill_subject(subject: str) -> dict:
    """
    Fill the Subject field in a Gmail compose window.

    Args:
        subject: Email subject text.

    Returns:
        {"success": bool, "message": str}
    """
    safe_subj = subject.replace("'", "\\'")
    js = f'''
    (function() {{
        var subj = document.querySelector('input[name="subjectbox"]')
            || document.querySelector('input[aria-label="Subject"]');
        if (!subj) return 'NOT_FOUND';
        subj.focus();
        subj.value = '{safe_subj}';
        subj.dispatchEvent(new Event('input', {{bubbles: true}}));
        subj.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'filled subject';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": "[-] Gmail Subject field not found"}
    return {"success": True, "message": f"Filled Subject: {subject}"}


def gmail_fill_body(body: str) -> dict:
    """
    Fill the email body in a Gmail compose window.
    Uses execCommand for contenteditable div.
    DOES NOT SEND — only fills the draft body.

    Args:
        body: Email body text.

    Returns:
        {"success": bool, "message": str}
    """
    safe_body = body.replace("'", "\\'").replace("\n", "\\n")
    js = f'''
    (function() {{
        var bodyDiv = document.querySelector('div[aria-label="Message Body"]')
            || document.querySelector('div[contenteditable=true][role="textbox"]')
            || document.querySelector('div.Am.Al.editable');
        if (!bodyDiv) return 'NOT_FOUND';
        bodyDiv.focus();
        bodyDiv.innerHTML = '';
        document.execCommand('insertText', false, '{safe_body}');
        bodyDiv.dispatchEvent(new Event('input', {{bubbles: true}}));
        return 'filled body';
    }})()
    '''
    result = browser_execute_js(js)
    if result.get("value") == "NOT_FOUND":
        return {"success": False, "message": "[-] Gmail body field not found"}
    return {"success": True, "message": "Filled email body (draft — NOT sent)"}


def gmail_compose_draft(to: str = "", subject: str = "", body: str = "") -> dict:
    """
    Open Gmail compose and fill all fields as a DRAFT.
    NEVER auto-sends. User must manually click Send.

    All arguments are optional — if 'to' is empty, opens a blank compose.

    Args:
        to:      Recipient email address (optional — opens blank compose if empty).
        subject: Email subject (optional).
        body:    Email body text (optional).

    Returns:
        {"success": bool, "message": str}
    """
    # Step 1: Open compose
    comp = gmail_compose()
    if not comp["success"]:
        return comp

    time.sleep(1.5)  # wait for compose window animation

    # Step 2: Fill To (if provided)
    if to:
        to_result = gmail_fill_to(to)
        if not to_result["success"]:
            return to_result

    # Step 3: Fill Subject (if provided)
    if subject:
        time.sleep(0.3)
        subj_result = gmail_fill_subject(subject)
        if not subj_result["success"]:
            return subj_result

    # Step 4: Fill Body (if provided)
    if body:
        time.sleep(0.3)
        body_result = gmail_fill_body(body)
        if not body_result["success"]:
            return body_result

    filled = []
    if to: filled.append(f"To={to}")
    if subject: filled.append(f"Subject={subject}")
    if body: filled.append("Body=filled")
    fields_str = ", ".join(filled) if filled else "blank compose"

    return {
        "success": True,
        "message": f"Gmail draft ready: {fields_str}. "
                   "Review and click Send manually."
    }


def gmail_read_email() -> dict:
    """
    Read the currently open email in Gmail.
    Extracts sender, subject, date, and body text.

    Returns:
        {"success": bool, "message": str}
    """
    js = '''
    (function() {
        // Subject
        var subj = document.querySelector('h2.hP') || document.querySelector('[data-legacy-thread-id] h2');
        var subject = subj ? subj.innerText : '(no subject)';

        // Sender — look for the most recent sender span
        var senderEl = document.querySelector('.gD');
        var sender = senderEl ? (senderEl.getAttribute('email') || senderEl.innerText) : '(unknown)';

        // Date
        var dateEl = document.querySelector('.g3');
        var date = dateEl ? dateEl.getAttribute('title') || dateEl.innerText : '';

        // Body — get the last (most recent) message body
        var bodies = document.querySelectorAll('.a3s.aiL');
        var body = '';
        if (bodies.length > 0) {
            body = bodies[bodies.length - 1].innerText.substring(0, 2000);
        }

        return JSON.stringify({
            sender: sender,
            subject: subject,
            date: date,
            body: body
        });
    })()
    '''
    result = browser_execute_js(js)
    if not result["success"]:
        return result
    try:
        data = json.loads(result["value"])
        msg = f"From: {data['sender']}\nSubject: {data['subject']}\nDate: {data['date']}\n\n{data['body']}"
        return {"success": True, "message": msg}
    except (json.JSONDecodeError, KeyError):
        return {"success": True, "message": result.get("value", "")}
