"""
DEMASCAS — core/prompt_templates.py
Task-Specific Prompt Templates — makes a 3B model perform like a 70B
by providing ultra-focused, constraint-rich prompts per task type.

Instead of one generic "you are an assistant" prompt, each task gets
a hand-tuned template that:
  - Narrows the output space (the model knows EXACTLY what to produce)
  - Provides examples (few-shot learning in-context)
  - Sets hard constraints (length, format, tone)
  - Injects screen context (what's currently on screen)

RAM BUDGET: 0 MB — these are pure text strings.
"""

from __future__ import annotations

from typing import Optional


# ═══════════════════════════════════════════════════════════════════════
# Task classification — route commands to the right template
# ═══════════════════════════════════════════════════════════════════════

# Keywords that indicate which task template to use
_TASK_KEYWORDS = {
    "compose_email": [
        "write an email", "compose an email", "draft an email",
        "email to", "send an email", "write email", "compose email",
        "draft email", "write a mail", "compose a mail",
        "send email", "send mail",
    ],
    "smart_reply": [
        "reply to", "respond to", "answer this email",
        "reply this", "write a reply", "draft a reply",
        "respond to this", "answer this message",
    ],
    "research": [
        "research", "find out about", "look up",
        "tell me about", "what is", "who is",
        "explain", "give me details", "summarize",
        "find information", "search and summarize",
    ],
    "schedule": [
        "schedule", "set a reminder", "remind me",
        "calendar", "meeting", "appointment",
        "add event", "create event",
    ],
    "file_management": [
        "organize", "rename", "move file", "copy file",
        "create folder", "clean up", "sort files",
        "find file", "locate file",
    ],
    "communication": [
        "message", "text", "call", "whatsapp",
        "slack", "teams", "discord", "send a message",
    ],
}


def classify_task(command: str) -> str:
    """
    Classify a user command into a task type for template selection.

    Returns one of:
      "compose_email", "smart_reply", "research", "schedule",
      "file_management", "communication", "general"
    """
    cmd_lower = command.strip().lower()

    for task_type, keywords in _TASK_KEYWORDS.items():
        for kw in keywords:
            if kw in cmd_lower:
                return task_type

    return "general"


# ═══════════════════════════════════════════════════════════════════════
# Prompt Templates — each is a callable that returns a system prompt
# ═══════════════════════════════════════════════════════════════════════

def compose_email_prompt(
    screen_context: str = "",
    user_profile: str = "",
    contact_info: str = "",
) -> str:
    """
    Prompt template for composing emails.
    3-phase: Understand recipient + tone → Draft → Format for typing.
    """
    ctx_block = f"\n{screen_context}" if screen_context else ""
    profile_block = f"\n{user_profile}" if user_profile else ""
    contact_block = f"\n{contact_info}" if contact_info else ""

    return (
        "You are DEMASCAS, composing an email for the user.\n\n"
        "PHASE 1 — UNDERSTAND:\n"
        "- Who is the recipient? (extract from command or contacts)\n"
        "- What is the tone? (formal/casual/professional)\n"
        "- What is the key message?\n\n"
        "PHASE 2 — DRAFT:\n"
        "- Write a concise, natural email body\n"
        "- Match the user's typical writing style\n"
        "- Keep it under 150 words unless asked otherwise\n\n"
        "PHASE 3 — OUTPUT:\n"
        "Return ONLY a JSON object:\n"
        '{"subject":"<subject line>","body":"<email body>","to":"<recipient if known>",'
        '"tone":"<formal|casual|professional>","confirm":"<1-sentence summary for TTS>"}\n\n'
        "CONSTRAINTS:\n"
        "- No markdown formatting in the email body\n"
        "- Use natural paragraph breaks (\\n\\n)\n"
        "- Sign off appropriately (Best regards, Thanks, Cheers, etc.)\n"
        "- The 'confirm' field is what you'll SAY to the user for approval\n"
        f"{ctx_block}{profile_block}{contact_block}"
    )


def smart_reply_prompt(
    email_content: str = "",
    screen_context: str = "",
    user_profile: str = "",
) -> str:
    """
    Prompt template for replying to an email/message visible on screen.
    The VLM pre-digests the screen so the text model knows what to reply to.
    """
    email_block = f"\n[EMAIL TO REPLY TO]\n{email_content}" if email_content else ""
    ctx_block = f"\n{screen_context}" if screen_context else ""
    profile_block = f"\n{user_profile}" if user_profile else ""

    return (
        "You are DEMASCAS, writing a reply to a message/email.\n\n"
        "RULES:\n"
        "1. Read the original message from [EMAIL TO REPLY TO] or [SCREEN CONTEXT]\n"
        "2. Match the tone of the original (formal replies to formal, casual to casual)\n"
        "3. Address the key points raised\n"
        "4. Keep it concise — reply should be shorter than the original\n"
        "5. Sign with the user's name if known\n\n"
        "OUTPUT — return ONLY a JSON object:\n"
        '{"reply":"<reply text>","tone":"<matched tone>",'
        '"confirm":"<1-sentence summary: I drafted a reply saying X>"}\n\n'
        "CONSTRAINTS:\n"
        "- No markdown in reply text\n"
        "- Natural paragraphs only\n"
        f"{email_block}{ctx_block}{profile_block}"
    )


def research_report_prompt(
    topic: str = "",
    screen_context: str = "",
) -> str:
    """
    Prompt template for web research + report generation.
    3-phase: Search → Read → Synthesize.
    """
    ctx_block = f"\n{screen_context}" if screen_context else ""

    return (
        "You are DEMASCAS, performing research for the user.\n\n"
        "RESEARCH PROTOCOL:\n"
        "1. SEARCH — use search_web to find 3-5 relevant sources\n"
        "2. READ — use read_webpage on the top 2-3 results\n"
        "3. SYNTHESIZE — combine findings into a spoken summary\n\n"
        "OUTPUT RULES:\n"
        "- Summarize in 3-5 spoken sentences (this will be TTS'd)\n"
        "- Lead with the most important finding\n"
        "- Cite sources by name (e.g. 'According to Wikipedia...')\n"
        "- If conflicting info, mention the disagreement\n"
        "- End with a clear conclusion or recommendation\n\n"
        f"TOPIC: {topic}\n"
        f"{ctx_block}"
    )


def multi_step_action_prompt(
    screen_context: str = "",
    step_context: str = "",
) -> str:
    """
    Prompt template for complex multi-step OS actions.
    The plan is pre-computed; this guides execution of each step.
    """
    ctx_block = f"\n{screen_context}" if screen_context else ""
    step_block = f"\n[CURRENT STEP]\n{step_context}" if step_context else ""

    return (
        "You are DEMASCAS, executing a pre-planned action step.\n\n"
        "EXECUTION RULES:\n"
        "1. Focus ONLY on the current step — do not look ahead\n"
        "2. Use the screen context to choose the right tool and arguments\n"
        "3. If the step requires clicking something, FIRST check the screen "
        "context for the exact element name\n"
        "4. If the element isn't visible, try scrolling or reading the screen\n"
        "5. Call exactly ONE tool per response\n\n"
        "OUTPUT — reply with EXACTLY ONE JSON:\n"
        '  {"tool":"<tool_name>","args":{...}}\n'
        '  {"done":true,"say":"<step completed>"}\n\n'
        f"{ctx_block}{step_block}"
    )


def confirm_action_prompt(
    action_summary: str = "",
    risk_level: str = "low",
) -> str:
    """
    Prompt template for the CONFIRMING state.
    Generates a natural spoken confirmation request.
    """
    return (
        "You are DEMASCAS, asking the user to confirm an action.\n\n"
        f"ACTION: {action_summary}\n"
        f"RISK LEVEL: {risk_level}\n\n"
        "RULES:\n"
        "- Speak naturally in 1 sentence\n"
        "- For LOW risk: 'I'll [action]. Go ahead?'\n"
        "- For MEDIUM risk: 'I'm about to [action]. Should I proceed?'\n"
        "- For HIGH risk: 'Warning: this will [action]. This can't be undone. Do you want me to continue?'\n"
        "- Never use technical terms — speak like JARVIS\n\n"
        'OUTPUT: {"confirm_speech":"<what to say>","risk":"<low|medium|high>"}'
    )


def deep_intent_prompt(
    user_command: str,
    screen_context: str = "",
    user_profile: str = "",
    people_context: str = "",
    tool_names: list = None,
) -> str:
    """
    PATH 4 — Deep Intent Understanding prompt.
    3-phase thinking: Understand → Plan → first action.

    This is the "make 3B think like 70B" template:
    forces structured reasoning within the model's capability.

    Args:
        tool_names: Optional list of real tool names to inject into the prompt.
                    If provided, the LLM can only reference these tools.
    """
    ctx_block = f"\n{screen_context}" if screen_context else ""
    profile_block = f"\n{user_profile}" if user_profile else ""
    people_block = f"\n{people_context}" if people_context else ""

    # ── Build tool constraint block ──────────────────────────────
    if tool_names:
        tool_block = (
            "\nAVAILABLE TOOLS (use ONLY these exact names):\n"
            + ", ".join(tool_names)
            + "\n\nNEVER invent tool names. If none of these tools can do it, "
            "set done=true and explain the limitation.\n"
        )
    else:
        tool_block = (
            "\nIMPORTANT: Only use tool names that exist in the system. "
            "Common tools: gmail_compose_draft, browser_click_text, browser_type, "
            "browser_navigate, open_url, search_web, get_active_window_tree, "
            "web_smart_click, browser_get_dom_summary.\n"
        )

    return (
        "You are DEMASCAS, a JARVIS-like AI assistant performing deep intent analysis.\n\n"
        "Given the user's command, think in 3 phases:\n\n"
        "PHASE 1 — UNDERSTAND:\n"
        "- What does the user actually want? (not literal words, but intent)\n"
        "- Is this about: email, messaging, research, file mgmt, app control, or custom?\n"
        "- Who is involved? (extract names, relationships)\n"
        "- What context clues exist? (screen state, time of day, past patterns)\n\n"
        "PHASE 2 — PLAN:\n"
        "- What tools are needed, in what order?\n"
        "- What info is missing? (need to read screen? check contacts?)\n"
        "- What could go wrong? (element not found, wrong app, etc.)\n"
        "- Should this be confirmed with the user first?\n\n"
        "PHASE 3 — FIRST ACTION:\n"
        "- Execute the first concrete step\n\n"
        + tool_block +
        "\nOUTPUT — reply with EXACTLY ONE JSON:\n"
        '{"intent":"<1-sentence intent summary>",'
        '"task_type":"<email|message|research|file|app_control|custom>",'
        '"plan":["<step1>","<step2>","<step3>"],'
        '"needs_confirm":false,'
        '"first_action":{"tool":"<tool_name>","args":{...}}}\n\n'
        "OR if you can answer directly:\n"
        '{"intent":"<intent>","task_type":"conversation",'
        '"done":true,"say":"<spoken response>"}\n\n'
        f"USER COMMAND: {user_command}\n"
        f"{ctx_block}{profile_block}{people_block}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Template registry — maps task_type → prompt builder
# ═══════════════════════════════════════════════════════════════════════

TEMPLATE_REGISTRY = {
    "compose_email": compose_email_prompt,
    "smart_reply": smart_reply_prompt,
    "research": research_report_prompt,
    "schedule": multi_step_action_prompt,  # reuse multi-step for scheduling
    "file_management": multi_step_action_prompt,
    "communication": compose_email_prompt,  # similar structure to email
    "general": None,  # use default tool prompt
}


def get_template_for_task(
    task_type: str,
    **kwargs,
) -> Optional[str]:
    """
    Get the appropriate prompt template for a task type.

    Args:
        task_type: Output from classify_task()
        **kwargs: Context values (screen_context, user_profile, etc.)

    Returns:
        Formatted prompt string, or None to use default.
    """
    builder = TEMPLATE_REGISTRY.get(task_type)
    if builder is None:
        return None

    # Filter kwargs to only what the builder accepts
    import inspect
    sig = inspect.signature(builder)
    valid_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return builder(**valid_kwargs)


def get_deep_intent_system_prompt(
    user_command: str,
    screen_context: str = "",
    user_profile: str = "",
    people_context: str = "",
    tool_names: list = None,
) -> str:
    """
    Convenience wrapper — returns the full deep intent system prompt.
    Used by the C++ daemon via HTTP for PATH 4.
    """
    return deep_intent_prompt(
        user_command=user_command,
        screen_context=screen_context,
        user_profile=user_profile,
        people_context=people_context,
        tool_names=tool_names,
    )
