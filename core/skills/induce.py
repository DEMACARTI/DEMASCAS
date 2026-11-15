"""
DEMASCAS — core/skills/induce.py
Learn new skills from experience.

After a command succeeds through the slow path (agent / tool loop) with 2+
real actions and no existing skill covers it, ONE background LLM call turns
the concrete run into a parameterized procedure:

  "open youtube and search for lofi beats"   (5 tool calls, 9 s)
      → skill "search youtube for {query}"   (status=candidate)

The candidate needs a closer match than built-ins and is promoted to active
after it succeeds twice (runner.record_outcome).  Runs on the tool server's
background learning worker — never on the user's critical path.

Also handles "teach me" requests:  "when I say good morning, open my
calendar and play lofi" → a taught skill made of sub-commands.
"""

from __future__ import annotations

import json
import os
import re
import time

from core.skills.schema import Skill
from core.skills.store import get_store

# Tools that change something / do the task (pure reads alone aren't a skill).
_READ_ONLY = {"get_active_window_tree", "list_open_windows", "browser_get_dom_summary",
              "browser_get_page_text", "browser_get_url", "browser_get_title",
              "list_running_applications", "retrieve_memories", "get_clipboard",
              "web_scan_for_obstacles", "wait_for_element", "wait_for_app_ready",
              "browser_wait_for_element", "browser_wait_for_page_load"}

_PARAPHRASE_PROMPT = """Rewrite this voice-assistant request 3 different ways a user might say it.
Keep the placeholder words in curly braces exactly as they are.
Request: "{template}"
Return JSON: {{"intents": ["...", "...", "..."]}}"""

_STOP = {"the", "a", "an", "and", "to", "for", "of", "on", "in", "my", "me", "it", "is",
         "please", "can", "you", "could", "would", "i", "want", "then", "with", "that", "this",
         "first", "one", "some", "up", "open"}


_SYNONYMS = {
    "search": ("search", "find", "look", "lookup", "browse"),
    "play": ("play", "start", "put", "watch", "listen"),
    "open": ("open", "launch", "start", "show"),
    "send": ("send", "email", "mail", "message", "text"),
    "create": ("create", "make", "new", "build", "add"),
    "close": ("close", "quit", "exit", "kill"),
}


def _guard(word: str) -> str:
    for key, syns in _SYNONYMS.items():
        if word == key or word in syns:
            return r"\b(" + "|".join(sorted(set(syns) | {key})) + r")"
    return rf"\b{re.escape(word)}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or "skill"


def _generalize(command: str, calls: list[dict]) -> tuple[dict, list[dict], str] | None:
    """Deterministic parameterization: every string argument whose value
    appears verbatim in the command becomes a {placeholder}.  No LLM — a 3B
    model invented placeholders and tool names when asked to do this."""
    low = command.lower()
    params: dict = {}
    value_to_name: dict[str, str] = {}
    for c in calls:
        for k, v in (c.get("args") or {}).items():
            if not isinstance(v, str) or len(v.strip()) < 2:
                continue
            val = v.strip().lower()
            if val in value_to_name or val in {v.lower() for v in value_to_name} or not re.search(rf"\b{re.escape(val)}\b", low):
                continue
            name = re.sub(r"\W+", "_", k.lower()).strip("_") or "value"
            while name in params:
                name += "_2"
            value_to_name[val] = name
            params[name] = {"description": f"the {k.replace('_', ' ')} (e.g. \"{v.strip()}\")",
                            "required": True}
    if not params:
        return None                      # nothing varies → not a reusable skill
    steps = []
    for c in calls:
        args = {}
        for k, v in (c.get("args") or {}).items():
            if isinstance(v, str) and v.strip().lower() in value_to_name:
                args[k] = "{" + value_to_name[v.strip().lower()] + "}"
            else:
                args[k] = v
        steps.append({"tool": c["tool"], "args": args})
    template = command
    for val, name in sorted(value_to_name.items(), key=lambda kv: -len(kv[0])):
        template = re.sub(rf"\b{re.escape(val)}\b", "{" + name + "}", template, flags=re.I)
    return params, steps, template


# Never learned as replayable steps: typing/sending reaches other people or
# documents, and tab hopping was the model flailing (live session 2026-11-12).
_UNSAFE_STEPS = {"type_text", "type_and_submit", "type_text_into_element", "switch_tab",
                 "send_chat_message", "gmail_send", "send_email", "delete_file",
                 "set_humor_level", "set_preference", "remove_preference", "save_memory",
                 "learn_app", "confirm_and_execute"}
_META = re.compile(r"\b(remember|spell(?:ed|t)?|meant|preference|humou?r|learn|study|good job|"
                   r"that'?s wrong|did nothing|didn'?t work|when i say)\b", re.I)
_PENDING_PATH = os.path.join(os.path.expanduser("~"), ".demascas", "skills", "_pending.json")


def skill_material(traj: dict, known_tools: set[str]) -> str:
    """'' if the run may become a skill, else the reason it can't."""
    if not traj.get("success") or traj.get("feedback") == "negative":
        return "did not succeed"
    if traj.get("skill_id"):
        return "already a skill"
    raw = [c for c in traj.get("tool_calls", []) if c.get("tool") in known_tools]
    if any(not c.get("ok") for c in raw):
        return "a step failed"            # honest runs only — no flailing
    if any(c.get("tool") in _UNSAFE_STEPS for c in raw):
        return "contains typing/sending or settings changes"
    if _META.search(traj.get("command", "")):
        return "a request about DEMASCAS itself, not a task"
    if len(raw) < 2 or not [c for c in raw if c["tool"] not in _READ_ONLY]:
        return "too simple"
    return ""


def _signature(steps: list[dict]) -> str:
    return "|".join(f"{s['tool']}:{sorted((s.get('args') or {}).items())}" for s in steps)


def _seen_before(sig: str, command: str) -> bool:
    """Two-sightings rule: a procedure becomes a candidate only after it
    worked for two DIFFERENT requests (or the user praised it)."""
    try:
        with open(_PENDING_PATH) as f:
            pending = json.load(f)
    except (FileNotFoundError, ValueError):
        pending = {}
    prev = pending.get(sig)
    if prev and prev != command:
        pending.pop(sig, None)
        hit = True
    else:
        pending[sig] = command
        hit = False
    os.makedirs(os.path.dirname(_PENDING_PATH), exist_ok=True)
    with open(_PENDING_PATH, "w") as f:
        json.dump(dict(list(pending.items())[-200:]), f)
    return hit


def prune_unsafe_learned() -> list[str]:
    """Disable learned skills that replay typing/sending or settings changes
    (made before these rules existed).  Returns their names."""
    store = get_store()
    gone = []
    for sk in list(store.skills.values()):
        if sk.source == "learned" and sk.status != "disabled" and (
                any(st.get("tool") in _UNSAFE_STEPS for st in sk.steps) or _META.search(sk.description)):
            sk.status = "disabled"
            store.save(sk)
            gone.append(sk.name)
    return gone


def induce_from_trajectory(traj: dict, known_tools: set[str], paraphrase: bool = True,
                           praised: bool = False) -> Skill | None:
    """Return a saved candidate skill, or None if this run isn't skill material."""
    if skill_material(traj, known_tools):
        return None
    calls = [c for c in traj.get("tool_calls", []) if c.get("ok") and c.get("tool") in known_tools]
    command = traj.get("command", "")
    store = get_store()
    if store.match(command)[0] is not None:
        return None                       # already covered by a skill
    gen = _generalize(command, calls)
    if gen is None:
        return None
    params, steps, template = gen
    if not praised and not _seen_before(_signature(steps), command):
        return None                       # wait for a second, different success

    # Generic phrasings of the template (placeholders → neutral words) for matching.
    neutral = lambda t: re.sub(r"\{(\w+)\}", lambda m: m.group(1).replace("_", " "), t)
    intents = [command, neutral(template)]
    if paraphrase:
        try:
            from actuators.documents import _llm_json
            got = _llm_json(_PARAPHRASE_PROMPT.format(template=template), max_tokens=150) or {}
            for it in (got.get("intents") or [])[:3]:
                if isinstance(it, str) and set(re.findall(r"\{(\w+)\}", it)) <= set(params):
                    intents.append(neutral(it))
        except RuntimeError:
            pass

    # Precision guards: the command words that name what the tools DO
    # ("youtube", "search", "play") must all be present (synonyms allowed).
    # Embedding similarity alone is dominated by the parameter value —
    # "search google for lofi beats" scored higher than a real reuse.
    fixed = re.sub(r"\{\w+\}", " ", template.lower())
    tool_words = set(re.findall(r"[a-z]+", " ".join(c["tool"] for c in steps).replace("_", " ")))
    words = [w for w in dict.fromkeys(re.findall(r"[a-z]{3,}", fixed))
             if w not in _STOP and (w in tool_words or any(w in _SYNONYMS.get(t, ()) for t in tool_words))]
    if not words:
        words = [w for w in dict.fromkeys(re.findall(r"[a-z]{3,}", fixed)) if w not in _STOP][:1]

    name = neutral(template)[:60]
    sk = Skill(
        id=f"learned_{_slug(name)}",
        name=name,
        description=f"Learned from: {command}",
        intents=list(dict.fromkeys(intents)),
        params=params,
        steps=steps,
        say="Done.",
        source="learned",
        status="candidate",
        require=[_guard(w) for w in words[:3]],
    )
    if sk.id in store.skills:
        return None                       # already learned this procedure
    store.save(sk)
    print(f"[SKILLS] Learned candidate skill '{sk.name}' ({len(steps)} steps) from: {command}")
    return sk


def parse_teaching(command: str) -> tuple[str, list[str]] | None:
    """'when I say good morning, open my calendar and play lofi'
       → ('good morning', ['open my calendar', 'play lofi'])"""
    m = re.match(r"^(?:when(?:ever)?|if)\s+i\s+say\s+[\"']?(.+?)[\"']?\s*(?:,|then|you should)\s*(.+)$",
                 command.strip(), re.IGNORECASE)
    if not m:
        return None
    trigger, body = m.group(1).strip(), m.group(2).strip()
    parts = [p.strip(" .") for p in re.split(r",\s*|\s+and then\s+|\s+then\s+|\s+and\s+", body) if p.strip(" .")]
    return (trigger, parts) if trigger and parts else None


def teach(command: str) -> Skill | None:
    """Create a taught skill whose steps are sub-commands run in order."""
    parsed = parse_teaching(command)
    if not parsed:
        return None
    trigger, parts = parsed
    words = [w for w in re.findall(r"[a-z]+", trigger.lower()) if len(w) > 2][:2]
    sk = Skill(
        id=f"taught_{_slug(trigger)}",
        name=trigger[:60],
        description=f"Taught: {command}",
        intents=[trigger, f"{trigger} please", f"do my {trigger} routine"],
        steps=[{"tool": "__command__", "args": {"command": p}} for p in parts],
        say=f"{trigger[:1].upper() + trigger[1:]} routine done.",
        source="taught",
        status="active",
        min_score=0.55,
        require=[rf"\b{re.escape(w)}" for w in words],
    )
    get_store().save(sk)
    return sk
