"""Business logic for investigations (cases).

All SQLite access is centralised here using prepared statements and short-
lived, WAL-tuned connections from :mod:`investigation_app.extensions`.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from investigation_app.extensions import db_write_lock, evidence_dir, get_connection, run_with_db_retry
from investigation_app.services import audit, jobs

_VALID_AI_MODES = {"standard", "smart", "deep"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_case(row) -> Dict[str, Any]:
    return {
        "uid": row["uid"],
        "title": row["title"],
        "reference_no": row["reference_no"],
        "status": row["status"],
        "ai_mode": row["ai_mode"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class CaseService:
    """CRUD + aggregate queries for investigations."""

    def create_case(
        self,
        title: str,
        reference_no: Optional[str] = None,
        ai_mode: str = "standard",
    ) -> Dict[str, Any]:
        if ai_mode not in _VALID_AI_MODES:
            ai_mode = "standard"
        uid = uuid.uuid4().hex[:12]
        now = _now()
        def _insert_case():
            conn = get_connection()
            try:
                conn.execute(
                    "INSERT INTO cases (uid, title, reference_no, status, ai_mode, "
                    "metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'open', ?, '{}', ?, ?)",
                    (uid, title, reference_no, ai_mode, now, now),
                )
                conn.commit()
                return conn.execute(
                    "SELECT * FROM cases WHERE uid = ?", (uid,)
                ).fetchone()
            finally:
                conn.close()

        row = run_with_db_retry(_insert_case, attempts=8, base_sleep=0.2)
        audit.record("case.create", uid=uid, title=title)
        return _row_to_case(row)

    def list_cases(self) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM cases ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_case(r) for r in rows]

    def get_case(self, uid: str) -> Optional[Dict[str, Any]]:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM cases WHERE uid = ?", (uid,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_case(row) if row else None

    def update_case(self, uid: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        fields = []
        values: List[Any] = []
        if "title" in payload and str(payload["title"]).strip():
            fields.append("title = ?")
            values.append(str(payload["title"]).strip())
        if "reference_no" in payload:
            fields.append("reference_no = ?")
            values.append((payload["reference_no"] or "").strip() or None)
        if "status" in payload and payload["status"] in {"open", "archived", "closed"}:
            fields.append("status = ?")
            values.append(payload["status"])
        if payload.get("ai_mode") in _VALID_AI_MODES:
            fields.append("ai_mode = ?")
            values.append(payload["ai_mode"])
        if not fields:
            return self.get_case(uid)
        fields.append("updated_at = ?")
        values.append(_now())
        values.append(uid)
        def _update_case():
            conn = get_connection()
            try:
                cur = conn.execute(
                    f"UPDATE cases SET {', '.join(fields)} WHERE uid = ?", values
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

        rowcount = run_with_db_retry(_update_case, attempts=8, base_sleep=0.2)
        if rowcount == 0:
            return None
        audit.record("case.update", uid=uid)
        return self.get_case(uid)


    def close_investigation(self, uid: str) -> Dict[str, Any]:
        """Permanently close one investigation and clear all local case data.

        This is intentionally stronger than archiving: it removes the case row,
        every derived intelligence row, FTS rows, workspace state, AI chats,
        evidence records, and physical evidence files that are not shared by
        another case. Audit-log files are left intact as operator accountability
        logs, not investigation workspace data.
        """
        cleanup_files: list[dict[str, str]] = []
        closed_title = ""

        def _close_rows() -> Dict[str, Any]:
            nonlocal cleanup_files, closed_title
            conn = get_connection()
            try:
                row = conn.execute("SELECT id, title FROM cases WHERE uid = ?", (uid,)).fetchone()
                if row is None:
                    return {"error": "case not found", "status": 404}
                case_id = int(row["id"])
                closed_title = row["title"] or uid
                evidence_rows = conn.execute(
                    "SELECT id, original_name, stored_path, sha256 FROM evidence WHERE case_id = ?",
                    (case_id,),
                ).fetchall()
                evidence_ids = [int(r["id"]) for r in evidence_rows]
                jobs.cancel_case(uid, evidence_ids)

                cleanup_files = [
                    {
                        "id": str(r["id"]),
                        "name": r["original_name"] or f"Evidence #{r['id']}",
                        "stored_path": r["stored_path"] or "",
                        "sha256": r["sha256"] or "",
                    }
                    for r in evidence_rows
                ]

                # Clear non-FK virtual/index tables and derived intelligence first.
                conn.execute("DELETE FROM search_index WHERE case_id = ?", (case_id,))
                if evidence_ids:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    conn.execute(f"DELETE FROM chain_of_custody WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM evidence_stages WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM embeddings WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM communications WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM social_profiles WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM technical_indicators WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM transactions WHERE evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(f"DELETE FROM timeline WHERE source_evidence_id IN ({placeholders})", evidence_ids)
                    conn.execute(
                        f"DELETE FROM evidence_similarity WHERE case_id = ? AND (a_id IN ({placeholders}) OR b_id IN ({placeholders}))",
                        [case_id, *evidence_ids, *evidence_ids],
                    )
                    conn.execute(f"DELETE FROM entity_links WHERE evidence_id IN ({placeholders})", evidence_ids)

                for table in (
                    "relationships", "evidence_similarity", "timeline", "entities",
                    "people", "notes", "bookmarks", "tasks", "ai_chats",
                    "activity", "workspace", "evidence",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE case_id = ?", (case_id,))

                conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
                conn.commit()

                # Keep files only when another investigation still references the same SHA.
                for item in cleanup_files:
                    sha = item.get("sha256") or ""
                    if not sha:
                        item["remove_file"] = "1"
                        continue
                    remaining = conn.execute("SELECT COUNT(*) AS c FROM evidence WHERE sha256 = ?", (sha,)).fetchone()
                    item["remove_file"] = "1" if int(remaining["c"] or 0) == 0 else "0"
                return {
                    "ok": True,
                    "closed": uid,
                    "title": closed_title,
                    "evidence_removed": len(evidence_ids),
                    "status": 200,
                }
            finally:
                conn.close()

        try:
            result = run_with_db_retry(_close_rows, attempts=12, base_sleep=0.25)
        except Exception as exc:
            return {"error": f"database busy while closing investigation: {exc}", "status": 423}
        if result.get("error"):
            return result

        root = os.path.abspath(evidence_dir())
        removed_files = 0
        for item in cleanup_files:
            if item.get("remove_file") != "1":
                continue
            stored_path = item.get("stored_path") or ""
            if not stored_path:
                continue
            try:
                target = os.path.abspath(stored_path)
                if target.startswith(root + os.sep) and os.path.exists(target):
                    try:
                        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass
                    os.remove(target)
                    removed_files += 1
            except OSError:
                pass

        audit.record(
            "case.close",
            uid=uid,
            title=closed_title,
            evidence=result.get("evidence_removed", 0),
            files_removed=removed_files,
        )
        result["files_removed"] = removed_files
        return result

    def dashboard_summary(self) -> Dict[str, Any]:
        """Aggregate counts for the hero card + intelligence widgets.

        The original keys (active_investigations, evidence_count,
        pending_tasks, recent_investigations, storage_bytes) are preserved
        exactly so the existing UI keeps working; the intelligence counts are
        additive and refresh automatically as evidence is processed.
        """
        conn = get_connection()
        try:
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM cases WHERE status = 'open'"
            ).fetchone()["c"]
            evidence = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence"
            ).fetchone()["c"]
            pending_tasks = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE done = 0"
            ).fetchone()["c"]
            recent = conn.execute(
                "SELECT uid, title, updated_at FROM cases "
                "ORDER BY updated_at DESC LIMIT 5"
            ).fetchall()

            entities = conn.execute(
                "SELECT COUNT(*) AS c FROM entities"
            ).fetchone()["c"]
            by_type_rows = conn.execute(
                "SELECT type, COUNT(*) AS c FROM entities GROUP BY type"
            ).fetchall()
            by_type = {r["type"]: r["c"] for r in by_type_rows}
            transaction_count = conn.execute(
                "SELECT COUNT(*) AS c FROM transactions"
            ).fetchone()["c"]
            message_count = conn.execute(
                "SELECT COUNT(*) AS c FROM communications"
            ).fetchone()["c"]
            social_profile_count = conn.execute(
                "SELECT COUNT(*) AS c FROM social_profiles"
            ).fetchone()["c"]
            technical_indicator_count = conn.execute(
                "SELECT COUNT(*) AS c FROM technical_indicators"
            ).fetchone()["c"]
            transaction_banks = conn.execute(
                "SELECT COUNT(DISTINCT bank) AS c FROM transactions WHERE bank IS NOT NULL AND bank != ''"
            ).fetchone()["c"]
            transaction_utrs = conn.execute(
                "SELECT COUNT(DISTINCT utr) AS c FROM transactions WHERE utr IS NOT NULL AND utr != ''"
            ).fetchone()["c"]
            transaction_accounts = conn.execute(
                "SELECT COUNT(DISTINCT COALESCE(NULLIF(account_no,''), NULLIF(receiver_account,''), NULLIF(sender_account,''))) AS c FROM transactions"
            ).fetchone()["c"]
            failed_stages = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence_stages WHERE state != 'ok'"
            ).fetchone()["c"]
            ai_ready = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence WHERE UPPER(status) = 'AI_READY'"
            ).fetchone()["c"]
            timeline_events = conn.execute(
                "SELECT COUNT(*) AS c FROM timeline"
            ).fetchone()["c"]
            duplicates = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence_similarity"
            ).fetchone()["c"]
            ai_documents = conn.execute(
                "SELECT COUNT(DISTINCT evidence_id) AS c FROM embeddings"
            ).fetchone()["c"]
            embeddings_count = conn.execute(
                "SELECT COUNT(*) AS c FROM embeddings"
            ).fetchone()["c"]
            graph_nodes = conn.execute(
                "SELECT COUNT(*) AS c FROM entities"
            ).fetchone()["c"]
            relationships_count = conn.execute(
                "SELECT COUNT(*) AS c FROM relationships"
            ).fetchone()["c"]
            search_index = conn.execute(
                "SELECT COUNT(*) AS c FROM search_index"
            ).fetchone()["c"]
            processing_queue = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence "
                "WHERE status IN ('pending', 'processing')"
            ).fetchone()["c"]
        finally:
            conn.close()
        return {
            "active_investigations": active,
            "evidence_count": evidence,
            "pending_tasks": pending_tasks,
            "recent_investigations": [
                {"uid": r["uid"], "title": r["title"], "updated_at": r["updated_at"]}
                for r in recent
            ],
            "storage_bytes": self._storage_bytes(),
            # --- Intelligence widgets (additive) --------------------------
            "entity_count": entities,
            "transaction_count": transaction_count,
            "message_count": message_count,
            "social_profile_count": social_profile_count,
            "technical_indicator_count": technical_indicator_count,
            "accounts": transaction_accounts or by_type.get("account", 0),
            "banks": transaction_banks or by_type.get("bank", 0),
            "utrs": transaction_utrs or by_type.get("utr", 0),
            "ai_ready_evidence": ai_ready,
            "failed_stages": failed_stages,
            "similar_evidence_links": duplicates,
            "phones": by_type.get("phone", 0),
            "emails": by_type.get("email", 0),
            "upis": by_type.get("upi", 0),
            "ips": by_type.get("ipv4", 0),
            "entity_types": by_type,
            "timeline_events": timeline_events,
            "duplicates": duplicates,
            "processing_queue": processing_queue,
            "ai_documents": ai_documents,
            "embeddings": embeddings_count,
            "search_index": search_index,
            "knowledge_graph_nodes": graph_nodes,
            "relationships": relationships_count,
        }

    @staticmethod
    def _storage_bytes() -> int:
        """Total bytes used by the read-only evidence store."""
        total = 0
        base = evidence_dir()
        for root, _dirs, files in os.walk(base):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
