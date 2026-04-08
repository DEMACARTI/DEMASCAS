"""
DEMASCAS — core/memory.py
Persistent Long-Term Memory using ChromaDB.

Provides RAG (Retrieval-Augmented Generation) so DEMASCAS can remember
facts, preferences, and past interactions across sessions.

Storage:  ~/.demascas/memory/  (ChromaDB persistent directory)
Model:    ChromaDB's built-in all-MiniLM-L6-v2 embedding (runs on CPU, ~80 MB)

OPTIMIZATIONS:
  - Lazy initialization: ChromaDB client created only on first use.
  - Timestamps stored as metadata for chronological context.
  - Duplicate detection via content hashing.
  - Top-k capped at 5 to keep LLM context lean.
"""

import hashlib
import os
from datetime import datetime, timezone, timedelta

# Indian Standard Time offset (matches agent.py)
_IST = timezone(timedelta(hours=5, minutes=30))

# Persistent storage location
_MEMORY_DIR = os.path.expanduser("~/.demascas/memory")

# Lazy-loaded ChromaDB client & collection
_client = None
_collection = None


def _get_collection():
    """Lazy-init ChromaDB client and collection on first use."""
    global _client, _collection
    if _collection is not None:
        return _collection

    try:
        import chromadb

        os.makedirs(_MEMORY_DIR, exist_ok=True)
        _client = chromadb.PersistentClient(path=_MEMORY_DIR)
        _collection = _client.get_or_create_collection(
            name="demascas_memory",
            metadata={"hnsw:space": "cosine"},  # cosine similarity
        )
        count = _collection.count()
        print(f"[🧠 MEMORY] Loaded {count} memories from {_MEMORY_DIR}")
        return _collection

    except ImportError:
        print("[!] chromadb not installed. Run: pip install chromadb")
        return None
    except Exception as e:
        print(f"[!] Memory initialization failed: {e}")
        return None


def save_memory(text: str) -> str:
    """
    Saves a piece of information to DEMASCAS's long-term memory.
    Automatically timestamps and deduplicates entries.

    Args:
        text: The information to remember (e.g. "User prefers dark mode",
              "User's name is Daksh", "Meeting with John at 3 PM tomorrow").

    Returns:
        A confirmation message or error string.
    """
    print(f"[🧠 MEMORY] Saving: '{text[:80]}...'")
    collection = _get_collection()
    if collection is None:
        return "[-] Memory system unavailable (chromadb not installed)."

    text = text.strip()
    if not text:
        return "[-] Cannot save empty memory."

    # Content-based dedup: hash the text
    content_hash = hashlib.md5(text.lower().encode()).hexdigest()

    # Check if this exact content already exists
    existing = collection.get(ids=[content_hash])
    if existing and existing["ids"]:
        return f"Already remembered: '{text[:60]}...'"

    # Save with timestamp metadata
    now_ist = datetime.now(_IST)
    collection.add(
        ids=[content_hash],
        documents=[text],
        metadatas=[{
            "timestamp": now_ist.isoformat(),
            "date": now_ist.strftime("%Y-%m-%d"),
            "source": "user",
        }],
    )
    count = collection.count()
    return f"Remembered: '{text[:60]}...' (total memories: {count})"


def retrieve_memories(query: str, top_k: int = 5) -> str:
    """
    Searches DEMASCAS's long-term memory for information relevant to the query.
    Uses semantic similarity (cosine distance on embeddings).

    Args:
        query:  The search query (e.g. "What is the user's name?",
                "preferences", "meetings").
        top_k:  Maximum number of results to return (default 5).

    Returns:
        A formatted string of relevant memories with timestamps, or a message
        if no memories are found.
    """
    print(f"[🧠 MEMORY] Retrieving memories for: '{query}'...")
    collection = _get_collection()
    if collection is None:
        return "[-] Memory system unavailable (chromadb not installed)."

    if collection.count() == 0:
        return "No memories stored yet."

    top_k = min(top_k, 5)  # Cap at 5 to keep context lean

    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(top_k, collection.count()),
        )

        if not results["documents"] or not results["documents"][0]:
            return f"No relevant memories found for '{query}'."

        lines = [f"Relevant memories for '{query}':\n"]
        for i, (doc, meta) in enumerate(
            zip(results["documents"][0], results["metadatas"][0]), 1
        ):
            date = meta.get("date", "unknown date")
            lines.append(f"{i}. [{date}] {doc}")

        return "\n".join(lines)

    except Exception as e:
        return f"[-] Memory retrieval failed: {e}"


def get_memory_count() -> int:
    """Returns the number of stored memories, or 0 if unavailable."""
    collection = _get_collection()
    if collection is None:
        return 0
    return collection.count()


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Testing DEMASCAS memory system...")
    print(save_memory("User's name is Daksh"))
    print(save_memory("User prefers dark mode"))
    print(save_memory("User's favourite programming language is Python"))
    print()
    print(retrieve_memories("What is the user's name?"))
    print()
    print(retrieve_memories("preferences"))
    print(f"\nTotal memories: {get_memory_count()}")
