"""
browser_agent.py - Brave Browser, Gmail, YouTube, web forms.
"""

import json
import re
from urllib.parse import quote
from .base_agent import AgentBase, AgentResult, StepRecord

URL_OPEN_PATTERN = (
    r"\b(open|go to|browse to|navigate to)\b.{0,80}"
    r"((?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+|page|website|site)"
    r"|\b(wikipedia|reddit|github|amazon|youtube)\b"
)


class BrowserAgent(AgentBase):
    domain_name = "browser"
    tool_names = [
        "open_url", "open_url_and_wait",
        "browser_get_url", "browser_get_title", "browser_get_page_text", "browser_get_dom_summary",
        "browser_navigate", "browser_navigate_and_wait", "browser_back", "browser_reload",
        "browser_click_text", "browser_type", "browser_type_by_label", "browser_scroll",
        "browser_fill_form", "browser_submit_form", "browser_wait_for_element",
        "gmail_compose_draft", "gmail_read_email",
        "web_smart_click", "web_scan_for_obstacles", "web_handle_obstacle",
        "web_search", "web_read_article", "web_youtube_search", "web_youtube_play_first",
        "search_web",
    ]

    @property
    def system_prompt(self) -> str:
        return (
            "You are the DEMASCAS Browser Agent, a specialist in Brave Browser on macOS.\n"
            "Rules:\n"
            "1. NAVIGATION IS ONE-SHOT. After open_url_and_wait or browser_navigate_and_wait succeeds, you are FORBIDDEN from calling any navigation tool again. Your ONLY valid next action is browser_get_dom_summary (to read page structure) or browser_get_page_text (to read page text). Calling another nav tool will be blocked and waste a step.\n"
            "2. Accessibility tree cannot fully see web content, so use browser_get_dom_summary to understand pages.\n"
            "3. For Gmail compose tasks, use gmail_compose_draft. Never auto-send emails.\n"
            "4. Before interacting with forms/buttons, scan for obstacles using web_scan_for_obstacles.\n"
            "5. Handle cookie banners/popups using web_handle_obstacle.\n"
            "6. For YouTube tasks, use web_youtube_search then web_youtube_play_first.\n"
            "7. If blocked by login wall or paywall, stop and clearly ask the user to proceed manually.\n"
            "8. Prefer browser_click_text when visible text is known.\n"
            "9. Prefer open_url_and_wait for navigation to ensure page load.\n"
            "10. Do not use browser_press_key in this agent.\n"
            "11. For form filling: inspect DOM first, map fields, fill values, and ask confirmation before submit.\n"
            "12. For table extraction: use browser_execute_js with document.querySelectorAll('table tr') and summarize rows."
        )

    def _extra_rules(self) -> str:
        return (
            "ABSOLUTE RULE: nav tools are one-shot. After first nav success -> read page. browser_get_dom_summary first, then browser_get_page_text if you need text.\n"
            "11. If current URL is mail.google.com -> you are in Gmail. Prioritize gmail_* tools.\n"
            "12. Use browser_scroll for page scrolling, not press_keyboard_shortcut.\n"
            "13. web_read_article is faster than browser_get_page_text for article content."
        )

    _EXCLUDE_RE = re.compile(
        r"\b(?:except(?:\s+for)?|other\s+than|instead\s+of|not\s+(?:on|from)|besides|"
        r"apart\s+from|rather\s+than|without)\s+(?:the\s+)?([a-z0-9][a-z0-9.-]*)",
        re.IGNORECASE,
    )
    _TOPIC_NOISE = re.compile(
        r"\b(can|could|would|will|you|please|i|want|wanted|meant|what|was|that|this|it|to|"
        r"show|me|open|display|find|bring|up|all|of|yours|your|the|a|an|some|information|info|"
        r"details|regarding|about|on|in|from|for|another|other|different|website|websites|"
        r"site|sites|page|pages|web|browser|theory|something|else|except|than|instead|"
        r"besides|apart|rather|without|alright|okay|so|well|just|my)\b",
        re.IGNORECASE,
    )

    def _topic_from(self, text: str) -> str:
        t = self._EXCLUDE_RE.sub(" ", text or "")
        t = self._TOPIC_NOISE.sub(" ", t)
        return re.sub(r"[^\w\s'-]", " ", re.sub(r"\s+", " ", t)).strip()

    def _alternative_site_flow(self, goal: str, excluded: str, context: dict) -> AgentResult:
        """'Show that on a site other than X' — search, skip X, open the next
        best result.  Deterministic: one search + one page load."""
        topic = self._topic_from(goal)
        if len(topic) < 3:
            topic = self._topic_from(str((context or {}).get("last_command", "")))
        if len(topic) < 3:
            return AgentResult(False, "Which topic should I look up?", 0, [], error="no_topic")
        res = self._call_tool("search_web", {"query": topic, "max_results": 8})
        urls = re.findall(r"URL:\s*(\S+)", res or "")
        excl = excluded.lower().split(".")[0]
        pick = next((u for u in urls if excl not in u.lower()), "")
        if not pick:
            return AgentResult(False, f"I couldn't find another site about {topic}.", 1,
                               ["search_web"], error=res[:200] if res else "no results")
        r = self._call_tool("open_url_and_wait",
                            {"url": pick, "browser": "Brave Browser", "wait_seconds": 3})
        if self._is_tool_error(r):
            return AgentResult(False, "I found a page but couldn't open it.", 2,
                               ["search_web", "open_url_and_wait"], error=r)
        host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", pick).split("/")[0])
        return AgentResult(True, f"Here's {topic.title()} on {host}.", 2,
                           ["search_web", "open_url_and_wait"])

    def run(self, goal: str, context: dict) -> AgentResult:
        goal_lower = (goal or "").lower()
        m = self._EXCLUDE_RE.search(goal or "")
        if m and re.search(r"\b(site|website|page|another|other|different|elsewhere)\b", goal_lower):
            return self._alternative_site_flow(goal, m.group(1), context)
        if any(k in goal_lower for k in ["open gmail", "gmail in browser", "open mail.google.com"]):
            return self._open_gmail_flow()
        if any(k in goal_lower for k in ["fill in the form", "fill the form", "fill form"]):
            return self._form_fill_flow(goal, context)
        if any(k in goal_lower for k in ["get all results", "go to next page", "next page", "load more"]):
            return self._pagination_flow(goal, context)
        if any(k in goal_lower for k in ["extract the data", "table say", "what does the table say", "extract table"]):
            return self._table_extract_flow(goal, context)
        if re.search(URL_OPEN_PATTERN, goal, re.IGNORECASE):
            return self._url_open_flow(goal, context)
        if any(k in goal_lower for k in [
            "compose email", "compose mail", "write email", "write mail",
            "draft email", "draft mail", "send email", "send mail",
            "email to", "mail to", "new email", "new mail",
        ]):
            return self._gmail_compose_flow(goal, context)
        if any(k in goal_lower for k in ["read email", "check email", "what did", "any emails", "inbox"]):
            return self._gmail_read_flow(goal, context)
        if any(k in goal_lower for k in ["youtube", "watch", "play video", "play music"]):
            return self._youtube_flow(goal, context)
        return self._execute(goal, context)

    def _open_gmail_flow(self) -> AgentResult:
        res = self._call_tool(
            "open_url_and_wait",
            {"url": "https://mail.google.com", "browser": "Brave Browser", "wait_seconds": 4},
        )
        if self._is_tool_error(res):
            return AgentResult(False, "I couldn't open Gmail in Brave.", 1, ["open_url_and_wait"], error=res)
        return AgentResult(True, "Opened Gmail in Brave Browser.", 1, ["open_url_and_wait"])

    def _call_tool(self, name: str, args: dict) -> str:
        # Browser domain should explicitly pin URL opens to Brave, not system defaults.
        patched_args = dict(args or {})
        if name in {"open_url", "open_url_and_wait"} and not patched_args.get("browser"):
            patched_args["browser"] = "Brave Browser"
        return super()._call_tool(name, patched_args)

    def _url_open_flow(self, goal: str, context: dict) -> AgentResult:
        """
        Handle commands like "open the X page for Y".
        If a direct URL is present, navigate first and continue in the regular loop.
        """
        base_context = context if isinstance(context, dict) else {}
        goal_lower = (goal or "").lower()

        def _extract_wikipedia_url(text: str) -> str:
            if "wikipedia" not in text.lower():
                return ""

            topic = ""
            m = re.search(r"wikipedia(?:\s+page)?\s+for\s+(.+?)(?:\s+(?:and|then)\b|$)", text, re.IGNORECASE)
            if m:
                topic = m.group(1).strip(" .,!?:;")

            if not topic:
                m = re.search(r"open\s+the\s+(.+?)\s+wikipedia\s+page", text, re.IGNORECASE)
                if m:
                    topic = m.group(1).strip(" .,!?:;")

            if not topic:
                return "https://en.wikipedia.org"

            normalized = quote(topic.replace(" ", "_"))
            return f"https://en.wikipedia.org/wiki/{normalized}"

        target_url = ""

        url_match = re.search(r"https?://\S+", goal)
        if url_match:
            target_url = url_match.group(0).rstrip(",.;:!?)")
        elif "wikipedia" in goal_lower:
            target_url = _extract_wikipedia_url(goal)
        else:
            domain_match = re.search(
                r"\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+\b",
                goal_lower,
                re.IGNORECASE,
            )
            if domain_match:
                target_url = domain_match.group(0)

        if target_url:
            print(f"[STEP] browser #1 open_url_and_wait({{'url': '{target_url}'}})")
            r = self._call_tool(
                "open_url_and_wait",
                {"url": target_url, "browser": "Brave Browser", "wait_seconds": 3},
            )
            print(f"[RESULT] browser #1 open_url_and_wait {'OK' if not self._is_tool_error(r) else 'FAIL'} {r[:180].replace(chr(10), ' ')}")
            if not self._is_tool_error(r):
                one_shot_open = (
                    any(k in goal_lower for k in ["open", "go to", "browse to", "navigate to"])
                    and not any(
                        k in goal_lower
                        for k in [
                            " then ", " and then ", "after ", "click", "type", "search",
                            "read", "extract", "summarize", "summarise", "fill", "play", "watch",
                            "what is", "who is", "when was", "where is", "why",
                        ]
                    )
                )
                if one_shot_open:
                    return AgentResult(
                        True,
                        f"Opened {target_url} in Brave Browser.",
                        1,
                        ["open_url_and_wait"],
                    )

                if any(k in goal_lower for k in ["birth year", "born", "when was"]):
                    print("[STEP] browser #2 web_read_article({})")
                    article = self._call_tool("web_read_article", {})
                    print(f"[RESULT] browser #2 web_read_article {'OK' if not self._is_tool_error(article) else 'FAIL'} {article[:180].replace(chr(10), ' ')}")
                    if not self._is_tool_error(article):
                        year_match = re.search(r"born[^\d]{0,30}(\d{4})", article, re.IGNORECASE)
                        if not year_match:
                            year_match = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", article)
                        if year_match:
                            year = year_match.group(1)
                            print(f"[DONE] browser step=2 summary=The birth year is {year}.")
                            return AgentResult(
                                True,
                                f"The birth year is {year}.",
                                2,
                                ["open_url_and_wait", "web_read_article"],
                            )

                return self._execute(
                    goal,
                    {**base_context, "active_url": target_url, "active_app": "Brave Browser"},
                )

        ctx = {
            **base_context,
            "active_app": "Brave Browser",
            "task_hint": (
                "The user wants to open a web page. "
                "Call open_url_and_wait with the most likely URL. "
                "For Wikipedia: https://en.wikipedia.org/wiki/TOPIC. "
                "For Google search: https://www.google.com/search?q=QUERY. "
                "Then use browser_get_dom_summary to read the page. "
                "Do not call open_url_and_wait more than once unless navigation failed."
            ),
        }
        return self._execute(goal, ctx)

    def _gmail_compose_flow(self, goal, context):
        goal_text = (goal or "")
        goal_lower = goal_text.lower()

        to_email = self._extract_email(goal_text)
        subject = self._extract_subject(goal_text)
        body = self._extract_body(goal_text)

        needs_recipient = any(k in goal_lower for k in ["send", "to ", "compose", "draft", "write"])
        if not to_email and needs_recipient:
            return AgentResult(
                False,
                "What's the recipient email address for this draft?",
                0,
                [],
                error="missing_recipient",
            )

        active_url = str((context or {}).get("active_url", ""))
        tools = []

        if "mail.google.com" not in active_url:
            nav = self._call_tool("open_url_and_wait", {"url": "https://mail.google.com", "wait_seconds": 3})
            tools.append("open_url_and_wait")
            if self._is_tool_error(nav):
                return AgentResult(False, "I couldn't open Gmail right now.", len(tools), tools, error=nav)

        draft = self._call_tool(
            "gmail_compose_draft",
            {
                "to": to_email,
                "subject": subject,
                "body": body,
            },
        )
        tools.append("gmail_compose_draft")
        if self._is_tool_error(draft):
            return AgentResult(
                False,
                "I opened Gmail but couldn't prepare the draft automatically.",
                len(tools),
                tools,
                error=draft,
            )

        who = to_email if to_email else "the recipient"
        if subject:
            summary = f"Draft ready in Gmail to {who} with subject '{subject}'."
        else:
            summary = f"Draft ready in Gmail to {who}."
        return AgentResult(True, summary, len(tools), tools)

    def _gmail_read_flow(self, goal, context):
        active_url = str((context or {}).get("active_url", ""))
        tools = []
        if "mail.google.com" not in active_url:
            nav = self._call_tool("open_url_and_wait", {"url": "https://mail.google.com", "wait_seconds": 3})
            tools.append("open_url_and_wait")
            if self._is_tool_error(nav):
                return AgentResult(False, "I couldn't open Gmail to check your inbox.", len(tools), tools, error=nav)

        read_res = self._call_tool("gmail_read_email", {})
        tools.append("gmail_read_email")
        if self._is_tool_error(read_res):
            return AgentResult(False, "I couldn't read the current Gmail message.", len(tools), tools, error=read_res)

        msg = (read_res or "").strip()
        if len(msg) > 380:
            msg = msg[:380].rstrip() + "..."
        return AgentResult(True, msg or "Opened Gmail inbox.", len(tools), tools)

    def _extract_email(self, text: str) -> str:
        m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
        return m.group(0) if m else ""

    def _extract_subject(self, text: str) -> str:
        src = text or ""
        patterns = [
            r"\bsubject\s*(?:is|:)?\s*['\"]?([^'\"\n]+)",
            r"\babout\s+['\"]?([^'\"\n]+)",
        ]
        for pat in patterns:
            m = re.search(pat, src, re.IGNORECASE)
            if m:
                value = m.group(1).strip(" .,!?:;")
                if value:
                    return value
        return ""

    def _extract_body(self, text: str) -> str:
        src = text or ""
        patterns = [
            r"\b(body|message|saying|that says)\s*(?:is|:)?\s*['\"]?(.+)$",
        ]
        for pat in patterns:
            m = re.search(pat, src, re.IGNORECASE)
            if m:
                value = m.group(2).strip(" \"'")
                if value:
                    return value
        return ""

    def _youtube_flow(self, goal, context):
        text = (goal or "").strip()
        query = re.sub(r"\b(play|watch|search for|youtube)\b", "", text, flags=re.IGNORECASE).strip(" :,-")
        if not query:
            query = "latest videos"

        s1 = self._call_tool("web_youtube_search", {"query": query})
        if self._is_tool_error(s1):
            return AgentResult(False, "I couldn't search YouTube right now.", 1, ["web_youtube_search"], error=s1)

        s2 = self._call_tool("web_youtube_play_first", {})
        if self._is_tool_error(s2):
            return AgentResult(False, "I found results but couldn't start playback.", 2, ["web_youtube_search", "web_youtube_play_first"], error=s2)

        return AgentResult(True, f"Playing '{query}' on YouTube.", 2, ["web_youtube_search", "web_youtube_play_first"])

    def _form_fill_flow(self, goal, context):
        dom = self._call_tool("browser_get_dom_summary", {})
        if self._is_tool_error(dom):
            return AgentResult(False, "I couldn't inspect the form fields on this page.", 1, ["browser_get_dom_summary"], error=dom)

        values = self._extract_form_values(goal)
        if not values:
            return AgentResult(False, "Please provide form values like name: John, email: john@example.com.", 1, ["browser_get_dom_summary"], error="missing_form_values")

        mapped = self._map_form_values(values)
        field_payload = json.dumps(mapped, ensure_ascii=True)
        fill = self._call_tool("browser_fill_form", {"field_values": field_payload})
        if self._is_tool_error(fill):
            return AgentResult(False, "I found the form but couldn't fill all fields.", 2, ["browser_get_dom_summary", "browser_fill_form"], error=fill)

        if any(k in goal.lower() for k in ["submit", "confirm submit", "go ahead and submit"]):
            submit = self._call_tool("browser_submit_form", {"selector": "form"})
            if self._is_tool_error(submit):
                return AgentResult(False, "I filled the form but submit failed.", 3, ["browser_get_dom_summary", "browser_fill_form", "browser_submit_form"], error=submit)
            return AgentResult(True, "Form filled and submitted successfully.", 3, ["browser_get_dom_summary", "browser_fill_form", "browser_submit_form"])

        return AgentResult(
            True,
            "Form fields are filled. Please confirm before submission, and I will submit it.",
            2,
            ["browser_get_dom_summary", "browser_fill_form"],
        )

    def _pagination_flow(self, goal, context):
        click_res = self._call_tool("browser_click_text", {"text": "Next"})
        if not self._is_tool_error(click_res):
            return AgentResult(True, "Moved to the next page.", 1, ["browser_click_text"])

        js = (
            "(function(){"
            "var n=document.querySelector('.pagination .next, a[rel=next], button[aria-label*=Next], button[aria-label*=next]');"
            "if(!n){return 'NOT_FOUND';}"
            "n.click();"
            "return 'clicked';"
            "})()"
        )
        js_res = self._call_tool("browser_execute_js", {"js_code": js})
        if self._is_tool_error(js_res) or "NOT_FOUND" in str(js_res):
            return AgentResult(False, "I couldn't find a next-page control.", 2, ["browser_click_text", "browser_execute_js"], error=str(js_res))
        return AgentResult(True, "Moved to the next page.", 2, ["browser_click_text", "browser_execute_js"])

    def _table_extract_flow(self, goal, context):
        js = (
            "(function(){"
            "var rows=Array.from(document.querySelectorAll('table tr')).slice(0,30);"
            "var data=rows.map(function(r){return Array.from(r.querySelectorAll('th,td')).map(function(c){return (c.innerText||'').trim();});});"
            "return JSON.stringify(data);"
            "})()"
        )
        data_res = self._call_tool("browser_execute_js", {"js_code": js})
        if self._is_tool_error(data_res):
            return AgentResult(False, "I couldn't extract table data from this page.", 1, ["browser_execute_js"], error=data_res)

        parsed = self._parse_table_json(str(data_res))
        if not parsed:
            return AgentResult(False, "I found the table but couldn't parse its rows.", 1, ["browser_execute_js"], error="table_parse_failed")

        headers = parsed[0] if parsed else []
        row_count = max(len(parsed) - 1, 0)
        preview = "; ".join([", ".join(r[:3]) for r in parsed[1:4] if r])
        summary = f"I extracted {row_count} data rows. Headers: {', '.join(headers[:6])}. Sample: {preview[:220]}"
        return AgentResult(True, summary, 1, ["browser_execute_js"], data={"rows": parsed[:10]})

    def _extract_form_values(self, goal: str) -> dict:
        text = goal.strip()
        # Try JSON object first.
        m = re.search(r"\{.*\}", text)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return {str(k).strip(): str(v).strip() for k, v in obj.items()}
            except Exception:
                pass

        values: dict[str, str] = {}
        pairs = re.split(r",|;", text)
        for p in pairs:
            if ":" in p:
                k, v = p.split(":", 1)
            elif "=" in p:
                k, v = p.split("=", 1)
            else:
                continue
            key = k.strip().lower()
            val = v.strip().strip(".")
            if key and val:
                values[key] = val
        return values

    def _map_form_values(self, values: dict) -> dict:
        mapped: dict[str, str] = {}
        for key, value in values.items():
            if "email" in key:
                selector = "input[type='email'],input[name*='email'],input[id*='email']"
            elif "password" in key:
                selector = "input[type='password'],input[name*='password'],input[id*='password']"
            elif "name" in key:
                selector = "input[name*='name'],input[id*='name'],input[placeholder*='name']"
            elif "phone" in key:
                selector = "input[type='tel'],input[name*='phone'],input[id*='phone']"
            else:
                safe_key = re.sub(r"[^a-z0-9_-]", "", key)
                selector = (
                    f"input[name*='{safe_key}'],input[id*='{safe_key}'],"
                    f"textarea[name*='{safe_key}'],textarea[id*='{safe_key}']"
                )
            mapped[selector] = value
        return mapped

    def _parse_table_json(self, raw: str) -> list[list[str]]:
        # browser_execute_js is often returned as stringified dict with a JSON payload in "value".
        blob = raw
        m = re.search(r"'value':\s*'(.+)'", raw)
        if m:
            blob = m.group(1)
        blob = blob.encode("utf-8", "ignore").decode("unicode_escape")
        try:
            obj = json.loads(blob)
            if isinstance(obj, list):
                return [[str(c) for c in row] for row in obj if isinstance(row, list)]
        except Exception:
            pass
        return []

    def _recover_from_browser_error(self, record: StepRecord):
        lower = (record.result or "").lower()
        self._call_tool("browser_get_url", {})
        if "404" in lower or "not found" in lower:
            self._call_tool("browser_back", {})
            return
        if "timeout" in lower:
            self._call_tool("browser_reload", {})
            return
        if "js error" in lower or "exception" in lower:
            self._call_tool("browser_get_dom_summary", {})
            return

    def _on_step_complete(self, record: StepRecord):
        if not record.success:
            if record.tool.startswith("browser_") or record.tool.startswith("web_"):
                self._recover_from_browser_error(record)
            return
        if record.tool in {"open_url", "open_url_and_wait", "browser_navigate", "browser_navigate_and_wait"}:
            m = re.search(r"(https?://[^\s]+)", record.result)
            if m:
                self._active_context["active_url"] = m.group(1)
