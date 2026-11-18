"""
DEMASCAS — core/skills/runner.py
Match a command to a skill, fill its parameters, run it, verify, learn.

Latency budget: match ≈ 0.1–2 ms; parameter filling = ONE short JSON-only
LLM call (skipped when a skill takes the whole command or has no params);
execution is deterministic tool calls — no step-by-step agent loop.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from core.skills.schema import Skill
from core.skills.store import get_store

# dispatch(name, args) -> (result_text, failed, raw)
Dispatch = Callable[[str, dict], tuple]

# Words that don't count as evidence a parameter value came from the command.
_GENERIC = {"the", "and", "for", "about", "with", "presentation", "slides", "slide", "deck",
            "document", "spreadsheet", "report", "make", "create", "some", "new", "topic",
            "powerpoint", "excel", "word", "file"}

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10}

PROMOTE_AFTER = 2   # candidate → active after this many verified successes
DISABLE_AFTER = 3   # consecutive failures before a learned skill is disabled


@dataclass
class SkillMatch:
    skill: Skill
    score: float
    params: dict
    missing: list[str] = field(default_factory=list)


@dataclass
class SkillRun:
    success: bool
    summary: str
    tool_calls: list[str]
    ms: float
    results: list[str] = field(default_factory=list)
    pending: dict | None = None        # a step is waiting for the user's yes/no
    file: str = ""                     # a file a step made ("open it" next means this)


def _short(v):
    """Spoken read-backs use the first clause, ≤ 6 words ("about you,
    meaning DEMASCAS, and it should say…" → "you")."""
    if not isinstance(v, str):
        return v
    first = re.split(r"\s*(?:[,;]| and | which | that | meaning )\s*", v.strip())[0]
    words = first.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


def say_fmt(template: str, params: dict) -> str:
    """For what DEMASCAS SPEAKS (ack / success line): short read-backs."""
    return _fmt(template, {k: _short(v) for k, v in params.items()})


def _fmt(template: str, params: dict) -> str:
    """Exact substitution (also fills learned-skill arguments)."""
    try:
        return template.format(**{k: v for k, v in params.items()})
    except (KeyError, IndexError, ValueError):
        return re.sub(r"\{[^}]*\}", "", template).strip()


def _fill_params(skill: Skill, command: str) -> tuple[dict, list[str]]:
    params: dict = {}
    to_ask: dict = {}
    for name, spec in (skill.params or {}).items():
        if spec.get("from_command"):
            params[name] = command
            continue
        if spec.get("type") == "app":
            # "discord so you can use it" → "Discord" (an installed app's real name)
            rx = (skill.extract or {}).get(name)
            m = re.search(rx, command, re.I) if rx else None
            from core.appknowledge.learner import _resolve_app_name, apps_in_text
            found = _resolve_app_name(m.group(1)) if m else ""
            pronoun = (not m) or m.group(1).strip().lower() in {
                "it", "this", "that", "them", "this app", "that app", "the app"}
            if not found and pronoun:
                # "…open Discord and help me use IT for study" — the app named
                # elsewhere in the sentence (learned apps first, then installed).
                named = apps_in_text(command)
                if not named:
                    from core.appnames import installed_apps
                    named = [a for a in installed_apps()
                             if len(a) > 3 and re.search(rf"\b{re.escape(a)}\b", command, re.I)]
                found = named[0] if named else ""
            if found or m:
                params[name] = found or m.group(1).strip()
                continue
        rx = (skill.extract or {}).get(name)
        m = re.search(rx, command, re.I) if rx else None
        if m and m.group(1).strip():
            val = m.group(1).strip(" .,!?\"'")
            if spec.get("type") == "integer":
                val = _NUM_WORDS.get(val.lower(), val)
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    to_ask[name] = spec
                    continue
            else:
                val = re.sub(r"^(?:the|a|an|my)\s+", "", val, flags=re.I)
            params[name] = val
        else:
            to_ask[name] = spec
    # Only consult the LLM if a REQUIRED value is still unknown.
    if not any(s.get("required") for s in to_ask.values()):
        to_ask = {}
    if to_ask:
        from core.config import LLM_CONFIGURED
        if LLM_CONFIGURED:
            from actuators.documents import _llm_json
            lines = "\n".join(
                f'- "{n}" ({s.get("type", "string")}{", optional" if not s.get("required") else ""}): '
                f'{s.get("description", "")}' for n, s in to_ask.items())
            prompt = (f'User request: "{command}"\n'
                      f"Extract these values from the request:\n{lines}\n"
                      "Use null for values the user did not give. Return a JSON object with exactly these keys.")
            try:
                got = _llm_json(prompt, max_tokens=120) or {}
            except RuntimeError:
                got = {}
            cmd_words = set(re.findall(r"[a-z0-9]{3,}", command.lower()))
            for n, s in to_ask.items():
                v = got.get(n)
                if v in (None, "", "null"):
                    continue
                if s.get("type") != "integer":
                    # Small models invent values ("make me some slides" →
                    # topic "make it clear").  A real value shares at least
                    # one content word with what the user said.
                    vw = set(re.findall(r"[a-z0-9]{3,}", str(v).lower())) - _GENERIC
                    if not vw or not (vw & cmd_words):
                        continue
                if s.get("type") == "integer":
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                elif len(str(v)) >= 0.8 * len(command):
                    continue           # model echoed the whole request
                params[n] = v
    # Learned single-parameter skills: the value is what's left of the
    # command once the skill's own fixed words are removed
    # ("find rain sounds on youtube and play the first one" → "rain sounds").
    req = [n for n, sp in (skill.params or {}).items() if sp.get("required") and not sp.get("from_command")]
    if skill.source == "learned" and len(req) == 1 and req[0] not in params:
        fixed = set()
        for it in skill.intents[1:]:
            fixed |= set(re.findall(r"[a-z]+", it.lower()))
        fixed |= {"the", "a", "an", "on", "in", "for", "and", "of", "to", "one", "me", "please", "some"}
        guard_words = set(re.findall(r"[a-z]+", " ".join(skill.require)))
        best, cur = [], []
        for tok in re.findall(r"[\w'-]+", command):
            if tok.lower() in fixed or tok.lower() in guard_words:
                best, cur = (cur if len(cur) > len(best) else best), []
            else:
                cur.append(tok)
        best = cur if len(cur) > len(best) else best
        if best:
            params[req[0]] = " ".join(best)
    missing = [n for n, s in (skill.params or {}).items() if s.get("required") and n not in params]
    return params, missing


def match(command: str) -> SkillMatch | None:
    """Find a skill for the command and fill its parameters."""
    sk, score, _ = get_store().match(command)
    if sk is None:
        return None
    params, missing = _fill_params(sk, command)
    return SkillMatch(sk, score, params, missing)


def _substitute(obj, params: dict):
    if isinstance(obj, str):
        m = re.fullmatch(r"\{(\w+)\}", obj)
        if m and m.group(1) in params:
            return params[m.group(1)]          # keep non-string types
        return _fmt(obj, params)
    if isinstance(obj, dict):
        return {k: _substitute(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, params) for v in obj]
    return obj


def run(skill: Skill, params: dict, dispatch: Dispatch) -> SkillRun:
    t0 = time.time()
    calls: list[str] = []
    results: list[str] = []
    ok = True
    steps = ([{"tool": skill.program, "args": params}] if skill.program
             else [{"tool": s["tool"], "args": _substitute(s.get("args", {}), params)}
                   for s in skill.steps])
    last_msg = ""
    made = ""
    for st in steps:
        result, failed, raw = dispatch(st["tool"], st.get("args") or {})
        calls.append(st["tool"])
        results.append(result[:300])
        if isinstance(raw, dict) and raw.get("status") == "pending_confirmation":
            ms = (time.time() - t0) * 1000
            return SkillRun(True, str(raw.get("confirm_speech", "Should I go ahead?")), calls, ms,
                            results, pending=raw)
        if isinstance(raw, dict) and raw.get("message"):
            last_msg = str(raw["message"])
        if isinstance(raw, dict) and raw.get("path") and not failed:
            made = str(raw["path"])
        if failed:
            ok = False
            last_msg = last_msg or result
            break
    ms = (time.time() - t0) * 1000
    record_outcome(skill, ok, ms)
    if ok:
        summary = say_fmt(skill.say, params) if skill.say else (last_msg or "Done.")
    else:
        msg = re.sub(r"^\[-\]\s*", "", last_msg or "")
        summary = msg if msg and len(msg) < 140 else f"I couldn't finish \"{skill.name}\"."
    return SkillRun(ok, summary, calls, ms, results, file=made)


def record_outcome(skill: Skill, success: bool, ms: float = 0.0, verified: bool = False) -> None:
    """Update stats; promote proven candidates, disable failing learned skills."""
    st = skill.stats
    st["uses"] = st.get("uses", 0) + 1
    st["total_ms"] = st.get("total_ms", 0) + int(ms)
    if success:
        st["successes"] = st.get("successes", 0) + 1
        st["consecutive_failures"] = 0
        if verified:
            st["verified_reuses"] = st.get("verified_reuses", 0) + 1
        if skill.status == "candidate" and st.get("successes", 0) >= PROMOTE_AFTER:
            skill.status = "active"
            print(f"[SKILLS] Promoted learned skill: {skill.name}")
    else:
        st["failures"] = st.get("failures", 0) + 1
        st["consecutive_failures"] = st.get("consecutive_failures", 0) + 1
        if skill.source != "builtin" and st["consecutive_failures"] >= DISABLE_AFTER:
            skill.status = "disabled"
            print(f"[SKILLS] Disabled unreliable skill: {skill.name}")
    get_store().save(skill)


def penalize(skill_id: str) -> bool:
    """User said "that's wrong" right after this skill ran."""
    store = get_store()
    sk = store.skills.get(skill_id)
    if not sk:
        return False
    record_outcome(sk, success=False)
    if sk.source != "builtin" and sk.status == "active" and sk.stats.get("failures", 0) >= 2:
        sk.status = "candidate"       # needs to prove itself again
        store.save(sk)
    return True
