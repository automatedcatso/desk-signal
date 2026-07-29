"""Incremental FTS5 indexing for global search.

Inserts textual content into the ``search_index`` virtual table. Re-indexing
an evidence item first deletes its previous rows so the index stays in sync
without duplicates (incremental, not full-rebuild).
"""
from __future__ import annotations

import sqlite3


def index_evidence_text(
    conn: sqlite3.Connection, case_id: int, evidence_id: int, content: str
) -> None:
    """(Re)index one evidence item's extracted text."""
    conn.execute(
        "DELETE FROM search_index WHERE ref_type = 'evidence' AND ref_id = ?",
        (evidence_id,),
    )
    if content and content.strip():
        conn.execute(
            "INSERT INTO search_index (case_id, ref_type, ref_id, content) "
            "VALUES (?, 'evidence', ?, ?)",
            (case_id, evidence_id, content),
        )


def index_entity(
    conn: sqlite3.Connection, case_id: int, entity_id: int, value: str
) -> None:
    """Index an entity value so it is discoverable via global search."""
    conn.execute(
        "DELETE FROM search_index WHERE ref_type = 'entity' AND ref_id = ?",
        (entity_id,),
    )
    if value and value.strip():
        conn.execute(
            "INSERT INTO search_index (case_id, ref_type, ref_id, content) "
            "VALUES (?, 'entity', ?, ?)",
            (case_id, entity_id, value),
        )
