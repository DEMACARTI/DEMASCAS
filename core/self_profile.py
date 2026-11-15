"""
DEMASCAS — core/self_profile.py
What DEMASCAS is and does — used when the user asks it to talk about
ITSELF ("make a presentation about yourself").  A web search for
"yourself" returned junk and the outline failed in the live session.

Built from the project's own README (first sections, markdown stripped),
plus capabilities that live in code, so it stays current with the project.
"""

from __future__ import annotations

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SELF = re.compile(r"^(?:you|yourself|your ?self|demascas|this assistant|my assistant|"
                   r"(?:about\s+)?(?:you|yourself)(?:\s*,?\s*demascas)?|demascas\s*\(?you\)?)$", re.I)

_CAPABILITIES = (
    "DEMASCAS is a local voice assistant (a JARVIS for your laptop): it listens for its wake word, "
    "understands speech on-device, and speaks back with a neural voice. "
    "It controls apps and the Mac: opens apps, clicks and types, reads the screen, browses the web, "
    "manages files, email and documents, and builds PowerPoint, Word and Excel files. "
    "It learns: it studies new apps from their menus, scripting dictionaries and help pages, turns "
    "successful multi-step tasks into reusable skills, takes spoken feedback, and can fine-tune its "
    "own model on verified experience when the laptop is idle and plugged in. "
    "It keeps listening while it works, can be interrupted at a normal voice, and asks before "
    "sending messages or deleting anything. Everything runs on-device with a plug-in local LLM."
)


def is_self_topic(topic: str) -> bool:
    t = re.sub(r"[^\w\s,()]", "", (topic or "")).strip()
    return bool(_SELF.match(t)) or t.lower() in {"demascas", "you", "yourself"}


def self_facts(max_chars: int = 1800) -> str:
    text = ""
    try:
        with open(os.path.join(_ROOT, "README.md"), encoding="utf-8") as f:
            raw = f.read(6000)
        raw = raw.split("\n## Stability")[0]          # intro + latest update, not changelogs
        raw = re.sub(r"`{1,3}[^`]*`{1,3}", "", raw)
        raw = re.sub(r"[#>*_\[\]]|\(http[^)]*\)|\(docs/[^)]*\)", "", raw)
        text = re.sub(r"\n{2,}", "\n", raw).strip()
    except OSError:
        pass
    return (_CAPABILITIES + "\n" + text)[:max_chars]
