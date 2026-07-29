"""Global search over the FTS5 index with a safe fuzzy fallback.

Primary path uses FTS5 MATCH (fast, indexed). If the query has no FTS hits
(or contains characters FTS would reject), it falls back to a LIKE scan so
short/partial queries still return results. Input is treated as data, never
interpolated as SQL.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from investigation_app.extensions import get_connection

_SAFE_TERM = re.compile(r"[A-Za-z0-9@._\-]+")


def _case_id(conn, case_uid: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM cases WHERE uid = ?", (case_uid,)).fetchone()
    return row["id"] if row else None


def _fts_query(raw: str) -> str:
    """Build a prefix FTS query from safe terms (e.g. 'foo bar' -> foo* bar*)."""
    terms = _SAFE_TERM.findall(raw or "")
    return " ".join(f'"{t}"*' for t in terms)


def search(case_uid: str, query: str, limit: int = 50) -> List[Dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return []

        results: List[Dict[str, Any]] = []
        fts = _fts_query(query)
        if fts:
            try:
                rows = conn.execute(
                    "SELECT ref_type, ref_id, "
                    "snippet(search_index, -1, '[', ']', ' ... ', 12) AS snip "
                    "FROM search_index "
                    "WHERE case_id = ? AND search_index MATCH ? LIMIT ?",
                    (case_id, fts, limit),
                ).fetchall()
                results = [
                    {"ref_type": r["ref_type"], "ref_id": r["ref_id"], "snippet": r["snip"]}
                    for r in rows
                ]
            except Exception:
                results = []

        if not results:
            like = f"%{query}%"
            rows = conn.execute(
                "SELECT ref_type, ref_id, substr(content, 1, 160) AS snip "
                "FROM search_index "
                "WHERE case_id = ? AND content LIKE ? LIMIT ?",
                (case_id, like, limit),
            ).fetchall()
            results = [
                {"ref_type": r["ref_type"], "ref_id": r["ref_id"], "snippet": r["snip"]}
                for r in rows
            ]
        return results
    finally:
        conn.close()
