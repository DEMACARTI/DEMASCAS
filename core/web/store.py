"""
DEMASCAS — core/web/store.py
Everything DEMASCAS has read on the web, ready to use in any task.

Pages are split into ~700-char chunks, embedded with the cached embedder
and kept in an in-memory matrix (recall ≈ 1 ms — no database on the hot
path).  Each chunk remembers its source, title and when it was fetched,
so answers can say where a fact came from and how fresh it is.

Bounded for 8 GB laptops: at most MAX_CHUNKS (8,000 × 384 floats ≈ 12 MB
of vectors); the least-used, oldest chunks are forgotten first.

Files: ~/.demascas/web/chunks.jsonl + vectors.npy
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import urlparse

import numpy as np

WEB_DIR = os.path.join(os.path.expanduser("~"), ".demascas", "web")
MAX_CHUNKS = 8000
CHUNK_CHARS = 700
MIN_SCORE = 0.45                 # below this a chunk isn't about the query
_REALTIME = re.compile(r"\b(today|tonight|now|current|currently|latest|news|this week|price|score|"
                       r"weather|live|right now|yesterday)\b", re.I)

_lock = threading.RLock()
_chunks: list[dict] | None = None
_matrix: np.ndarray | None = None
_dirty_meta = False


# ── persistence ──────────────────────────────────────────────────────

def _load() -> None:
    global _chunks, _matrix
    if _chunks is not None:
        return
    _chunks, _matrix = [], np.zeros((0, 384), dtype=np.float32)
    try:
        with open(os.path.join(WEB_DIR, "chunks.jsonl")) as f:
            _chunks = [json.loads(ln) for ln in f if ln.strip()]
        _matrix = np.load(os.path.join(WEB_DIR, "vectors.npy"))
        if _matrix.shape[0] != len(_chunks):
            _chunks, _matrix = [], np.zeros((0, 384), dtype=np.float32)
    except (FileNotFoundError, ValueError, OSError):
        pass


def _save() -> None:
    os.makedirs(WEB_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=WEB_DIR, suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for c in _chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, os.path.join(WEB_DIR, "chunks.jsonl"))
    fd, tmpv = tempfile.mkstemp(dir=WEB_DIR, suffix=".npy")
    os.close(fd)
    np.save(tmpv, _matrix)
    os.replace(tmpv, os.path.join(WEB_DIR, "vectors.npy"))


def flush() -> None:
    """Persist usage counters (called from the background worker)."""
    global _dirty_meta
    with _lock:
        if _dirty_meta and _chunks is not None:
            _save()
            _dirty_meta = False


# ── chunking ─────────────────────────────────────────────────────────

_BOILER = re.compile(r"cookie|subscribe|sign up|newsletter|all rights reserved|privacy policy|"
                     r"accept|advertis|share on|follow us|log ?in|skip to", re.I)


def _chunk(text: str) -> list[str]:
    paras = [p.strip() for p in text.split("\n")]
    paras = [p for p in paras if len(p) > 40 and not (_BOILER.search(p) and len(p) < 200)]
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > CHUNK_CHARS and cur:
            out.append(cur.strip())
            cur = cur.strip().split("\n")[-1] + "\n" if len(cur) < CHUNK_CHARS * 1.5 else ""
        cur += p + "\n"
    if len(cur.strip()) > 80:
        out.append(cur.strip())
    return [c[:CHUNK_CHARS * 2] for c in out]


def _table_text(tables: list) -> list[str]:
    out = []
    for header, rows in tables or []:
        lines = []
        if header:
            lines.append(" | ".join(header))
        lines += [" | ".join(r) for r in rows[:40]]
        if len(lines) >= 2:
            out.append("\n".join(lines)[:CHUNK_CHARS * 2])
    return out


# ── writing ──────────────────────────────────────────────────────────

def add_page(url: str, title: str, text: str, tables: list | None = None,
             topic: str = "", published: str = "") -> int:
    """Store a page (replacing an older copy of the same URL).  Returns
    the number of chunks kept."""
    global _matrix
    from core.embeddings import embed
    pieces = _chunk(text) + _table_text(tables or [])
    if not pieces:
        return 0
    head = f"{title}\n" if title else ""
    vecs = np.array(embed([head + p for p in pieces]), dtype=np.float32)
    now = time.time()
    with _lock:
        _load()
        keep = [i for i, c in enumerate(_chunks) if c["url"] != url]
        if len(keep) != len(_chunks):
            _chunks[:] = [_chunks[i] for i in keep]
            _matrix = _matrix[keep]
        seen = {c["hash"] for c in _chunks}
        new, new_vecs = [], []
        for p, v in zip(pieces, vecs):
            h = hashlib.sha1(p.encode()).hexdigest()[:16]
            if h in seen:
                continue
            seen.add(h)
            new.append({"url": url, "site": urlparse(url).netloc, "title": title[:150], "text": p,
                        "topic": topic[:100], "fetched": now, "published": published,
                        "hash": h, "hits": 0, "used": 0.0})
            new_vecs.append(v)
        if not new:
            return 0
        _chunks.extend(new)
        _matrix = np.vstack([_matrix, np.array(new_vecs, dtype=np.float32)])
        _evict()
        _save()
        return len(new)


def _evict() -> None:
    global _matrix
    if len(_chunks) <= MAX_CHUNKS:
        return
    # forget the least useful: never recalled + oldest first
    order = sorted(range(len(_chunks)), key=lambda i: (_chunks[i]["hits"],
                                                         max(_chunks[i]["used"], _chunks[i]["fetched"])))
    drop = set(order[:len(_chunks) - MAX_CHUNKS])
    keep = [i for i in range(len(_chunks)) if i not in drop]
    _chunks[:] = [_chunks[i] for i in keep]
    _matrix = _matrix[keep]


def forget(match: str) -> int:
    """Remove chunks whose URL, site or topic contains `match`."""
    global _matrix
    m = match.lower().strip()
    with _lock:
        _load()
        keep = [i for i, c in enumerate(_chunks)
                if not m or m not in (c["url"] + " " + c["topic"] + " " + c["site"]).lower()]
        removed = len(_chunks) - len(keep)
        if removed:
            _chunks[:] = [_chunks[i] for i in keep]
            _matrix = _matrix[keep]
            _save()
        return removed


# ── reading ──────────────────────────────────────────────────────────

def recall(query: str, k: int = 4, min_score: float = MIN_SCORE,
           max_age_days: float | None = None) -> list[dict]:
    """Most relevant stored chunks for a query (score ≥ min_score).
    Questions about 'today / latest / news / prices' only use fresh pages."""
    global _dirty_meta
    from core.embeddings import embed
    with _lock:
        _load()
        if not _chunks:
            return []
        if max_age_days is None and _REALTIME.search(query or ""):
            max_age_days = 1.0
        q = np.array(embed([query])[0], dtype=np.float32)
        scores = _matrix @ q
        now = time.time()
        out, sites = [], {}
        for i in np.argsort(-scores):
            s = float(scores[i])
            if s < min_score or len(out) >= k:
                break
            c = _chunks[int(i)]
            if max_age_days is not None and now - c["fetched"] > max_age_days * 86400:
                continue
            if sites.get(c["site"], 0) >= 2:      # variety: ≤ 2 chunks per site
                continue
            sites[c["site"]] = sites.get(c["site"], 0) + 1
            c["hits"] += 1
            c["used"] = now
            _dirty_meta = True
            out.append({**{k2: c[k2] for k2 in ("url", "site", "title", "text", "fetched", "published")},
                        "score": round(s, 3)})
        return out


def _age(ts: float) -> str:
    d = (time.time() - ts) / 86400
    if d < 1:
        return "today"
    if d < 2:
        return "yesterday"
    if d < 30:
        return f"{int(d)} days ago"
    return f"{int(d / 30)} months ago"


def context_block(query: str, max_chars: int = 900, k: int = 3) -> str:
    """Prompt-ready notes with source and age, or '' if nothing relevant."""
    hits = recall(query, k=k)
    if not hits:
        return ""
    lines, used = [], 0
    for h in hits:
        body = re.sub(r"\s+", " ", h["text"])
        room = max_chars - used - 60
        if room < 120:
            break
        body = body[:room]
        lines.append(f"- ({h['site']}, read {_age(h['fetched'])}) {body}")
        used += len(lines[-1])
    return "FROM THE WEB (notes DEMASCAS gathered; prefer these over guessing):\n" + "\n".join(lines)


def coverage(query: str, fresh_days: float = 7.0) -> int:
    """How many good, fresh chunks we already have about a query."""
    return len(recall(query, k=6, min_score=0.55, max_age_days=fresh_days))


def stats() -> dict:
    with _lock:
        _load()
        sites = {c["site"] for c in _chunks}
        return {"chunks": len(_chunks), "pages": len({c["url"] for c in _chunks}),
                "sites": len(sites), "max_chunks": MAX_CHUNKS}
