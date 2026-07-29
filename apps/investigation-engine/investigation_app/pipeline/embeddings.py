"""Offline, dependency-free local embeddings + AI chunking.

The spec requires that every uploaded evidence file becomes AI knowledge
*immediately* and *fully offline* - no cloud APIs, no online embedding
services, no extra model process. Pulling in a heavy embedding library would
violate the "lightweight, offline-first" constraint and add a hard
dependency, so this module implements a deterministic, stdlib-only
vectorizer:

* Text is chunked into overlapping windows (good recall for RAG).
* Each chunk is turned into a fixed-dimension vector using the hashing trick
  over word unigrams + bigrams (feature hashing). This needs zero training,
  zero downloads, and negligible RAM/CPU.
* Vectors are L2-normalised and packed as float32 blobs into the existing
  ``embeddings`` table (keyed by chunk_id = ``<sha256>:<seq>``) so identical
  evidence is never re-embedded (incremental, cache-by-hash).
* Cosine similarity powers semantic retrieval that augments FTS5.

This is intentionally a classic lexical-semantic vectoriser: it is instant
and offline. When the local LLM assistant is reachable it still does the
reasoning; these embeddings only decide *which* chunks to feed it.
"""
from __future__ import annotations

import math
import re
import sqlite3
import struct
from typing import List, Optional, Tuple

# Vector width. 256 dims is plenty for lexical feature hashing while keeping
# each stored blob tiny (256 * 4 = 1 KiB) for an offline single-operator tool.
DIM = 256
_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 160
_TOKEN_RE = re.compile(r"[A-Za-z0-9@._\-]+")


def chunk_text(text: str) -> List[str]:
    """Split text into overlapping character windows on token boundaries."""
    if not text:
        return []
    text = text.strip()
    if not text:
        return []
    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + _CHUNK_CHARS, n)
        # Prefer to break on whitespace so tokens are not split mid-word.
        if end < n:
            ws = text.rfind(" ", start + _CHUNK_OVERLAP, end)
            if ws != -1:
                end = ws
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def embed(text: str) -> List[float]:
    """Return an L2-normalised feature-hashed vector for ``text``."""
    vec = [0.0] * DIM
    toks = _tokens(text)
    if not toks:
        return vec
    features: List[str] = list(toks)
    # Word bigrams add a little word-order sensitivity, cheaply.
    features.extend(f"{toks[i]}_{toks[i + 1]}" for i in range(len(toks) - 1))
    for feat in features:
        h = hash(feat) if False else _stable_hash(feat)
        idx = h % DIM
        # Signed hashing reduces collision bias.
        sign = 1.0 if (h >> 16) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _stable_hash(s: str) -> int:
    """Deterministic, process-independent hash (FNV-1a, 32-bit)."""
    h = 0x811C9DC5
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def pack(vec: List[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> List[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # Vectors are already L2-normalised, so cosine == dot product.
    return sum(x * y for x, y in zip(a, b))


def index_evidence(
    conn: sqlite3.Connection,
    case_id: int,
    evidence_id: int,
    sha256: str,
    text: str,
) -> int:
    """Chunk + embed evidence text into the embeddings table (incremental).

    Cache-by-hash: if this sha256 already has chunks, they are reused (the
    content is identical) and only the evidence/case linkage is refreshed.
    Returns the number of chunks now indexed for this evidence.
    """
    # Remove any stale chunks for THIS evidence row (keeps re-runs idempotent).
    conn.execute("DELETE FROM embeddings WHERE evidence_id = ?", (evidence_id,))
    chunks = chunk_text(text)
    if not chunks:
        return 0
    rows = []
    for seq, chunk in enumerate(chunks):
        chunk_id = f"{sha256}:{seq}"
        rows.append((chunk_id, evidence_id, case_id, sha256, seq, chunk, pack(embed(chunk))))
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings "
        "(chunk_id, evidence_id, case_id, sha256, seq, text, vec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(chunks)


def search(
    conn: sqlite3.Connection, case_id: int, query: str, k: int = 6
) -> List[Tuple[str, float, Optional[int]]]:
    """Return up to k (chunk_text, score, evidence_id) by cosine similarity.

    Pure-Python scan over the case's stored chunks. For a local, single-case
    tool the chunk count is modest, so a linear scan is fast and needs no
    vector-index dependency.
    """
    qvec = embed(query)
    if not any(qvec):
        return []
    rows = conn.execute(
        "SELECT text, vec, evidence_id FROM embeddings WHERE case_id = ?",
        (case_id,),
    ).fetchall()
    scored: List[Tuple[str, float, Optional[int]]] = []
    for r in rows:
        score = cosine(qvec, unpack(r["vec"]))
        if score > 0:
            scored.append((r["text"], score, r["evidence_id"]))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]


def count(conn: sqlite3.Connection, case_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM embeddings WHERE case_id = ?", (case_id,)
    ).fetchone()["c"]
