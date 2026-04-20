"""
DEMASCAS — actuators/communication.py
JARVIS-Grade Communication Tools — email, messaging, contacts, research.

These tools make DEMASCAS feel like JARVIS by enabling:
  - Read the current email/message on screen (VLM-powered)
  - Compose emails using task-specific prompts
  - Smart reply to visible messages
  - Confirm-and-execute safety gate
  - Extract contact info from screen/conversation
  - Web research with structured reports

All tools use EXISTING infrastructure:
  - VLM server for screen reading (Ollama or MLX)
  - LLM text model for drafting (Ollama or MLX)
  - AppleScript for UI automation
  - No new models, no extra RAM

RAM BUDGET: 0 MB extra.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import sys
from typing import Optional

# Platform-specific vision imports
if sys.platform.startswith('linux'):
    from actuators.system_control_linux import get_active_window_tree

    # Linux stubs for VLM functions (not yet implemented on Linux)
    def take_screenshot_base64() -> str:
        return ""  # Not implemented on Linux yet

    def vision_query(image_base64: str, prompt: str) -> str:
        return "Error: VLM not available on Linux"
else:
    from sensors.vision import (
        take_screenshot_base64,
        vision_query,
        get_active_window_tree,
    )

from actuators.network import search_web, read_webpage


# ═══════════════════════════════════════════════════════════════════════
# 1. READ CURRENT EMAIL — extracts email content from screen via VLM
# ═══════════════════════════════════════════════════════════════════════

def read_current_email() -> str:
    """
    Uses the VLM to read and extract the email/message currently visible
    on screen. Works with Mail, Gmail in browser, Outlook, etc.

    Returns:
        JSON string with: {from, subject, body_preview, timestamp}
        or error message.
    """
    print("[📧 COMM] Reading current email from screen...")

    screenshot = take_screenshot_base64()
    if screenshot.startswith("[-]"):
        return screenshot

    vlm_prompt = (
        "Look at this screen and extract the email/message content. "
        "Return ONLY a JSON object:\n"
        '{"from":"<sender name or email>","subject":"<subject line>",'
        '"body_preview":"<first 300 chars of email body>",'
        '"timestamp":"<date/time if visible>","app":"<email app name>"}\n'
        "If no email is visible, return: "
        '{"error":"No email visible on screen"}\n'
        "Output ONLY the JSON."
    )

    resp = vision_query(vlm_prompt, screenshot)
    if resp.startswith("[-]"):
        return resp

    # Try to parse and return structured data
    try:
        first_brace = resp.find("{")
        last_brace = resp.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            parsed = json.loads(resp[first_brace:last_brace + 1])
            if "error" in parsed:
                return f"[-] {parsed['error']}"
            return json.dumps(parsed, indent=2)
    except (json.JSONDecodeError, KeyError):
        pass

    return f"[-] Could not parse email from screen: {resp[:200]}"


# ═══════════════════════════════════════════════════════════════════════
# 2. COMPOSE EMAIL — creates an email draft using task-specific prompts
# ═══════════════════════════════════════════════════════════════════════

def compose_email(
    recipient: str = "",
    subject: str = "",
    key_points: str = "",
    tone: str = "professional",
) -> str:
    """
    Composes an email draft. Does NOT send — returns the draft for
    user confirmation via the CONFIRMING state.

    Uses the LLM text model with the compose_email prompt template
    for high-quality output from the 3B model.

    Args:
        recipient: Who to send to (name or email)
        subject: Email subject (optional — will be generated if empty)
        key_points: What the email should convey
        tone: "formal", "casual", or "professional" (default)

    Returns:
        JSON string with: {subject, body, to, tone, confirm}
    """
    import requests

    print(f"[📧 COMM] Composing email to '{recipient}' about '{key_points[:50]}'...")

    from core.prompt_templates import compose_email_prompt
    from core.screen_context import get_screen_context_block

    screen_ctx = get_screen_context_block()

    # Build prompt
    system_prompt = compose_email_prompt(
        screen_context=screen_ctx,
    )

    user_msg = f"Write an email"
    if recipient:
        user_msg += f" to {recipient}"
    if subject:
        user_msg += f" with subject '{subject}'"
    if key_points:
        user_msg += f". Key points: {key_points}"
    user_msg += f". Tone: {tone}."

    try:
        llm_endpoint = os.environ.get("DEMASCAS_LLM_ENDPOINT", "http://127.0.0.1:11434")
        llm_model = os.environ.get("DEMASCAS_LLM_MODEL", "gemma4:31b-cloud")
        resp = requests.post(
            f"{llm_endpoint}/v1/chat/completions",
            json={
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 300,
                "temperature": 0.3,
                "stream": False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Try to extract JSON
        first_brace = content.find("{")
        last_brace = content.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            parsed = json.loads(content[first_brace:last_brace + 1])
            # Ensure required fields
            result = {
                "subject": parsed.get("subject", subject or "No subject"),
                "body": parsed.get("body", content),
                "to": parsed.get("to", recipient),
                "tone": parsed.get("tone", tone),
                "confirm": parsed.get("confirm",
                    f"I've drafted an email to {recipient or 'them'}. Want me to proceed?"),
            }
            return json.dumps(result, indent=2)

        # If no JSON, wrap raw content
        return json.dumps({
            "subject": subject or "Email",
            "body": content,
            "to": recipient,
            "tone": tone,
            "confirm": f"I've drafted an email. Want me to proceed?",
        }, indent=2)

    except Exception as e:
        return f"[-] Email composition failed: {e}"


# ═══════════════════════════════════════════════════════════════════════
# 3. SMART REPLY — reads email on screen, generates context-aware reply
# ═══════════════════════════════════════════════════════════════════════

def smart_reply(
    instructions: str = "",
    tone: str = "",
) -> str:
    """
    Reads the email/message currently on screen and generates a reply.
    Combines VLM (to read the screen) + text model (to draft reply).

    Args:
        instructions: Optional guidance (e.g. "accept the meeting", "decline politely")
        tone: Override tone (otherwise matches the original message's tone)

    Returns:
        JSON string with: {reply, original_subject, tone, confirm}
    """
    import requests

    print("[📧 COMM] Smart reply — reading screen and drafting...")

    # Step 1: Read the email on screen
    email_data = read_current_email()
    if email_data.startswith("[-]"):
        return email_data

    # Step 2: Draft reply using task template
    from core.prompt_templates import smart_reply_prompt
    from core.screen_context import get_screen_context_block

    screen_ctx = get_screen_context_block()

    system_prompt = smart_reply_prompt(
        email_content=email_data,
        screen_context=screen_ctx,
    )

    user_msg = "Write a reply to the email shown on screen."
    if instructions:
        user_msg += f" Instructions: {instructions}"
    if tone:
        user_msg += f" Use a {tone} tone."

    try:
        llm_endpoint = os.environ.get("DEMASCAS_LLM_ENDPOINT", "http://127.0.0.1:11434")
        llm_model = os.environ.get("DEMASCAS_LLM_MODEL", "gemma4:31b-cloud")
        resp = requests.post(
            f"{llm_endpoint}/v1/chat/completions",
            json={
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 300,
                "temperature": 0.3,
                "stream": False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Parse reply JSON
        first_brace = content.find("{")
        last_brace = content.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            parsed = json.loads(content[first_brace:last_brace + 1])
            result = {
                "reply": parsed.get("reply", content),
                "tone": parsed.get("tone", tone or "matched"),
                "confirm": parsed.get("confirm",
                    "I've drafted a reply. Shall I type it in?"),
            }
            return json.dumps(result, indent=2)

        return json.dumps({
            "reply": content,
            "tone": tone or "auto",
            "confirm": "I've drafted a reply. Shall I type it in?",
        }, indent=2)

    except Exception as e:
        return f"[-] Smart reply failed: {e}"


# ═══════════════════════════════════════════════════════════════════════
# 4. CONFIRM AND EXECUTE — safety gate for risky actions
# ═══════════════════════════════════════════════════════════════════════

def confirm_and_execute(
    action_description: str,
    tool_name: str,
    tool_args: str = "{}",
) -> str:
    """
    Stores a pending action for user confirmation.
    The C++ daemon will enter CONFIRMING state, speak the description,
    and wait for yes/no.

    This tool does NOT execute anything — it returns the confirmation
    request. The daemon handles the yes/no flow.

    Args:
        action_description: Human-readable description of what will happen
        tool_name: Tool to execute if confirmed
        tool_args: JSON string of tool arguments

    Returns:
        JSON with: {status, confirm_speech, pending_tool, pending_args}
    """
    print(f"[⚠️ CONFIRM] Queuing: {action_description}")

    try:
        args = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
    except json.JSONDecodeError:
        args = {}

    return json.dumps({
        "status": "pending_confirmation",
        "confirm_speech": action_description,
        "pending_tool": tool_name,
        "pending_args": args,
    })


# ═══════════════════════════════════════════════════════════════════════
# 5. EXTRACT CONTACT — pulls contact info from screen or conversation
# ═══════════════════════════════════════════════════════════════════════

def extract_contact() -> str:
    """
    Uses the VLM to extract contact information visible on screen.
    Works with: email headers, contact cards, social profiles,
    business cards, signatures, etc.

    Returns:
        JSON string with: {name, email, phone, organization, source}
    """
    print("[👤 COMM] Extracting contact info from screen...")

    screenshot = take_screenshot_base64()
    if screenshot.startswith("[-]"):
        return screenshot

    vlm_prompt = (
        "Look at this screen and extract any contact information visible. "
        "Return ONLY a JSON object:\n"
        '{"name":"<full name>","email":"<email address or null>",'
        '"phone":"<phone number or null>","organization":"<company/org or null>",'
        '"source":"<where you found this info>"}\n'
        'If no contact info is visible: {"error":"No contact information visible"}\n'
        "Output ONLY the JSON."
    )

    resp = vision_query(vlm_prompt, screenshot)
    if resp.startswith("[-]"):
        return resp

    try:
        first_brace = resp.find("{")
        last_brace = resp.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            parsed = json.loads(resp[first_brace:last_brace + 1])
            if "error" in parsed:
                return f"[-] {parsed['error']}"

            # Auto-save to people graph
            try:
                from core.learning import add_person
                name = parsed.get("name", "")
                if name:
                    add_person(
                        name=name,
                        email=parsed.get("email"),
                        phone=parsed.get("phone"),
                        organization=parsed.get("organization"),
                        source="screen_extraction",
                    )
            except Exception:
                pass  # non-fatal

            return json.dumps(parsed, indent=2)
    except (json.JSONDecodeError, KeyError):
        pass

    return f"[-] Could not extract contact info: {resp[:200]}"


# ═══════════════════════════════════════════════════════════════════════
# 6. WEB RESEARCH AND REPORT — structured research with synthesis
# ═══════════════════════════════════════════════════════════════════════

def web_research_and_report(
    topic: str,
    depth: str = "brief",
) -> str:
    """
    Performs structured web research and returns a synthesized report.

    Phase 1: Search the web for the topic
    Phase 2: Read top 2-3 results
    Phase 3: Synthesize into a spoken summary

    Args:
        topic: What to research (e.g. "latest MacBook Pro features")
        depth: "brief" (3 sentences) or "detailed" (5-8 sentences)

    Returns:
        Synthesized research report as spoken text.
    """
    import requests

    print(f"[🔍 RESEARCH] Researching: '{topic}' (depth={depth})")

    # Phase 1: Search
    try:
        search_results = search_web(topic, max_results=5)
    except Exception as e:
        return f"[-] Search failed: {e}"

    if not search_results or search_results.startswith("[-]"):
        return f"[-] No search results found for '{topic}'"

    # Phase 2: Read top results (max 2 to keep it fast)
    sources = []
    urls_extracted = []

    # Parse search results for URLs
    for line in search_results.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("http://") or line_stripped.startswith("https://"):
            urls_extracted.append(line_stripped)
        elif "http" in line_stripped:
            # Try to extract URL from line
            for word in line_stripped.split():
                if word.startswith("http"):
                    urls_extracted.append(word.rstrip(".,;)"))
                    break

    for url in urls_extracted[:2]:  # read max 2 pages
        try:
            page_content = read_webpage(url)
            if page_content and not page_content.startswith("[-]"):
                # Truncate to 1000 chars per source
                sources.append({
                    "url": url,
                    "content": page_content[:1000],
                })
        except Exception:
            continue

    # Phase 3: Synthesize using text model
    from core.prompt_templates import research_report_prompt

    system_prompt = research_report_prompt(topic=topic)

    source_text = ""
    if sources:
        for i, src in enumerate(sources):
            source_text += f"\n[SOURCE {i+1}: {src['url']}]\n{src['content']}\n"
    else:
        source_text = f"\n[SEARCH RESULTS]\n{search_results}"

    max_sentences = "3-4" if depth == "brief" else "6-8"
    user_msg = (
        f"Research topic: {topic}\n"
        f"Depth: {depth} ({max_sentences} sentences)\n"
        f"{source_text}\n\n"
        "Synthesize a spoken summary. Lead with the most important finding."
    )

    try:
        llm_endpoint = os.environ.get("DEMASCAS_LLM_ENDPOINT", "http://127.0.0.1:11434")
        llm_model = os.environ.get("DEMASCAS_LLM_MODEL", "gemma4:31b-cloud")
        resp = requests.post(
            f"{llm_endpoint}/v1/chat/completions",
            json={
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 250 if depth == "brief" else 400,
                "temperature": 0.3,
                "stream": False,
            },
            timeout=20,
        )
        resp.raise_for_status()
        report = resp.json()["choices"][0]["message"]["content"]

        # Clean up — remove any markdown or artifacts
        report = report.replace("**", "").replace("##", "").replace("- ", "")
        return report.strip()

    except Exception as e:
        # Fallback: return raw search results
        return f"Here's what I found: {search_results[:500]}"
