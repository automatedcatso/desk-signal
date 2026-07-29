"""Evidence intake and querying.

Handles safe, read-only uploads:

* Filenames are sanitised; content is stored under
  ``instance/evidence_store/<sha256>`` (path-traversal safe - the on-disk name
  is the digest, never user input).
* Streamed hashing bounds memory; the digest is the per-case dedup key.
* Stored files are made read-only so the original evidence is never modified.
* A chain-of-custody row is written on intake with the verified hash.

Processing is dispatched to the background job runner so the request returns
immediately (instant UI).
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from investigation_app.extensions import db_write_lock, evidence_dir, get_connection, run_with_db_retry
from investigation_app.pipeline import registry
from investigation_app.pipeline.hashing import sha256_file
from investigation_app.services import audit, jobs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceService:
    """Intake, dedup, and listing of evidence for an investigation."""

    def _case_id(self, conn, case_uid: str) -> Optional[int]:
        row = conn.execute(
            "SELECT id FROM cases WHERE uid = ?", (case_uid,)
        ).fetchone()
        return row["id"] if row else None

    def add_upload(self, case_uid: str, file: FileStorage) -> Dict[str, Any]:
        """Store an uploaded file read-only, dedup by hash, queue processing."""
        original_name = secure_filename(file.filename or "evidence.bin")
        store = evidence_dir()

        # Write to a temp file first, hash it, then move to <sha256>.
        fd, tmp_path = tempfile.mkstemp(dir=store)
        os.close(fd)
        file.save(tmp_path)
        digest = sha256_file(tmp_path)
        size = os.path.getsize(tmp_path)
        final_path = os.path.join(store, digest)
        mime = file.mimetype or "application/octet-stream"

        def _db_insert() -> Dict[str, Any]:
            conn = get_connection()
            try:
                case_id = self._case_id(conn, case_uid)
                if case_id is None:
                    return {"error": "case not found", "status": 404}

                existing = conn.execute(
                    "SELECT id FROM evidence WHERE case_id = ? AND sha256 = ?",
                    (case_id, digest),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return {
                        "id": existing["id"], "sha256": digest,
                        "duplicate": True, "status": 200,
                    }

                if not os.path.exists(final_path):
                    os.replace(tmp_path, final_path)
                    os.chmod(final_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
                elif os.path.exists(tmp_path):
                    os.remove(tmp_path)

                cur = conn.execute(
                    "INSERT INTO evidence (case_id, original_name, stored_path, mime, "
                    "size, sha256, status, progress_percent, progress_current, progress_total, progress_detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 0, 100, 'Queued for processing', ?)",
                    (case_id, original_name, final_path, mime, size, digest, _now()),
                )
                evidence_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO chain_of_custody (evidence_id, action, sha256, actor, at) "
                    "VALUES (?, 'intake', ?, 'operator', ?)",
                    (evidence_id, digest, _now()),
                )
                conn.commit()
                return {"id": evidence_id, "sha256": digest, "duplicate": False, "status": 201}
            finally:
                conn.close()

        try:
            result = run_with_db_retry(_db_insert, attempts=10, base_sleep=0.25)
        except Exception as exc:  # keep upload endpoint from Flask 500s on lock storms
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return {"error": f"database busy while saving evidence: {exc}", "status": 423}

        # Any non-new upload path (duplicate, missing case, DB busy, etc.) must
        # remove the temporary file. Earlier builds leaked temp uploads when the
        # case UID was wrong or the DB returned an error.
        if result.get("status") != 201 and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if result.get("status") == 201:
            audit.record(
                "evidence.intake", id=result.get("id"), case=case_uid, sha256=digest
            )
            jobs.submit(registry.process_evidence, int(result["id"]))
        return result


    def reprocess_evidence(self, case_uid: str, evidence_id: int) -> Dict[str, Any]:
        """Queue one existing evidence item for pipeline reprocessing.

        Useful after parser upgrades: stored originals are not modified and all
        derived rows are replaced idempotently by the pipeline stages.
        """
        def _queue_one():
            conn = get_connection()
            try:
                case_id = self._case_id(conn, case_uid)
                if case_id is None:
                    return {"error": "case not found", "status": 404}
                row = conn.execute(
                    "SELECT id FROM evidence WHERE id = ? AND case_id = ?",
                    (evidence_id, case_id),
                ).fetchone()
                if row is None:
                    return {"error": "evidence not found", "status": 404}
                conn.execute("UPDATE evidence SET status = 'pending', progress_percent = 0, progress_current = 0, progress_total = 100, progress_detail = 'Queued for reprocessing' WHERE id = ?", (evidence_id,))
                conn.commit()
                return {"ok": True}
            finally:
                conn.close()
        try:
            result = run_with_db_retry(_queue_one, attempts=10, base_sleep=0.25)
        except Exception as exc:
            return {"error": f"database busy while queuing reprocess: {exc}", "status": 423}
        if result.get("error"):
            return result
        jobs.submit(registry.process_evidence, evidence_id)
        return {"queued": 1, "id": evidence_id, "status": 202}

    def reprocess_all(self, case_uid: str) -> Dict[str, Any]:
        """Queue every evidence item in a case for reprocessing."""
        def _queue_all():
            conn = get_connection()
            try:
                case_id = self._case_id(conn, case_uid)
                if case_id is None:
                    return {"error": "case not found", "status": 404}
                rows = conn.execute(
                    "SELECT id FROM evidence WHERE case_id = ? ORDER BY id",
                    (case_id,),
                ).fetchall()
                ids = [int(r["id"]) for r in rows]
                if ids:
                    conn.executemany("UPDATE evidence SET status = 'pending', progress_percent = 0, progress_current = 0, progress_total = 100, progress_detail = 'Queued for reprocessing' WHERE id = ?", [(i,) for i in ids])
                    conn.commit()
                return {"ids": ids}
            finally:
                conn.close()
        try:
            queued_result = run_with_db_retry(_queue_all, attempts=10, base_sleep=0.25)
        except Exception as exc:
            return {"error": f"database busy while queuing reprocess: {exc}", "status": 423}
        if queued_result.get("error"):
            return queued_result
        ids = queued_result.get("ids", [])
        for evidence_id in ids:
            jobs.submit(registry.process_evidence, evidence_id)
        return {"queued": len(ids), "ids": ids, "status": 202}

    def list_evidence(self, case_uid: str) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT e.id, e.original_name, e.mime, e.size, e.sha256, e.status, e.created_at, e.intel_json, "
                "COALESCE(e.progress_percent, 0) AS progress_percent, COALESCE(e.progress_current, 0) AS progress_current, "
                "COALESCE(e.progress_total, 0) AS progress_total, COALESCE(e.progress_detail, '') AS progress_detail, "
                "(SELECT COUNT(*) FROM communications m WHERE m.evidence_id = e.id) AS message_count, "
                "(SELECT COUNT(*) FROM social_profiles sp WHERE sp.evidence_id = e.id) AS social_profile_count, "
                "(SELECT COUNT(*) FROM technical_indicators ti WHERE ti.evidence_id = e.id) AS technical_indicator_count "
                "FROM evidence e WHERE e.case_id = ? ORDER BY e.created_at DESC",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            intel = {}
            try:
                intel = json.loads(r["intel_json"] or "{}")
            except (TypeError, ValueError):
                intel = {}
            out.append({
                "id": r["id"], "original_name": r["original_name"], "mime": r["mime"],
                "size": r["size"], "sha256": r["sha256"], "status": r["status"],
                "created_at": r["created_at"],
                "progress_percent": float(r["progress_percent"] or 0),
                "progress_current": int(r["progress_current"] or 0),
                "progress_total": int(r["progress_total"] or 0),
                "progress_detail": r["progress_detail"] or "",
                "summary": intel.get("summary", ""),
                "transaction_count": len(intel.get("transactions") or []),
                "message_count": r["message_count"],
                "social_profile_count": r["social_profile_count"],
                "technical_indicator_count": r["technical_indicator_count"],
                "evidence_types": intel.get("evidence_types") or [],
            })
        return out

    def delete_evidence(self, case_uid: str, evidence_id: int) -> Dict[str, Any]:
        """Remove one mistakenly uploaded evidence item from an investigation.

        The DB cleanup is retried behind the process write gate so delete does
        not race against a long-running XLSX processing job. Manual files are
        removed only after the database commit succeeds.
        """
        cleanup: Dict[str, Any] = {"stored_path": "", "sha256": "", "remove_file": False, "name": ""}

        def _delete_rows() -> Dict[str, Any]:
            conn = get_connection()
            try:
                case_id = self._case_id(conn, case_uid)
                if case_id is None:
                    return {"error": "case not found", "status": 404}
                row = conn.execute(
                    "SELECT id, original_name, stored_path, sha256 FROM evidence "
                    "WHERE case_id = ? AND id = ?",
                    (case_id, evidence_id),
                ).fetchone()
                if row is None:
                    return {"error": "evidence not found", "status": 404}

                original_name = row["original_name"] or f"Evidence #{evidence_id}"
                stored_path = row["stored_path"] or ""
                sha256 = row["sha256"] or ""

                # Virtual/derived tables that are not FK-backed must be cleaned
                # explicitly before the parent evidence row is removed.
                conn.execute(
                    "DELETE FROM search_index WHERE ref_type = 'evidence' AND ref_id = ?",
                    (evidence_id,),
                )
                conn.execute(
                    "DELETE FROM evidence_similarity "
                    "WHERE case_id = ? AND (a_id = ? OR b_id = ?)",
                    (case_id, evidence_id, evidence_id),
                )
                conn.execute("DELETE FROM timeline WHERE source_evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM communications WHERE evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM social_profiles WHERE evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM technical_indicators WHERE evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM transactions WHERE evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM embeddings WHERE evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM evidence_stages WHERE evidence_id = ?", (evidence_id,))
                conn.execute("DELETE FROM evidence WHERE case_id = ? AND id = ?", (case_id, evidence_id))

                # Remove entities that only existed because of this evidence item,
                # and clear their FTS rows so search does not return stale hits.
                orphan_rows = conn.execute(
                    "SELECT e.id FROM entities e "
                    "LEFT JOIN entity_links l ON l.entity_id = e.id "
                    "WHERE e.case_id = ? GROUP BY e.id HAVING COUNT(l.evidence_id) = 0",
                    (case_id,),
                ).fetchall()
                orphan_ids = [int(r["id"]) for r in orphan_rows]
                for entity_id in orphan_ids:
                    conn.execute(
                        "DELETE FROM search_index WHERE ref_type = 'entity' AND ref_id = ?",
                        (entity_id,),
                    )
                if orphan_ids:
                    placeholders = ",".join("?" for _ in orphan_ids)
                    conn.execute(f"DELETE FROM entities WHERE id IN ({placeholders})", orphan_ids)

                remove_file = False
                if sha256:
                    remaining = conn.execute(
                        "SELECT COUNT(*) AS c FROM evidence WHERE sha256 = ?",
                        (sha256,),
                    ).fetchone()
                    remove_file = int(remaining["c"] or 0) == 0

                conn.execute(
                    "INSERT INTO activity (case_id, msg, at) VALUES (?, ?, ?)",
                    (case_id, f"Removed evidence: {original_name}", _now()),
                )
                conn.commit()
                cleanup.update({"stored_path": stored_path, "sha256": sha256, "remove_file": remove_file, "name": original_name})
                return {"ok": True, "removed": evidence_id, "status": 200}
            finally:
                conn.close()

        try:
            result = run_with_db_retry(_delete_rows, attempts=12, base_sleep=0.25)
        except Exception as exc:
            return {"error": f"database busy while deleting evidence: {exc}", "status": 423}
        if result.get("error"):
            return result

        # Physical file cleanup happens after DB commit. It is guarded to the
        # current evidence_store and only happens if no other evidence row uses
        # the same SHA-256 file.
        stored_path = cleanup.get("stored_path") or ""
        if cleanup.get("remove_file") and stored_path:
            try:
                root = os.path.abspath(evidence_dir())
                target = os.path.abspath(stored_path)
                if target.startswith(root + os.sep) and os.path.exists(target):
                    try:
                        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass
                    os.remove(target)
            except OSError:
                pass

        audit.record(
            "evidence.delete",
            id=evidence_id,
            case=case_uid,
            sha256=cleanup.get("sha256") or "",
            name=cleanup.get("name") or f"Evidence #{evidence_id}",
        )
        return result

    def get_graph(self, case_uid: str) -> Dict[str, Any]:
        """Return knowledge-graph nodes (entities) and edges (relationships)."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return {"nodes": [], "edges": []}
            nodes = conn.execute(
                "SELECT id, type, value FROM entities "
                "WHERE case_id = ? "
                "AND type != 'evidence_type' "
                "AND NOT (type IN ('social_handle','instagram_username','telegram_username','facebook_username','social_username') "
                "         AND length(value) <= 4 "
                "         AND value NOT GLOB '*[0-9._]*')",
                (case_id,),
            ).fetchall()
            visible_ids = {int(r["id"]) for r in nodes}
            raw_edges = conn.execute(
                "SELECT src_id, dst_id, weight FROM relationships WHERE case_id = ?",
                (case_id,),
            ).fetchall()
            edges = [r for r in raw_edges if int(r["src_id"]) in visible_ids and int(r["dst_id"]) in visible_ids]
        finally:
            conn.close()
        return {
            "nodes": [
                {"id": r["id"], "type": r["type"], "value": r["value"]}
                for r in nodes
            ],
            "edges": [
                {"src": r["src_id"], "dst": r["dst_id"], "weight": r["weight"]}
                for r in edges
            ],
        }

    def get_duplicates(self, case_uid: str) -> List[Dict[str, Any]]:
        """Return duplicate / similar / shared-entity evidence edges."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT s.a_id, s.b_id, s.score, s.kind, s.reasons, "
                "ea.original_name AS a_name, eb.original_name AS b_name "
                "FROM evidence_similarity s "
                "LEFT JOIN evidence ea ON ea.id = s.a_id "
                "LEFT JOIN evidence eb ON eb.id = s.b_id "
                "WHERE s.case_id = ? ORDER BY s.score DESC",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            try:
                reasons = json.loads(r["reasons"] or "[]")
            except (TypeError, ValueError):
                reasons = []
            out.append({
                "a_id": r["a_id"], "b_id": r["b_id"],
                "a_name": r["a_name"], "b_name": r["b_name"],
                "score": r["score"], "kind": r["kind"], "reasons": reasons,
            })
        return out

    def get_stages(self, evidence_id: int) -> List[Dict[str, Any]]:
        """Return per-stage processing status for one evidence item."""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT stage, state, detail, at FROM evidence_stages "
                "WHERE evidence_id = ? ORDER BY at",
                (evidence_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"stage": r["stage"], "state": r["state"],
             "detail": r["detail"], "at": r["at"]}
            for r in rows
        ]



    def get_transactions(self, case_uid: str) -> List[Dict[str, Any]]:
        """Return structured financial transaction rows for a case."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT t.*, e.original_name FROM transactions t "
                "LEFT JOIN evidence e ON e.id = t.evidence_id "
                "WHERE t.case_id = ? "
                "ORDER BY COALESCE(t.layer, 999), t.txn_date, t.id",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_messages(self, case_uid: str) -> List[Dict[str, Any]]:
        """Return structured chat/message/email-style communication records."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT m.*, e.original_name FROM communications m "
                "LEFT JOIN evidence e ON e.id = m.evidence_id "
                "WHERE m.case_id = ? ORDER BY COALESCE(m.timestamp, ''), m.id LIMIT 2000",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            for key in ("entities_json", "attachments_json", "urls_json", "amounts_json", "risk_flags_json"):
                try:
                    d[key.replace("_json", "")] = json.loads(d.get(key) or "[]")
                except (TypeError, ValueError):
                    d[key.replace("_json", "")] = []
                d.pop(key, None)
            out.append(d)
        return out

    def get_social_profiles(self, case_uid: str) -> List[Dict[str, Any]]:
        """Return extracted social profile/handle records."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT sp.*, e.original_name FROM social_profiles sp "
                "LEFT JOIN evidence e ON e.id = sp.evidence_id "
                "WHERE sp.case_id = ? ORDER BY sp.platform, sp.username LIMIT 1000",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                d["metadata"] = {}
            d.pop("metadata_json", None)
            out.append(d)
        return out

    def get_technical_indicators(self, case_uid: str) -> List[Dict[str, Any]]:
        """Return extracted technical/forensic indicators."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT ti.*, e.original_name FROM technical_indicators ti "
                "LEFT JOIN evidence e ON e.id = ti.evidence_id "
                "WHERE ti.case_id = ? ORDER BY ti.type, ti.value LIMIT 2000",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                d["metadata"] = {}
            d.pop("metadata_json", None)
            out.append(d)
        return out

    def get_intel(self, case_uid: str, evidence_id: int) -> Dict[str, Any]:
        """Return the stored structured intelligence profile for one evidence."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return {"error": "case not found"}
            row = conn.execute(
                "SELECT id, original_name, sha256, status, intel_json FROM evidence "
                "WHERE case_id = ? AND id = ?",
                (case_id, evidence_id),
            ).fetchone()
            if row is None:
                return {"error": "evidence not found"}
            try:
                intel = json.loads(row["intel_json"] or "{}")
            except (TypeError, ValueError):
                intel = {}
            intel.setdefault("source", {})
            intel["source"].update({
                "evidence_id": row["id"],
                "source_file": row["original_name"],
                "file_hash": row["sha256"],
                "status": row["status"],
            })
            return intel
        finally:
            conn.close()

    def get_entities(self, case_uid: str) -> List[Dict[str, Any]]:
        """Return all extracted entities with their evidence link counts."""
        conn = get_connection()
        try:
            case_id = self._case_id(conn, case_uid)
            if case_id is None:
                return []
            rows = conn.execute(
                "SELECT e.id, e.type, e.value, e.norm, "
                "COUNT(l.evidence_id) AS links "
                "FROM entities e "
                "LEFT JOIN entity_links l ON l.entity_id = e.id "
                "WHERE e.case_id = ? GROUP BY e.id "
                "ORDER BY links DESC, e.type",
                (case_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"id": r["id"], "type": r["type"], "value": r["value"],
             "norm": r["norm"], "links": r["links"]}
            for r in rows
        ]
