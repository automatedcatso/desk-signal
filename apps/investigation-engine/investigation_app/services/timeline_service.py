"""Timeline auto-merge and querying.

Merges chronological events from every processed artifact into one filterable
timeline. Stage 3 seeds events from ``date`` entities discovered by the rule
engine and from evidence intake; later stages can add richer parsers without
changing this interface.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from investigation_app.extensions import db_write_lock, get_connection


def _case_id(conn, case_uid: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM cases WHERE uid = ?", (case_uid,)).fetchone()
    return row["id"] if row else None


def rebuild(case_uid: str) -> int:
    """Rebuild the timeline for a case from current evidence/entities.

    Idempotent: clears auto-generated rows first, then re-derives them, so
    running it repeatedly never duplicates events.
    """
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return 0
        with db_write_lock():
            conn.execute(
                "DELETE FROM timeline WHERE case_id = ? AND kind = 'auto'", (case_id,)
            )
            conn.commit()
        # Evidence intake events.
        rows = conn.execute(
            "SELECT id, original_name, created_at FROM evidence WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO timeline (case_id, ts, source_evidence_id, kind, summary) "
                "VALUES (?, ?, ?, 'auto', ?)",
                (case_id, r["created_at"], r["id"],
                 f"Evidence added: {r['original_name']}"),
            )
        # Date entities linked to evidence.
        drows = conn.execute(
            "SELECT e.value AS ts, l.evidence_id "
            "FROM entities e JOIN entity_links l ON l.entity_id = e.id "
            "WHERE e.case_id = ? AND e.type = 'date'",
            (case_id,),
        ).fetchall()
        for r in drows:
            conn.execute(
                "INSERT INTO timeline (case_id, ts, source_evidence_id, kind, summary) "
                "VALUES (?, ?, ?, 'auto', ?)",
                (case_id, r["ts"], r["evidence_id"],
                 f"Date referenced in evidence #{r['evidence_id']}"),
            )

        # Structured transaction events. These provide real financial
        # chronology instead of relying only on generic date mentions.
        trows = conn.execute(
            "SELECT id, evidence_id, txn_date, utr, amount, account_no, receiver_account, bank, source_ref "
            "FROM transactions WHERE case_id = ? AND txn_date IS NOT NULL AND txn_date != ''",
            (case_id,),
        ).fetchall()
        for r in trows:
            acct = r["receiver_account"] or r["account_no"] or "account"
            summary = (
                f"Transaction row #{r['id']}: UTR {r['utr'] or 'N/A'}, "
                f"amount {r['amount'] if r['amount'] is not None else 'N/A'}, "
                f"account {acct}, bank {r['bank'] or 'N/A'}"
            )
            if r["source_ref"]:
                summary += f" ({r['source_ref']})"
            conn.execute(
                "INSERT INTO timeline (case_id, ts, source_evidence_id, kind, summary) "
                "VALUES (?, ?, ?, 'auto', ?)",
                (case_id, r["txn_date"], r["evidence_id"], summary),
            )
        # Structured communication/chat events.
        mrows = conn.execute(
            "SELECT id, evidence_id, platform, sender, timestamp, message_text, source_ref "
            "FROM communications WHERE case_id = ? AND timestamp IS NOT NULL AND timestamp != ''",
            (case_id,),
        ).fetchall()
        for r in mrows:
            msg = (r["message_text"] or "").replace("\n", " ")[:140]
            summary = f"{r['platform'] or 'message'} message #{r['id']} from {r['sender'] or 'unknown'}: {msg}"
            if r["source_ref"]:
                summary += f" ({r['source_ref']})"
            conn.execute(
                "INSERT INTO timeline (case_id, ts, source_evidence_id, kind, summary) "
                "VALUES (?, ?, ?, 'auto', ?)",
                (case_id, r["timestamp"], r["evidence_id"], summary),
            )

        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM timeline WHERE case_id = ?", (case_id,)
        ).fetchone()["c"]
        return count
    finally:
        conn.close()


def list_events(case_uid: str) -> List[Dict[str, Any]]:
    """Return the merged timeline ordered chronologically (nulls last)."""
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return []
        rows = conn.execute(
            "SELECT ts, source_evidence_id, kind, summary FROM timeline "
            "WHERE case_id = ? ORDER BY (ts IS NULL), ts",
            (case_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"ts": r["ts"], "evidence_id": r["source_evidence_id"],
         "kind": r["kind"], "summary": r["summary"]}
        for r in rows
    ]
