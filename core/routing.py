"""
DEMASCAS — core/routing.py
Chat or action?  Decided once, on the server, inside the /skill/match call
the daemon already makes (no extra round-trip).

Keyword lists in the daemon defaulted to "chat" whenever no verb they knew
appeared — so "send a message to Hatem on Discord" was answered by the chat
model, which then CLAIMED to have sent it.  Here a nearest-neighbour check
against example commands (cached embedder, ~1 ms) decides, plus hard rules:
naming a learned app, messaging, realtime data or the screen ⇒ action.
When unsure the route is "" and the daemon keeps its own fallback rules.
"""

from __future__ import annotations

import re
import threading

import numpy as np

_CHAT = [
    "how are you", "how's your day going", "good morning", "thank you so much",
    "I watched a movie called Ramayana", "my day was good", "tell me a joke",
    "who was Albert Einstein", "explain recursion simply", "what is the capital of France",
    "what do you think about artificial intelligence", "can you explain how gravity works",
    "what does photosynthesis mean", "translate hello to Spanish", "give me a motivational quote",
    "I'm feeling tired today", "that's interesting", "tell me a fun fact",
    "what's the difference between a virus and bacteria", "write a short poem about rain",
    "how do airplanes fly", "nice to meet you", "what's your name", "who made you",
    "recommend a good book", "how many planets are there", "define entropy",
    "what is two plus two times three", "do you like music", "good night",
]
_ACTION = [
    "send a message to Hatem on Discord saying hi", "message my friend that I'm late",
    "text mom I'll be home soon", "dm John on Slack", "reply to Sarah on WhatsApp",
    "open Discord", "open the friends section in Discord", "mute my microphone",
    "play some lofi music", "create a presentation about black holes", "search YouTube for jazz",
    "what's on my screen", "remember that my brother's name is Neon", "close Safari",
    "show my downloads folder", "is Discord open", "check my email", "call John",
    "take a screenshot", "turn up the volume", "find my resume", "set a timer for ten minutes",
    "what's the weather in Delhi", "go to the next channel", "upload a file in Discord",
    "send hi to Hatem Thai", "post this in the general channel", "share this file with Aman",
    "switch to Chrome", "type hello world", "what time is it", "learn how to use Notion",
]
_FORCE_ACTION = re.compile(
    r"\b(send|message|text|dm|reply|post|share|call|email|mail|whatsapp|discord|slack|telegram|"
    r"open|close|launch|quit|play|pause|mute|unmute|type|click|press|screenshot|upload|download|"
    r"remember|remind|timer|alarm|volume|brightness|wifi|bluetooth|screen|clipboard|folder|file|"
    r"weather|news|price|score|today|tomorrow|now|latest|learn|study)\b", re.I)

_lock = threading.Lock()
_index: dict | None = None


def _build():
    from core.embeddings import embed
    vecs = np.array(embed(_CHAT + _ACTION), dtype=np.float32)
    return {"chat": vecs[:len(_CHAT)], "action": vecs[len(_CHAT):]}


def intent(command: str) -> tuple[str, float]:
    """('chat'|'action'|'', margin).  '' = not sure."""
    global _index
    from core.embeddings import embed
    text = (command or "").strip()
    if not text:
        return "", 0.0
    with _lock:
        if _index is None:
            _index = _build()
    q = np.array(embed([text])[0], dtype=np.float32)
    # mean of the 3 nearest examples per class — robust to one odd example
    top = lambda m: float(np.sort(m @ q)[-3:].mean())
    chat, action = top(_index["chat"]), top(_index["action"])
    margin = action - chat
    if _FORCE_ACTION.search(text):
        return "action", max(margin, 0.0)
    try:
        from core.appknowledge.learner import apps_in_text
        if apps_in_text(text):
            return "action", max(margin, 0.0)
    except Exception:
        pass
    if margin <= -0.04:
        return "chat", margin
    if margin >= 0.04:
        return "action", margin
    return "", margin


_FRAGMENT = re.compile(r"^[\w' -]+$")


def unclear(command: str) -> bool:
    """Speech-recognition debris, not a request: three or more comma-separated
    scraps of one or two words ("run to, delete, cloud").  Acting on it once
    opened a random website."""
    parts = [p.strip() for p in re.split(r"[,;]", command or "") if p.strip()]
    if len(parts) < 3:
        return False
    short = [p for p in parts if len(p.split()) <= 2 and _FRAGMENT.match(p)]
    return len(short) == len(parts) and not re.search(r"\b(and|or|then)\b", command, re.I)


def route(command: str) -> str:
    """'chat' | 'agent' | 'tool' | 'unclear' | '' (daemon decides)."""
    if unclear(command):
        return "unclear"
    kind, _ = intent(command)
    if kind == "chat":
        return "chat"
    if kind == "":
        return ""
    try:
        from agents.dispatcher import should_use_agent
    except Exception:
        try:
            from server.agents.dispatcher import should_use_agent
        except Exception:
            return "tool"
    if should_use_agent(command):
        return "agent"
    try:
        from core.appknowledge.learner import apps_in_text
        if apps_in_text(command):
            return "agent"            # driving a learned app = multi-step, manual-aware
    except Exception:
        pass
    return "tool"
