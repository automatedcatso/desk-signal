"""Streamed SHA-256 hashing.

Hashes files in fixed-size chunks so memory use stays flat regardless of file
size. The digest is the dedup key: evidence with a digest already present for
a case is never stored or processed twice.
"""
from __future__ import annotations

import hashlib

_CHUNK = 1024 * 1024  # 1 MiB


def sha256_file(path: str) -> str:
    """Return the hex SHA-256 of the file at ``path`` (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 of an in-memory buffer."""
    return hashlib.sha256(data).hexdigest()
