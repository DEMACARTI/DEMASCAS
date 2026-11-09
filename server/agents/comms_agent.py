"""
comms_agent.py - Email, smart reply, contact extraction.
"""

import json
import re
from .base_agent import AgentBase, AgentResult, StepRecord


class CommsAgent(AgentBase):
    domain_name = "comms"
    tool_names = [
        "read_current_email", "gmail_read_email",
        "compose_email", "gmail_compose_draft",
        "smart_reply", "extract_contact", "web_research_and_report",
        "get_active_window_tree", "vision_execute",
        "get_clipboard", "set_clipboard", "open_url_and_wait",
    ]

    @property
    def system_prompt(self) -> str:
        return (
            "You are the DEMASCAS Communications Agent for email workflows.\n"
            "Rules:\n"
            "1. NEVER send any email. Draft only.\n"
            "2. If no email address is present, try extract_contact first; otherwise ask user.\n"
            "3. For 'reply to that email', read_current_email first.\n"
            "4. If Gmail URL is active use gmail_compose_draft; for native Mail use compose_email.\n"
            "5. compose_email returns draft JSON and does not send.\n"
            "6. gmail_compose_draft opens compose window and does not send.\n"
            "7. Match the original email tone (formal/casual).\n"
            "8. Finish with {'done': true, 'say': "
            "\"I've drafted an email to X with subject 'Y'. Ready to review.\"}."
        )

    def run(self, goal, context):
        g = (goal or "").lower()
        if re.search(r"\b(compose|draft|write|new|create|send)\b", g) and re.search(r"\b(e?mails?|gmail)\b", g):
            return self._compose_email_flow(goal, context)
        if re.search(r"\b(reply|respond)\b", g) and re.search(r"\b(email|message|that|it)\b", g):
            return self._smart_reply_flow(goal, context)
        if re.search(r"\b(read|check|show|what|any)\b", g) and re.search(r"\b(email|emails|mail|inbox)\b", g):
            return self._read_email_flow(goal, context)
        if re.search(r"\b(contact|email address|phone number|find)\b", g) and re.search(r"\b(info|information|for|of)\b", g):
            return self._extract_contact_flow(goal, context)
        return self._execute(goal, context)

    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    _NOT_NAMES = {
        "the", "my", "a", "an", "him", "her", "them", "me", "us", "it", "gmail",
        "mail", "email", "everyone", "all", "about", "regarding", "say", "tell",
    }

    def _resolve_recipient(self, goal: str) -> tuple[str, str]:
        """Return (email_address, spoken_name).  Uses an explicit address if
        present, otherwise looks the spoken name up in the people graph."""
        m = self._EMAIL_RE.search(goal)
        if m:
            return m.group(0), ""
        nm = re.search(
            r"\bto\s+([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*)?)", goal
        )
        name = ""
        if nm:
            words = [w for w in nm.group(1).split() if w.lower() not in self._NOT_NAMES]
            # stop at the topic ("to Aman about …")
            name = " ".join(words[:2]).strip()
            if name.lower() in self._NOT_NAMES:
                name = ""
        if not name:
            return "", ""
        try:
            from core.learning import get_person
            person = get_person(name) or {}
            if person.get("email"):
                return str(person["email"]), name
        except Exception:
            pass
        return "", name

    @staticmethod
    def _extract_topic(goal: str) -> tuple[str, str]:
        """Return (subject, key_points) spoken in the command."""
        subject = ""
        sm = re.search(r"\bsubject(?:\s+line)?\s*(?:is|:|as|saying)?\s+(.+?)(?:\s+(?:and|saying|that says)\b|$)",
                       goal, re.IGNORECASE)
        if sm:
            subject = sm.group(1).strip(" .\"'")
        km = re.search(r"\b(?:about|regarding|saying|that says|to say|telling (?:him|her|them))\b\s+(.+)$",
                       goal, re.IGNORECASE)
        key_points = km.group(1).strip(" .") if km else ""
        return subject, key_points

    def _compose_email_flow(self, goal, context):
        tools: list[str] = []
        recipient, spoken_name = self._resolve_recipient(goal)
        subject, key_points = self._extract_topic(goal)

        # A named recipient we can't resolve → ask instead of guessing.
        if spoken_name and not recipient:
            return AgentResult(
                True, f"What's {spoken_name}'s email address?", 0, [],
                data={"needs": "recipient_email", "name": spoken_name},
            )

        body = ""
        if key_points or subject:
            draft_json = self._call_tool("compose_email", {
                "recipient": recipient or spoken_name,
                "subject": subject,
                "key_points": key_points or subject,
                # The spoken request has the content — skip the ~1-2 s
                # screen digest.
                "use_screen": False,
            })
            tools.append("compose_email")
            if not self._is_tool_error(draft_json):
                try:
                    draft = json.loads(draft_json)
                    subject = str(draft.get("subject") or subject)
                    body = str(draft.get("body") or "")
                except Exception:
                    body = ""
            if not body:
                # LLM drafting failed — still put the user's words in the draft.
                body = key_points
            if not subject and key_points:
                subject = key_points[:60]

        nav_res = self._call_tool(
            "open_url_and_wait",
            {"url": "https://mail.google.com/", "browser": "Brave Browser", "wait_seconds": 4.0},
        )
        tools.append("open_url_and_wait")
        if self._is_tool_error(nav_res):
            return AgentResult(False, "I couldn't open Gmail in Brave.", len(tools), tools, error=nav_res)

        draft_args = {k: v for k, v in (("to", recipient), ("subject", subject), ("body", body)) if v}
        draft_res = self._call_tool("gmail_compose_draft", draft_args)
        tools.append("gmail_compose_draft")
        if self._is_tool_error(draft_res):
            return AgentResult(False, "Gmail opened, but I couldn't start the draft.", len(tools), tools, error=draft_res)

        who = f" to {recipient}" if recipient else ""
        about = f" about {subject}" if subject else ""
        if body:
            summary = f"I've drafted an email{who}{about}. It's ready for you to review; I haven't sent it."
        else:
            summary = f"I've opened a new Gmail draft{who}."
        return AgentResult(True, summary, len(tools), tools)

    def _smart_reply_flow(self, goal, context):
        active_url = str((context or {}).get("active_url", ""))
        read_tool = "gmail_read_email" if "mail.google.com" in active_url else "read_current_email"
        read_res = self._call_tool(read_tool, {})
        if self._is_tool_error(read_res) or not read_res.strip():
            return AgentResult(False, "Couldn't read email. Please open one first.", 1, [read_tool], error=read_res)

        self._active_context["email_content"] = read_res[:600]
        draft_res = self._call_tool("smart_reply", {"instructions": goal})
        if self._is_tool_error(draft_res):
            return AgentResult(False, "I read the email but couldn't draft a reply.", 2, [read_tool, "smart_reply"], error=draft_res)

        return AgentResult(True, "I've drafted a reply. Check the compose window.", 2, [read_tool, "smart_reply"])

    def _read_email_flow(self, goal, context):
        active_url = str((context or {}).get("active_url", ""))
        tool = "gmail_read_email" if "mail.google.com" in active_url else "read_current_email"
        result = self._call_tool(tool, {})
        if self._is_tool_error(result):
            return AgentResult(False, "I couldn't read your email right now.", 1, [tool], error=result)

        sender = "unknown sender"
        subject = "no subject"
        sender_m = re.search(r"from\s*:\s*(.+)", result, re.IGNORECASE)
        subject_m = re.search(r"subject\s*:\s*(.+)", result, re.IGNORECASE)
        if sender_m:
            sender = sender_m.group(1).strip()
        if subject_m:
            subject = subject_m.group(1).strip()

        return AgentResult(True, f"Email from {sender}, subject: {subject}.", 1, [tool])

    def _extract_contact_flow(self, goal, context):
        m = re.search(r"(?:contact|info for|information for|info of|information of)\s+(.+)$", goal, re.IGNORECASE)
        name = m.group(1).strip(" .") if m else ""
        if not name:
            return AgentResult(False, "Couldn't identify the contact name.", 0, [], error="missing_name")

        result = self._call_tool("extract_contact", {})
        if self._is_tool_error(result):
            return AgentResult(False, "Couldn't find contact info.", 1, ["extract_contact"], error=result)

        text = (result or "").strip()
        if not text:
            text = "Couldn't find contact info."
        return AgentResult(True, text[:200], 1, ["extract_contact"])

    def _on_step_complete(self, record):
        if record.success and record.tool in {"read_current_email", "gmail_read_email"}:
            self._active_context["last_result"] = (record.result or "")[:300]
