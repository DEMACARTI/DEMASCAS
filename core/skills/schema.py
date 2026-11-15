"""
DEMASCAS — core/skills/schema.py
A Skill is a reusable, learned procedure: "when the user asks for something
like THIS, do THESE steps".  Skills live outside the model (JSON files) so
DEMASCAS learns instantly — no training needed — and runs them without the
slow step-by-step agent loop.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class Skill:
    id: str
    name: str
    description: str
    intents: list[str]                       # example phrasings (embedded for matching)
    params: dict = field(default_factory=dict)
    # params: {name: {"description": str, "required": bool,
    #                 "type": "string"|"integer", "from_command": bool}}
    #   from_command=True → the whole user command is passed (no LLM call)
    program: str = ""                        # one tool called with the params...
    steps: list = field(default_factory=list)  # ...or tool calls with {param} placeholders
    say: str = ""                            # spoken on success ({param} allowed)
    ack: str = ""                            # spoken BEFORE a slow skill runs
    app: str = ""
    source: str = "builtin"                  # builtin | learned | taught | imported
    status: str = "active"                   # candidate | active | disabled
    min_score: float = 0.0                   # per-skill match threshold override
    # Precision guards (regexes, case-insensitive): ALL `require` must match
    # and NO `exclude` may match.  Embedding similarity says what a command is
    # ABOUT; guards make sure it asks for THIS action ("open powerpoint" must
    # never trigger "create a presentation").
    require: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    # Fast parameter extraction: {param: regex with ONE capture group}.
    # When every required param is found this way the LLM call is skipped
    # (~1-1.5 s saved before DEMASCAS can even acknowledge).
    extract: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {
        "uses": 0, "successes": 0, "failures": 0, "total_ms": 0, "verified_reuses": 0})
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def avg_ms(self) -> float:
        u = self.stats.get("uses", 0)
        return self.stats.get("total_ms", 0) / u if u else 0.0
