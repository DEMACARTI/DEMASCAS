"""
DEMASCAS — core/embeddings.py
Fast, cached text embeddings for every ChromaDB collection.

Why this exists
---------------
ChromaDB's DefaultEmbeddingFunction builds a brand-new ONNXMiniLM_L6_V2
object on EVERY call, which re-loads the ONNX model and tokenizer from disk
(~70 ms) and then runs the model padded to 256 tokens (~20 ms) — about
90 ms for a 7-word query.  /context embeds the same command 2-5 times, so
that was ~0.2-0.5 s of pure overhead before the LLM even started.

install() patches DefaultEmbeddingFunction.__call__ to use ONE persistent
model session, pad only to the longest text in the batch, and cache recent
results.  Pooling is attention-masked, so the vectors are identical to
ChromaDB's (cosine 1.0) — existing stored memories stay compatible and the
collections' persisted "default" embedding config is unchanged.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict

import numpy as np

_lock = threading.Lock()
_session = None
_tokenizer = None
_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_CACHE_SIZE = 512
_installed = False


def _load() -> None:
    """Load the ONNX session + tokenizer once (downloads the model if needed)."""
    global _session, _tokenizer
    if _session is not None:
        return
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    ef = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
    ef(["warm-up"])  # triggers the one-time model download if missing
    tok = ef.Tokenizer.from_file(
        os.path.join(ef.DOWNLOAD_PATH, ef.EXTRACTED_FOLDER_NAME, "tokenizer.json")
    )
    tok.enable_truncation(max_length=256)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")  # pad to longest, not 256
    _tokenizer = tok
    _session = ef.model


def _embed_uncached(texts: list[str]) -> list[list[float]]:
    _load()
    encoded = _tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    out = _session.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": np.zeros_like(input_ids),
    })[0]
    mask = attention_mask[:, :, None].astype(out.dtype)
    pooled = (out * mask).sum(1) / np.clip(mask.sum(1), 1e-9, None)
    norm = np.linalg.norm(pooled, axis=1, keepdims=True)
    norm[norm == 0] = 1e-12
    return (pooled / norm).astype(np.float32).tolist()


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the shared session; repeated texts hit the cache."""
    texts = [str(t) for t in texts]
    with _lock:
        missing = [t for t in dict.fromkeys(texts) if t not in _cache]
        if missing:
            for t, vec in zip(missing, _embed_uncached(missing)):
                _cache[t] = vec
                if len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)
        for t in texts:
            _cache.move_to_end(t)
        return [list(_cache[t]) for t in texts]


def install() -> None:
    """Route ChromaDB's default embedding function through embed()."""
    global _installed
    if _installed:
        return
    try:
        from chromadb.api.types import DefaultEmbeddingFunction
    except Exception:
        return

    def _fast_call(self, input):  # noqa: ANN001 — matches chroma's signature
        return [np.array(v, dtype=np.float32) for v in embed(list(input))]

    DefaultEmbeddingFunction.__call__ = _fast_call
    _installed = True


def warm_up() -> None:
    """Load the model now so the first spoken command doesn't pay for it."""
    try:
        install()
        embed(["warm up the embedding model"])
    except Exception as e:  # never block startup
        print(f"[!] Embedding warm-up failed (non-fatal): {e}")
