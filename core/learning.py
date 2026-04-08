"""
DEMASCAS — core/learning.py
Continuous Learning Engine — makes DEMASCAS hyper-intelligent over time.

Three layers, all persisted to ~/.demascas/:

1. EPISODIC MEMORY  (ChromaDB collection: demascas_episodes)
   Auto-logs every command + its outcome.  Semantic search finds what
   worked for similar past commands.  Capped at 500 episodes; older
   entries are compressed into distilled patterns.

2. USER PROFILE  (JSON: ~/.demascas/user_profile.json)
   Extracted preferences, personal facts, app usage stats, correction
   history, and workflow patterns.  Updated after every interaction via
   lightweight heuristics — zero extra LLM calls.

3. CONTEXT BUILDER  (pre-command injection)
   Before each LLM call, builds a compact learned-context block:
     - Relevant past episodes (what worked for similar commands)
     - User profile summary (name, preferences, habits)
     - Recent corrections (avoid repeating past mistakes)
   Injected into the system prompt so the LLM sees personalized context.

RAM BUDGET: ~0 MB extra.
  - Reuses the SAME ChromaDB client and embedding model as core/memory.py.
  - User profile is a tiny JSON file (<50 KB even after 1000s of sessions).
  - No background threads, no timers, no extra processes.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any

# IST offset (matches agent.py / memory.py)
_IST = timezone(timedelta(hours=5, minutes=30))

# Persistent storage
_DEMASCAS_DIR = os.path.expanduser("~/.demascas")
_PROFILE_PATH = os.path.join(_DEMASCAS_DIR, "user_profile.json")

# Episode limits — keeps ChromaDB lean on 8 GB
_MAX_EPISODES = 500
_COMPRESS_THRESHOLD = 550  # when we hit this, compress down to _MAX_EPISODES

# Lazy-loaded ChromaDB collection for episodes
_episodes_collection = None

# In-memory profile cache — avoids re-reading JSON on every call.
# Dirty flag controls when we flush to disk.
_profile_cache: dict | None = None
_profile_dirty: bool = False
# Episode write counter — flush profile to disk every N commands, not every command
_flush_interval: int = 5
_write_counter: int = 0


# ═══════════════════════════════════════════════════════════════════════
# 1. EPISODIC MEMORY  — auto-logged, semantically searchable
# ═══════════════════════════════════════════════════════════════════════

def _get_episodes():
    """
    Lazy-init the episodes collection.

    CRITICAL: reuses the SAME ChromaDB PersistentClient that memory.py
    already created.  ChromaDB's PersistentClient loads the all-MiniLM-L6-v2
    embedding model (~80 MB) — two clients would waste 80 MB.
    """
    global _episodes_collection
    if _episodes_collection is not None:
        return _episodes_collection
    try:
        # Import memory.py's lazy-loaded client to reuse it
        from core.memory import _get_collection as _get_mem_collection

        # Calling _get_mem_collection() forces memory.py to init its client.
        # We then grab the same client object from its module globals.
        _get_mem_collection()
        from core import memory as _mem_mod
        client = _mem_mod._client

        if client is None:
            # memory.py failed to init — fall back to our own client
            import chromadb
            memory_dir = os.path.join(_DEMASCAS_DIR, "memory")
            os.makedirs(memory_dir, exist_ok=True)
            client = chromadb.PersistentClient(path=memory_dir)

        _episodes_collection = client.get_or_create_collection(
            name="demascas_episodes",
            metadata={"hnsw:space": "cosine"},
        )
        count = _episodes_collection.count()
        if count > 0:
            print(f"[🧠 LEARN] Loaded {count} past interaction(s)")
        return _episodes_collection
    except ImportError:
        return None
    except Exception as e:
        print(f"[!] Learning init failed: {e}")
        return None


def log_episode(
    command: str,
    path_taken: str,        # "shortcut" | "fast" | "tool"
    tools_used: list[str],
    result_summary: str,
    success: bool = True,
) -> None:
    """
    Auto-log an interaction for future retrieval.
    Called after every command — completely invisible to the user.
    """
    collection = _get_episodes()
    if collection is None:
        return

    now = datetime.now(_IST)
    doc_id = hashlib.md5(
        f"{command}:{now.isoformat()}".encode()
    ).hexdigest()

    # Truncate result to keep storage lean
    result_short = result_summary[:300] if result_summary else ""
    tools_str = ",".join(tools_used) if tools_used else "none"

    try:
        collection.add(
            ids=[doc_id],
            documents=[command],
            metadatas=[{
                "timestamp": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "hour": str(now.hour),
                "path": path_taken,
                "tools": tools_str,
                "result": result_short,
                "success": str(success).lower(),
            }],
        )
    except Exception:
        pass  # Never block the main flow

    # Periodic compression check (every ~50 new episodes)
    try:
        if collection.count() > _COMPRESS_THRESHOLD:
            _compress_episodes(collection)
    except Exception:
        pass


def get_relevant_episodes(query: str, top_k: int = 3) -> list[dict]:
    """
    Semantic search over past interactions.
    Returns list of dicts: {command, path, tools, result, date, success}
    """
    collection = _get_episodes()
    if collection is None or collection.count() == 0:
        return []

    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(top_k, collection.count()),
        )
        if not results["documents"] or not results["documents"][0]:
            return []

        episodes = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            # Only include reasonably similar episodes (cosine < 0.7)
            if dist > 0.7:
                continue
            episodes.append({
                "command": doc,
                "path": meta.get("path", "?"),
                "tools": meta.get("tools", "none"),
                "result": meta.get("result", ""),
                "date": meta.get("date", "?"),
                "success": meta.get("success", "true") == "true",
                "similarity": round(1.0 - dist, 2),
            })
        return episodes
    except Exception:
        return []


def _compress_episodes(collection) -> None:
    """
    When episodes exceed the threshold, delete the oldest ones.
    Keeps the collection bounded for 8 GB RAM.
    """
    try:
        count = collection.count()
        if count <= _COMPRESS_THRESHOLD:
            return

        # Get all, sorted by timestamp, delete oldest
        all_data = collection.get(
            include=["metadatas"],
        )
        if not all_data["ids"]:
            return

        # Sort by timestamp (oldest first)
        pairs = list(zip(all_data["ids"], all_data["metadatas"]))
        pairs.sort(key=lambda p: p[1].get("timestamp", ""))

        # Delete the oldest batch to get back under _MAX_EPISODES
        to_delete = len(pairs) - _MAX_EPISODES
        if to_delete > 0:
            delete_ids = [p[0] for p in pairs[:to_delete]]
            collection.delete(ids=delete_ids)
            print(f"[🧠 LEARN] Compressed episodes: deleted {to_delete} oldest, kept {_MAX_EPISODES}")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# 2. USER PROFILE  — persistent JSON, updated by heuristics
# ═══════════════════════════════════════════════════════════════════════

def _load_profile() -> dict:
    """
    Load user profile — cached in-memory after first read.
    Disk I/O only on cold start.
    """
    global _profile_cache
    if _profile_cache is not None:
        return _profile_cache

    os.makedirs(_DEMASCAS_DIR, exist_ok=True)
    if os.path.exists(_PROFILE_PATH):
        try:
            with open(_PROFILE_PATH, "r") as f:
                _profile_cache = json.load(f)
                return _profile_cache
        except Exception:
            pass

    # Fresh profile
    _profile_cache = {
        "name": None,
        "facts": [],               # ["studies CS", "likes dark mode", ...]
        "app_usage": {},            # {"Chrome": 45, "VS Code": 30, ...}
        "corrections": [],          # [{"wrong": "...", "right": "...", "ts": "..."}]
        "frequent_commands": {},    # {"open chrome": 12, "screenshot": 8, ...}
        "workflows": [],            # [{"trigger": "work mode", "steps": [...], "count": 3}]
        "interaction_count": 0,
        "first_seen": datetime.now(_IST).isoformat(),
        "last_active": datetime.now(_IST).isoformat(),
    }
    return _profile_cache


def _save_profile(profile: dict) -> None:
    """
    Mark profile as dirty.  Actual disk flush happens every _flush_interval
    commands (via _maybe_flush_profile) to avoid hammering the SSD.
    """
    global _profile_cache, _profile_dirty
    _profile_cache = profile
    _profile_dirty = True


def _flush_profile_now() -> None:
    """Force-write profile to disk.  Called periodically or at shutdown."""
    global _profile_dirty
    if not _profile_dirty or _profile_cache is None:
        return
    try:
        os.makedirs(_DEMASCAS_DIR, exist_ok=True)
        tmp = _PROFILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_profile_cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _PROFILE_PATH)
        _profile_dirty = False
    except Exception:
        pass


def _maybe_flush_profile() -> None:
    """Flush profile to disk every N writes (batched I/O)."""
    global _write_counter
    _write_counter += 1
    if _write_counter >= _flush_interval:
        _write_counter = 0
        _flush_profile_now()


def update_profile(
    command: str,
    tools_used: list[str],
    result: str,
    success: bool = True,
) -> None:
    """
    Extract preferences and patterns from the interaction and update
    the user profile.  Uses lightweight heuristics — no LLM calls.
    """
    profile = _load_profile()
    now = datetime.now(_IST)
    profile["last_active"] = now.isoformat()
    profile["interaction_count"] = profile.get("interaction_count", 0) + 1

    cmd_lower = command.strip().lower()

    # ── Extract personal facts ──────────────────────────────────
    _extract_facts(cmd_lower, command, profile)

    # ── Track app usage ─────────────────────────────────────────
    _track_app_usage(tools_used, profile)

    # ── Track command frequency ─────────────────────────────────
    # Normalize to first 60 chars to group similar commands
    cmd_key = cmd_lower[:60].strip()
    freq = profile.get("frequent_commands", {})
    freq[cmd_key] = freq.get(cmd_key, 0) + 1
    # Keep only top 50 commands to bound file size
    if len(freq) > 50:
        sorted_cmds = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        freq = dict(sorted_cmds[:50])
    profile["frequent_commands"] = freq

    # ── Detect corrections ──────────────────────────────────────
    _detect_correction(cmd_lower, command, result, profile, now)

    _save_profile(profile)
    _maybe_flush_profile()


def _extract_facts(cmd_lower: str, cmd_original: str, profile: dict) -> None:
    """Extract personal facts from the command text."""
    facts = profile.get("facts", [])

    # Name detection
    name_patterns = [
        "my name is ", "i am ", "i'm ", "call me ", "they call me ",
    ]
    for pat in name_patterns:
        if pat in cmd_lower:
            after = cmd_original[cmd_lower.index(pat) + len(pat):].strip()
            name = after.split(".")[0].split(",")[0].split("!")[0].strip()
            if name and len(name) < 40:
                # Only extract name from "my name is" and "call me"
                if pat in ("my name is ", "call me ", "they call me "):
                    profile["name"] = name
                fact = f"User's name: {name}"
                if fact not in facts:
                    facts.append(fact)

    # Preference detection
    pref_patterns = [
        ("i prefer ", "User prefers "),
        ("i like ", "User likes "),
        ("i love ", "User loves "),
        ("i hate ", "User hates "),
        ("i don't like ", "User doesn't like "),
        ("i always use ", "User always uses "),
        ("i usually use ", "User usually uses "),
        ("my favorite ", "User's favorite "),
        ("my favourite ", "User's favourite "),
    ]
    for trigger, prefix in pref_patterns:
        if trigger in cmd_lower:
            after = cmd_original[cmd_lower.index(trigger) + len(trigger):].strip()
            value = after.split(".")[0].split(",")[0].strip()
            if value and len(value) < 80:
                fact = f"{prefix}{value}"
                if fact not in facts:
                    facts.append(fact)

    # Location / occupation / study
    info_patterns = [
        ("i study ", "User studies "),
        ("i work at ", "User works at "),
        ("i work in ", "User works in "),
        ("i live in ", "User lives in "),
        ("i'm from ", "User is from "),
    ]
    for trigger, prefix in info_patterns:
        if trigger in cmd_lower:
            after = cmd_original[cmd_lower.index(trigger) + len(trigger):].strip()
            value = after.split(".")[0].split(",")[0].strip()
            if value and len(value) < 80:
                fact = f"{prefix}{value}"
                if fact not in facts:
                    facts.append(fact)

    # Cap facts at 30 to bound profile size
    profile["facts"] = facts[:30]


def _track_app_usage(tools_used: list[str], profile: dict) -> None:
    """Track which apps the user opens/activates most."""
    usage = profile.get("app_usage", {})
    for tool in tools_used:
        # We only get tool names here, not arguments.
        # App tracking is enriched by _extract_app_from_tools()
        pass
    profile["app_usage"] = usage


def track_app_open(app_name: str) -> None:
    """
    Called directly when open_application or activate_application is executed.
    Tracks which apps the user uses most.  In-memory only — flushed with
    the next after_command() cycle.  Zero extra disk I/O.
    """
    profile = _load_profile()  # returns cache (no disk read)
    usage = profile.get("app_usage", {})
    usage[app_name] = usage.get(app_name, 0) + 1
    # Keep top 20 apps
    if len(usage) > 20:
        sorted_apps = sorted(usage.items(), key=lambda x: x[1], reverse=True)
        usage = dict(sorted_apps[:20])
    profile["app_usage"] = usage
    _save_profile(profile)  # marks dirty, no disk write


def _detect_correction(
    cmd_lower: str, cmd_original: str, result: str,
    profile: dict, now: datetime,
) -> None:
    """Detect when the user is correcting a previous mistake."""
    correction_triggers = [
        "no ", "no,", "that's wrong", "that's not what i",
        "i meant ", "i said ", "not that", "wrong ",
        "i didn't say", "i didn't mean", "i wanted",
        "try again", "do it again", "redo ",
    ]

    is_correction = any(cmd_lower.startswith(t) or t in cmd_lower
                        for t in correction_triggers)

    if is_correction:
        corrections = profile.get("corrections", [])
        corrections.append({
            "user_said": cmd_original[:200],
            "result": result[:200] if result else "",
            "timestamp": now.isoformat(),
        })
        # Keep last 20 corrections
        profile["corrections"] = corrections[-20:]


def get_user_profile() -> dict:
    """Get the current user profile."""
    return _load_profile()


def get_profile_summary() -> str:
    """
    Build a compact profile summary for system prompt injection.
    Returns empty string if no meaningful data exists yet.
    """
    profile = _load_profile()
    parts = []

    # Name
    name = profile.get("name")
    if name:
        parts.append(f"User's name: {name}")

    # Key facts (max 5 most relevant)
    facts = profile.get("facts", [])
    if facts:
        parts.append("Known facts: " + "; ".join(facts[:5]))

    # Top apps
    usage = profile.get("app_usage", {})
    if usage:
        top_apps = sorted(usage.items(), key=lambda x: x[1], reverse=True)[:5]
        app_str = ", ".join(f"{a}({c})" for a, c in top_apps)
        parts.append(f"Most-used apps: {app_str}")

    # Frequent commands (top 5)
    freq = profile.get("frequent_commands", {})
    if freq:
        top_cmds = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]
        cmd_str = ", ".join(f'"{c}"({n})' for c, n in top_cmds)
        parts.append(f"Frequent commands: {cmd_str}")

    # Recent corrections (last 3)
    corrections = profile.get("corrections", [])
    if corrections:
        recent = corrections[-3:]
        corr_str = "; ".join(
            f'"{c["user_said"][:50]}"'
            for c in recent
        )
        parts.append(f"Recent corrections (avoid these mistakes): {corr_str}")

    # Interaction count
    count = profile.get("interaction_count", 0)
    if count > 0:
        parts.append(f"Total interactions: {count}")

    if not parts:
        return ""

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# 3. CONTEXT BUILDER  — combines episodes + profile for LLM injection
# ═══════════════════════════════════════════════════════════════════════

def get_learned_context(query: str) -> str:
    """
    Build a compact learned-context block for injection into the LLM prompt.
    Combines:
      - User profile summary (who the user is, their preferences)
      - Relevant past episodes (what worked for similar commands)
      - Recent corrections (avoid repeating past mistakes)

    Returns:
        A formatted string, or empty string if nothing useful exists.
    """
    sections = []

    # ── Profile summary ─────────────────────────────────────────
    profile_str = get_profile_summary()
    if profile_str:
        sections.append(f"[USER PROFILE]\n{profile_str}")

    # ── Similar past interactions ────────────────────────────────
    episodes = get_relevant_episodes(query, top_k=3)
    if episodes:
        ep_lines = []
        for ep in episodes:
            tools = ep["tools"] if ep["tools"] != "none" else "direct answer"
            status = "✓" if ep["success"] else "✗"
            ep_lines.append(
                f'  {status} "{ep["command"][:80]}" → {tools} '
                f'(similarity={ep["similarity"]})'
            )
        sections.append("[SIMILAR PAST COMMANDS]\n" + "\n".join(ep_lines))

    if not sections:
        return ""

    return "\n\n".join(sections)


def get_context_for_http(query: str) -> dict:
    """
    Structured version of get_learned_context for the HTTP API.
    Used by the C++ daemon via /context endpoint.
    """
    profile = _load_profile()
    episodes = get_relevant_episodes(query, top_k=3)

    return {
        "profile_summary": get_profile_summary(),
        "episodes": episodes,
        "user_name": profile.get("name"),
        "interaction_count": profile.get("interaction_count", 0),
        "context_text": get_learned_context(query),
    }


# ═══════════════════════════════════════════════════════════════════════
# 4. CONVENIENCE WRAPPERS  — called from handle_command()
# ═══════════════════════════════════════════════════════════════════════

def after_command(
    command: str,
    path_taken: str,
    tools_used: list[str],
    result: str,
    success: bool = True,
) -> None:
    """
    One-call learning hook — called after every command completes.
    Logs the episode AND updates the user profile.  Non-blocking.
    Profile is flushed to disk every _flush_interval commands.
    """
    try:
        log_episode(command, path_taken, tools_used, result, success)
    except Exception:
        pass

    try:
        update_profile(command, tools_used, result, success)
    except Exception:
        pass


def flush() -> None:
    """Force-flush any pending profile writes.  Call at shutdown."""
    _flush_profile_now()


def before_command(query: str) -> str:
    """
    One-call context hook — called before each LLM invocation.
    Returns the learned context string to inject, or empty string.
    """
    try:
        return get_learned_context(query)
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════
# 5. MACRO AUTO-PROMOTION  — learns multi-step sequences over time
# ═══════════════════════════════════════════════════════════════════════
#
# When the user repeats a multi-step command successfully N times,
# the sequence of tools is "promoted" to a macro.  Macros are stored
# in ~/.demascas/macros.json and can be injected into the planner
# or returned via RAG context for the LLM.

_MACROS_PATH = os.path.join(_DEMASCAS_DIR, "macros.json")
_MACRO_PROMOTE_THRESHOLD = 3  # how many successful repeats before promotion


def _load_macros() -> dict:
    """Load macros from disk."""
    if os.path.exists(_MACROS_PATH):
        try:
            with open(_MACROS_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sequences": {}, "promoted": []}


def _save_macros(macros: dict) -> None:
    """Persist macros to disk."""
    try:
        os.makedirs(_DEMASCAS_DIR, exist_ok=True)
        tmp = _MACROS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(macros, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _MACROS_PATH)
    except Exception:
        pass


def record_successful_sequence(
    command: str,
    tools_used: list[str],
    result: str,
) -> None:
    """
    Record a successful multi-step tool sequence.  If the same command
    pattern (normalized) has been executed successfully N times with the
    same tool sequence, promote it to a macro.

    Called from the /learn endpoint when success=True and tools_used has 2+ tools.
    """
    if len(tools_used) < 2:
        return  # only multi-step sequences qualify

    macros = _load_macros()

    # Normalize command to first 80 chars lowercase for grouping
    cmd_key = command.strip().lower()[:80]
    tools_key = ",".join(tools_used)

    sequences = macros.get("sequences", {})
    if cmd_key not in sequences:
        sequences[cmd_key] = {"tools": tools_key, "count": 0, "last_result": ""}

    entry = sequences[cmd_key]

    # Only count if the tool sequence matches (same approach)
    if entry["tools"] == tools_key:
        entry["count"] += 1
        entry["last_result"] = result[:200]
    else:
        # Different approach — reset counter
        entry["tools"] = tools_key
        entry["count"] = 1
        entry["last_result"] = result[:200]

    sequences[cmd_key] = entry
    macros["sequences"] = sequences

    # Check for promotion
    if entry["count"] >= _MACRO_PROMOTE_THRESHOLD:
        promoted = macros.get("promoted", [])
        # Don't duplicate
        existing = [m for m in promoted if m.get("command") == cmd_key]
        if not existing:
            promoted.append({
                "command": cmd_key,
                "tools": tools_used,
                "count": entry["count"],
                "promoted_at": datetime.now(_IST).isoformat(),
            })
            macros["promoted"] = promoted
            print(f"[🧠 MACRO] Promoted: '{cmd_key}' → {tools_used}")

    _save_macros(macros)


def get_promoted_macros() -> list[dict]:
    """Get all promoted macros (successfully repeated multi-step sequences)."""
    macros = _load_macros()
    return macros.get("promoted", [])


def get_all_macros_for_rag(query: str) -> str:
    """
    Build a context string of relevant promoted macros for RAG injection.
    Returns empty string if no macros exist or none are relevant.
    """
    promoted = get_promoted_macros()
    if not promoted:
        return ""

    query_lower = query.strip().lower()

    # Simple relevance: check if any words overlap
    query_words = set(query_lower.split())
    relevant = []
    for macro in promoted:
        cmd_words = set(macro["command"].split())
        overlap = query_words & cmd_words
        if len(overlap) >= 1:  # at least one word in common
            tools_str = " → ".join(macro["tools"])
            relevant.append(f'  "{macro["command"]}" → {tools_str} (used {macro["count"]}x)')

    if not relevant:
        return ""

    return "[LEARNED MACROS]\n" + "\n".join(relevant[:5])


# ═══════════════════════════════════════════════════════════════════════
# 6. PEOPLE GRAPH — auto-extracted contact/relationship memory
# ═══════════════════════════════════════════════════════════════════════
#
# A lightweight JSON-based people graph that stores contacts, relationships,
# and interaction history.  Auto-populated from:
#   - Screen extraction (VLM reads email headers, contact cards)
#   - Conversation (user mentions names, introductions)
#   - Command patterns ("email John", "message Sarah")
#
# RAM BUDGET: ~0 MB — tiny JSON file + reuses ChromaDB for search.

_PEOPLE_PATH = os.path.join(_DEMASCAS_DIR, "people.json")
_people_cache: dict | None = None
_people_dirty: bool = False


def _load_people() -> dict:
    """Load people graph from disk (cached after first read)."""
    global _people_cache
    if _people_cache is not None:
        return _people_cache

    os.makedirs(_DEMASCAS_DIR, exist_ok=True)
    if os.path.exists(_PEOPLE_PATH):
        try:
            with open(_PEOPLE_PATH, "r") as f:
                _people_cache = json.load(f)
                return _people_cache
        except Exception:
            pass

    _people_cache = {"contacts": {}, "last_updated": None}
    return _people_cache


def _save_people(data: dict) -> None:
    """Mark people graph as dirty for deferred flush."""
    global _people_cache, _people_dirty
    _people_cache = data
    _people_dirty = True


def _flush_people_now() -> None:
    """Force-write people graph to disk."""
    global _people_dirty
    if not _people_dirty or _people_cache is None:
        return
    try:
        os.makedirs(_DEMASCAS_DIR, exist_ok=True)
        tmp = _PEOPLE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_people_cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _PEOPLE_PATH)
        _people_dirty = False
    except Exception:
        pass


def add_person(
    name: str,
    email: str | None = None,
    phone: str | None = None,
    organization: str | None = None,
    relationship: str | None = None,
    source: str = "conversation",
    notes: str | None = None,
) -> None:
    """
    Add or update a person in the people graph.

    If a person with the same name already exists, merges new info
    (never overwrites existing fields with None).

    Args:
        name: Full name (primary key)
        email: Email address
        phone: Phone number
        organization: Company/org
        relationship: e.g. "friend", "colleague", "professor"
        source: Where this info came from ("conversation", "screen_extraction", "email")
        notes: Any additional notes
    """
    if not name or not name.strip():
        return

    people = _load_people()
    contacts = people.get("contacts", {})
    now = datetime.now(_IST).isoformat()

    # Normalize name as key
    key = name.strip().lower()

    existing = contacts.get(key, {
        "name": name.strip(),
        "email": None,
        "phone": None,
        "organization": None,
        "relationship": None,
        "notes": [],
        "sources": [],
        "interaction_count": 0,
        "first_seen": now,
        "last_seen": now,
    })

    # Merge — never overwrite with None
    if email:
        existing["email"] = email
    if phone:
        existing["phone"] = phone
    if organization:
        existing["organization"] = organization
    if relationship:
        existing["relationship"] = relationship
    if notes:
        note_list = existing.get("notes", [])
        if notes not in note_list:
            note_list.append(notes)
            existing["notes"] = note_list[-10:]  # keep last 10 notes
    if source and source not in existing.get("sources", []):
        existing.setdefault("sources", []).append(source)

    existing["last_seen"] = now
    existing["interaction_count"] = existing.get("interaction_count", 0) + 1

    contacts[key] = existing
    people["contacts"] = contacts
    people["last_updated"] = now

    # Cap at 100 contacts
    if len(contacts) > 100:
        # Remove oldest by last_seen
        sorted_contacts = sorted(
            contacts.items(),
            key=lambda x: x[1].get("last_seen", ""),
        )
        contacts = dict(sorted_contacts[-100:])
        people["contacts"] = contacts

    _save_people(people)
    _flush_people_now()  # flush immediately for contacts (important data)
    print(f"[👤 PEOPLE] Added/updated: {name}")


def get_person(name: str) -> dict | None:
    """Look up a person by name (case-insensitive)."""
    people = _load_people()
    contacts = people.get("contacts", {})
    key = name.strip().lower()

    # Exact match
    if key in contacts:
        return contacts[key]

    # Partial match (first name)
    for k, v in contacts.items():
        if key in k or k.startswith(key.split()[0] if key else ""):
            return v

    return None


def find_people(query: str) -> list[dict]:
    """
    Search for people matching a query.
    Matches against name, email, organization, relationship.
    """
    people = _load_people()
    contacts = people.get("contacts", {})
    query_lower = query.strip().lower()

    results = []
    for _key, person in contacts.items():
        score = 0
        if query_lower in (person.get("name", "").lower()):
            score += 3
        if person.get("email") and query_lower in person["email"].lower():
            score += 2
        if person.get("organization") and query_lower in person["organization"].lower():
            score += 1
        if person.get("relationship") and query_lower in person["relationship"].lower():
            score += 1
        if score > 0:
            results.append({**person, "_score": score})

    results.sort(key=lambda x: x["_score"], reverse=True)
    return [r for r in results[:5]]  # top 5


def get_people_context(query: str) -> str:
    """
    Build a people context block for system prompt injection.
    Returns empty string if no relevant people found.
    """
    matches = find_people(query)
    if not matches:
        # Also check if any names appear in the query
        people = _load_people()
        contacts = people.get("contacts", {})
        query_lower = query.strip().lower()
        for key, person in contacts.items():
            name_parts = key.split()
            for part in name_parts:
                if len(part) > 2 and part in query_lower:
                    matches.append(person)
                    break
            if matches:
                break

    if not matches:
        return ""

    lines = []
    for p in matches[:3]:  # max 3 people
        parts = [p.get("name", "Unknown")]
        if p.get("relationship"):
            parts.append(f"({p['relationship']})")
        if p.get("email"):
            parts.append(f"email: {p['email']}")
        if p.get("organization"):
            parts.append(f"org: {p['organization']}")
        lines.append(" | ".join(parts))

    return "[KNOWN PEOPLE]\n" + "\n".join(lines)


def extract_names_from_command(command: str) -> list[str]:
    """
    Simple heuristic to extract proper names from a command.
    Looks for capitalized words that aren't common English words.
    """
    COMMON_WORDS = {
        "the", "a", "an", "to", "in", "on", "at", "is", "are", "was",
        "were", "be", "been", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "can", "may", "might", "shall",
        "must", "need", "this", "that", "these", "those", "for", "with",
        "from", "about", "into", "through", "after", "before", "between",
        "under", "over", "up", "down", "out", "off", "then", "than",
        "when", "where", "how", "what", "which", "who", "whom", "whose",
        "why", "not", "no", "yes", "all", "each", "every", "both",
        "few", "more", "most", "other", "some", "such", "only", "own",
        "same", "so", "very", "just", "also", "and", "but", "or", "nor",
        "if", "my", "your", "his", "her", "its", "our", "their",
        "open", "close", "click", "type", "search", "send", "email",
        "message", "reply", "write", "compose", "draft", "call", "text",
        "safari", "chrome", "finder", "notes", "mail", "calendar",
        "hey", "hi", "hello", "please", "thanks", "thank", "good",
        "great", "ok", "okay", "sure", "right", "well", "now", "here",
        "there", "i", "me", "we", "you", "he", "she", "it", "they",
        "morning", "afternoon", "evening", "night", "today", "tomorrow",
        "tell", "ask", "show", "find", "get", "set", "make",
    }

    words = command.split()
    names = []
    i = 0
    while i < len(words):
        word = words[i]
        # Check if word starts with uppercase and isn't common
        if (word[0].isupper() and
                word.lower() not in COMMON_WORDS and
                len(word) > 1):
            # Try to grab consecutive capitalized words (full name)
            name_parts = [word]
            j = i + 1
            while j < len(words) and words[j][0].isupper() and words[j].lower() not in COMMON_WORDS:
                name_parts.append(words[j])
                j += 1
            names.append(" ".join(name_parts))
            i = j
        else:
            i += 1

    return names
