"""
DEMASCAS — core/knowledge.py
Knowledge System — structured long-term knowledge across sessions.

Three ChromaDB collections, all sharing the SAME PersistentClient as
core/memory.py (zero extra RAM for embedding model):

1. PROCEDURES   (collection: demascas_procedures)
   Step-by-step how-tos learned from successful multi-step tasks.
   e.g. "How to send an email to Aman" → [open Mail, compose, type_text...]

2. FACTS         (collection: demascas_facts)
   Atomic facts about the world and the user's contacts.
   e.g. "Aman Gupta's email is aman@example.com"

3. CORRECTIONS   (collection: demascas_corrections)
   Past mistakes and their fixes — injected into context so the LLM
   doesn't repeat the same error twice.
   e.g. "DON'T open_url for local files — use find_file + open_file"

Auto-learning hooks:
  - auto_save_from_tool_result()  — called after every tool execution
  - detect_and_save_correction()  — called after /learn with success=false
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))
_DEMASCAS_DIR = os.path.expanduser("~/.demascas")

# Lazy-loaded ChromaDB collections
_procedures_collection = None
_facts_collection = None
_corrections_collection = None


# ═══════════════════════════════════════════════════════════════════════
# ChromaDB client reuse — same pattern as core/learning.py
# ═══════════════════════════════════════════════════════════════════════

def _get_chromadb_client():
    """Get the shared ChromaDB PersistentClient from core/memory.py."""
    try:
        from core.memory import _get_collection as _get_mem_collection
        _get_mem_collection()  # force init
        from core import memory as _mem_mod
        client = _mem_mod._client
        if client is not None:
            return client
    except Exception:
        pass

    # Fallback — create our own client
    import chromadb
    memory_dir = os.path.join(_DEMASCAS_DIR, "memory")
    os.makedirs(memory_dir, exist_ok=True)
    return chromadb.PersistentClient(path=memory_dir)


def _get_procedures():
    """Lazy-init the procedures collection."""
    global _procedures_collection
    if _procedures_collection is not None:
        return _procedures_collection
    client = _get_chromadb_client()
    _procedures_collection = client.get_or_create_collection(
        name="demascas_procedures",
        metadata={"hnsw:space": "cosine"},
    )
    count = _procedures_collection.count()
    if count > 0:
        print(f"[📘 KNOWLEDGE] Loaded {count} procedure(s)")
    return _procedures_collection


def _get_facts():
    """Lazy-init the facts collection."""
    global _facts_collection
    if _facts_collection is not None:
        return _facts_collection
    client = _get_chromadb_client()
    _facts_collection = client.get_or_create_collection(
        name="demascas_facts",
        metadata={"hnsw:space": "cosine"},
    )
    count = _facts_collection.count()
    if count > 0:
        print(f"[📘 KNOWLEDGE] Loaded {count} fact(s)")
    return _facts_collection


def _get_corrections():
    """Lazy-init the corrections collection."""
    global _corrections_collection
    if _corrections_collection is not None:
        return _corrections_collection
    client = _get_chromadb_client()
    _corrections_collection = client.get_or_create_collection(
        name="demascas_corrections",
        metadata={"hnsw:space": "cosine"},
    )
    count = _corrections_collection.count()
    if count > 0:
        print(f"[📘 KNOWLEDGE] Loaded {count} correction(s)")
    return _corrections_collection


# ═══════════════════════════════════════════════════════════════════════
# 1. PROCEDURES — multi-step how-tos
# ═══════════════════════════════════════════════════════════════════════

def save_procedure(title: str, steps: list[str], trigger_phrase: str = "") -> dict:
    """
    Save a step-by-step procedure.

    Args:
        title: Short descriptive name, e.g. "Send email to Aman"
        steps: List of step strings, e.g. ["open Mail", "click Compose", ...]
        trigger_phrase: Optional natural-language trigger for RAG matching

    Returns:
        {"success": True/False, "message": "..."}
    """
    try:
        coll = _get_procedures()
        doc_text = f"PROCEDURE: {title}\n" + "\n".join(
            f"  {i+1}. {s}" for i, s in enumerate(steps)
        )
        doc_id = "proc_" + hashlib.md5(title.lower().encode()).hexdigest()[:12]

        # Use trigger phrase for embedding if available, else title
        embed_text = trigger_phrase if trigger_phrase else title

        coll.upsert(
            ids=[doc_id],
            documents=[doc_text],
            metadatas=[{
                "title": title,
                "steps_json": json.dumps(steps),
                "trigger": trigger_phrase,
                "created": datetime.now(_IST).isoformat(),
                "type": "procedure",
            }],
        )
        return {"success": True, "message": f"Saved procedure: {title}"}
    except Exception as e:
        return {"success": False, "message": f"Failed to save procedure: {e}"}


def get_relevant_procedures(query: str, top_k: int = 3) -> list[dict]:
    """
    Find procedures relevant to a user query via semantic search.

    Returns list of {"title": str, "steps": list[str], "text": str}
    """
    try:
        coll = _get_procedures()
        if coll.count() == 0:
            return []

        results = coll.query(query_texts=[query], n_results=min(top_k, coll.count()))
        procedures = []
        for doc, meta in zip(
            results.get("documents", [[]])[0],
            results.get("metadatas", [[]])[0],
        ):
            steps = []
            try:
                steps = json.loads(meta.get("steps_json", "[]"))
            except Exception:
                pass
            procedures.append({
                "title": meta.get("title", "Unknown"),
                "steps": steps,
                "text": doc,
            })
        return procedures
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════
# 2. FACTS — atomic knowledge items
# ═══════════════════════════════════════════════════════════════════════

def save_fact(fact: str, category: str = "general") -> dict:
    """
    Save an atomic fact.

    Args:
        fact: The fact text, e.g. "Aman Gupta's email is aman@example.com"
        category: "general", "contact", "preference", "location", etc.

    Returns:
        {"success": True/False, "message": "..."}
    """
    try:
        coll = _get_facts()
        doc_id = "fact_" + hashlib.md5(fact.lower().strip().encode()).hexdigest()[:12]

        coll.upsert(
            ids=[doc_id],
            documents=[fact],
            metadatas=[{
                "category": category,
                "created": datetime.now(_IST).isoformat(),
                "type": "fact",
            }],
        )
        return {"success": True, "message": f"Saved fact: {fact[:60]}..."}
    except Exception as e:
        return {"success": False, "message": f"Failed to save fact: {e}"}


def get_fact(query: str, top_k: int = 5) -> list[str]:
    """
    Retrieve facts relevant to a query via semantic search.

    Returns: list of fact strings.
    """
    try:
        coll = _get_facts()
        if coll.count() == 0:
            return []

        results = coll.query(query_texts=[query], n_results=min(top_k, coll.count()))
        return results.get("documents", [[]])[0]
    except Exception:
        return []


def save_contact(name: str, **details) -> dict:
    """
    Save a contact as a structured fact.

    Args:
        name: Person's name
        **details: email=, phone=, relationship=, notes=, etc.

    Returns:
        {"success": True/False, "message": "..."}
    """
    parts = [f"{name}:"]
    for k, v in details.items():
        if v:
            parts.append(f"  {k}: {v}")
    fact_text = " ".join(parts)
    return save_fact(fact_text, category="contact")


# ═══════════════════════════════════════════════════════════════════════
# 3. CORRECTIONS — past mistakes to avoid
# ═══════════════════════════════════════════════════════════════════════

def save_correction(wrong_action: str, correct_action: str, context: str = "") -> dict:
    """
    Save a correction so the LLM avoids repeating the same mistake.

    Args:
        wrong_action: What went wrong, e.g. "Used open_url for local file"
        correct_action: What should be done instead, e.g. "Use find_file + open_file"
        context: Optional context about when this applies

    Returns:
        {"success": True/False, "message": "..."}
    """
    try:
        coll = _get_corrections()
        doc_text = (
            f"CORRECTION: When asked to {context or 'do this task'}:\n"
            f"  WRONG: {wrong_action}\n"
            f"  RIGHT: {correct_action}"
        )
        doc_id = "corr_" + hashlib.md5(
            (wrong_action + correct_action).lower().encode()
        ).hexdigest()[:12]

        coll.upsert(
            ids=[doc_id],
            documents=[doc_text],
            metadatas=[{
                "wrong": wrong_action,
                "correct": correct_action,
                "context": context,
                "created": datetime.now(_IST).isoformat(),
                "type": "correction",
            }],
        )
        return {"success": True, "message": f"Saved correction"}
    except Exception as e:
        return {"success": False, "message": f"Failed to save correction: {e}"}


def get_relevant_corrections(query: str, top_k: int = 3) -> list[str]:
    """
    Retrieve corrections relevant to a query.

    Returns: list of correction text strings.
    """
    try:
        coll = _get_corrections()
        if coll.count() == 0:
            return []

        results = coll.query(query_texts=[query], n_results=min(top_k, coll.count()))
        return results.get("documents", [[]])[0]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════
# 4. AUTO-LEARNING HOOKS — called automatically by tool_server.py
# ═══════════════════════════════════════════════════════════════════════

def auto_save_from_tool_result(
    tool_name: str, args: dict, result: str, success: bool
) -> None:
    """
    Called after every tool execution.  Automatically extracts and saves
    knowledge from tool results without any extra LLM calls.

    Currently auto-saves:
    - Contact info from extract_contact results
    - File locations from find_file results (as facts)
    - Search results summaries from smart_search/search_web
    """
    if not success:
        return

    try:
        # Auto-save contact info
        if tool_name == "extract_contact":
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
                if isinstance(parsed, dict) and parsed.get("name"):
                    details = {k: v for k, v in parsed.items() if k != "name" and v}
                    save_contact(parsed["name"], **details)
            except Exception:
                pass

        # Auto-save file locations as facts
        elif tool_name == "find_file":
            try:
                parsed = json.loads(result) if isinstance(result, str) else result
                if isinstance(parsed, dict):
                    matches = parsed.get("matches", [])
                    partial = args.get("partial_name", "")
                    if len(matches) == 1 and partial:
                        m = matches[0]
                        save_fact(
                            f"File '{partial}' is located at {m.get('path', m.get('name', '?'))}",
                            category="file_location",
                        )
            except Exception:
                pass

        # Auto-save search result summaries
        elif tool_name in ("smart_search", "search_web"):
            query = args.get("query", "")
            if query and result and not result.startswith("[-]"):
                # Save a condensed version as a fact (first 300 chars)
                snippet = result[:300].strip()
                if len(snippet) > 50:
                    save_fact(
                        f"Search result for '{query}': {snippet}",
                        category="search_cache",
                    )
    except Exception:
        pass  # never let auto-learning crash a tool call


def detect_and_save_correction(
    command: str, tools_used: list[str], result: str, success: bool
) -> None:
    """
    Called by /learn endpoint when success=False.
    Heuristically detects common error patterns and saves corrections.
    """
    if success:
        return

    try:
        result_lower = result.lower() if result else ""

        # Pattern: used open_url for a local file
        if "open_url" in tools_used and (
            "not found" in result_lower
            or "file://" in result_lower
            or "no such" in result_lower
        ):
            save_correction(
                wrong_action="Used open_url to open a local file",
                correct_action="Use find_file to locate the file, then open_file to open it",
                context=f"User said: {command[:100]}",
            )

        # Pattern: wrong app name
        if "open_application" in tools_used and (
            "not found" in result_lower
            or "does not exist" in result_lower
        ):
            save_correction(
                wrong_action=f"Tried to open application with wrong name",
                correct_action="Use list_running_applications first, or check exact app bundle name",
                context=f"User said: {command[:100]}",
            )

        # Pattern: element not found in UI
        if "click_ui_element" in tools_used and "not found" in result_lower:
            save_correction(
                wrong_action="Tried to click UI element that doesn't exist",
                correct_action="Use get_active_window_tree first to find exact element names",
                context=f"User said: {command[:100]}",
            )
    except Exception:
        pass  # never crash for auto-learning


# ═══════════════════════════════════════════════════════════════════════
# 5. CONTEXT INJECTION — return knowledge block for /context endpoint
# ═══════════════════════════════════════════════════════════════════════

def get_knowledge_context(query: str) -> str:
    """
    Build a compact knowledge block to inject into the LLM context.
    Called by the /context endpoint.

    Returns a string with relevant procedures, facts, and corrections.
    """
    parts = []

    # Relevant procedures
    try:
        procs = get_relevant_procedures(query, top_k=2)
        for p in procs:
            parts.append(p["text"])
    except Exception:
        pass

    # Relevant facts
    try:
        facts = get_fact(query, top_k=3)
        if facts:
            parts.append("KNOWN FACTS:\n" + "\n".join(f"- {f}" for f in facts))
    except Exception:
        pass

    # Relevant corrections
    try:
        corrections = get_relevant_corrections(query, top_k=2)
        if corrections:
            parts.append("PAST CORRECTIONS (avoid these mistakes):\n" + "\n".join(corrections))
    except Exception:
        pass

    return "\n\n".join(parts)
