"""Ordered evidence-processing pipeline.

Runs one evidence item through the complete offline intelligence path:
metadata/type -> text/OCR -> structured financial intelligence -> entity
extraction -> transactions -> correlation graph -> FTS5 -> AI chunks/embeddings
-> duplicate/similarity analysis -> timeline -> AI_READY.

Every stage is isolated: one bad file or extractor marks only that stage as an
error and the rest of the safe stages continue. This keeps uploads instant and
processing incremental; there is no manual reindex/restart step.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from investigation_app.extensions import db_write_lock, get_connection, run_with_db_retry
from investigation_app.pipeline import embeddings, extraction, financial_extract, indexing, metadata, relationships, universal_intel
from investigation_app.services import audit, jobs, timeline_service

_logger = logging.getLogger("iie.pipeline")

# Fast-mode defaults are tuned for the portal: structured rows remain fully
# persisted, while very large raw text is capped for FTS/RAG because exact
# account/UTR/amount questions are answered from the transaction table.
_PROGRESS_CACHE: dict[int, tuple[float, str, str]] = {}
_MAX_ENTITY_SCAN_CHARS = int(os.environ.get("IIE_MAX_ENTITY_SCAN_CHARS", "64000000"))
_MAX_FTS_TEXT_CHARS = 1_500_000
_MAX_RAG_TEXT_CHARS = 650_000


def _is_large_structured_evidence(text: str, intel: dict | None = None) -> bool:
    txns = len((intel or {}).get("transactions") or [])
    return bool(text and len(text) > 900_000 and txns >= 800)


def _sample_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.72)
    tail = limit - head
    marker = "\n\n--- middle content omitted for faster indexed processing; structured rows remain in SQLite ---\n\n"
    return text[:head] + marker + text[-tail:]

def _dedupe_entities(entities) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for ent in entities or []:
        if not ent or not ent.get("type") or not ent.get("norm"):
            continue
        key = (str(ent.get("type", "")).lower(), str(ent.get("norm", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": ent.get("type"), "value": ent.get("value", ""), "norm": ent.get("norm")})
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _commit_every(conn, count: int, step: int = 250) -> None:
    if count and count % step == 0:
        conn.commit()




def _set_progress(evidence_id: int, percent: float, detail: str = "", status: str | None = None, current: int | None = None, total: int | None = None, force: bool = False) -> None:
    """Persist processing progress for the UI with DB-write throttling.

    Older builds wrote progress on nearly every row/update. On large Excel
    evidence that alone could slow processing and aggravate SQLite locks. This
    keeps visible percent updates accurate enough for the progress bar while
    reducing write amplification.
    """
    pct = max(0.0, min(100.0, float(percent or 0.0)))
    short_detail = (detail or "")[:240]
    status_key = status or ""
    last = _PROGRESS_CACHE.get(evidence_id)
    if not force and last:
        last_pct, last_detail, last_status = last
        # Always write final/error/status changes, otherwise require a visible movement.
        if pct < 100 and status_key == last_status and abs(pct - last_pct) < 1.0 and short_detail == last_detail:
            return

    def _update() -> None:
        c = get_connection()
        try:
            fields = ["progress_percent = ?", "progress_detail = ?"]
            params: list[Any] = [pct, short_detail]
            if status:
                fields.append("status = ?")
                params.append(status)
            if current is not None:
                fields.append("progress_current = ?")
                params.append(int(max(0, current)))
            if total is not None:
                fields.append("progress_total = ?")
                params.append(int(max(0, total)))
            params.append(evidence_id)
            c.execute(f"UPDATE evidence SET {', '.join(fields)} WHERE id = ?", params)
            c.commit()
            _PROGRESS_CACHE[evidence_id] = (pct, short_detail, status_key)
        finally:
            c.close()

    try:
        run_with_db_retry(_update, attempts=2, base_sleep=0.03)
    except Exception:
        _logger.debug("progress update skipped for evidence %s", evidence_id, exc_info=True)

def _write_stage(conn, fn: Callable[[], Any]) -> Any:
    """Run one write-heavy stage behind the SQLite write gate."""
    with db_write_lock():
        result = fn()
        conn.commit()
        return result


def _mark_stage(conn, evidence_id: int, stage: str, state: str, detail: str = "") -> None:
    """Record the outcome of one pipeline stage (best-effort, never raises)."""
    try:
        with db_write_lock():
            conn.execute(
                "INSERT INTO evidence_stages (evidence_id, stage, state, detail, at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(evidence_id, stage) DO UPDATE SET "
                "state = excluded.state, detail = excluded.detail, at = excluded.at",
                (evidence_id, stage, state, detail[:500], _now()),
            )
            conn.commit()
    except Exception:  # pragma: no cover - stage tracking must never break flow
        _logger.exception("Failed to record stage %s for evidence %s", stage, evidence_id)


def _run_stage(conn, evidence_id: int, stage: str, fn: Callable[[], Any]) -> Optional[Any]:
    """Execute one stage in isolation. Log + mark failure, never propagate."""
    try:
        result = fn()
        _mark_stage(conn, evidence_id, stage, "ok", str(result) if result is not None else "")
        _logger.info("evidence %s stage '%s' ok (%s)", evidence_id, stage, result)
        return result
    except Exception as exc:  # noqa: BLE001 - isolate every stage
        _logger.exception("evidence %s stage '%s' failed", evidence_id, stage)
        _mark_stage(conn, evidence_id, stage, "error", str(exc))
        return None


def _entities_from_metadata(meta: dict) -> list:
    """Derive entities from rich extracted metadata (EXIF/GPS, doc props)."""
    extracted = (meta or {}).get("extracted") or {}
    out: list = []

    def _add(etype: str, value) -> None:
        if value is None:
            return
        raw = str(value).strip()
        if raw:
            out.append({"type": etype, "value": raw, "norm": raw.lower()})

    gps = extracted.get("gps")
    if isinstance(gps, dict) and "lat" in gps and "lon" in gps:
        _add("gps", f"{gps['lat']},{gps['lon']}")
    for key in ("Artist", "author", "last_modified_by"):
        _add("name", extracted.get(key))
    for key in ("DateTimeOriginal", "DateTime", "created", "modified", "creationdate", "moddate"):
        _add("date", extracted.get(key))
    return out


def _persist_entities(conn, case_id: int, evidence_id: int, entities) -> int:
    """Replace this evidence item's entity links using bulk inserts.

    The previous row-by-row insert + select + FTS update path was accurate but
    slow for large BankAction sheets with thousands of accounts/UTRs. This
    version dedupes first, inserts entities in batches, fetches their IDs in
    grouped queries, and indexes entity values in one FTS batch.
    """
    entities = _dedupe_entities(entities)
    conn.execute("DELETE FROM entity_links WHERE evidence_id = ?", (evidence_id,))
    conn.execute(
        "DELETE FROM entities WHERE case_id = ? AND id NOT IN (SELECT DISTINCT entity_id FROM entity_links)",
        (case_id,),
    )
    if not entities:
        conn.commit()
        return 0

    conn.executemany(
        "INSERT OR IGNORE INTO entities (case_id, type, value, norm) VALUES (?, ?, ?, ?)",
        [(case_id, ent["type"], ent.get("value", ""), ent["norm"]) for ent in entities],
    )

    entity_map: dict[tuple[str, str], tuple[int, str]] = {}
    by_type: dict[str, list[str]] = {}
    for ent in entities:
        by_type.setdefault(str(ent["type"]), []).append(str(ent["norm"]))

    for etype, norms in by_type.items():
        unique_norms = sorted(set(norms))
        for i in range(0, len(unique_norms), 900):
            batch = unique_norms[i:i + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT id, type, norm, value FROM entities WHERE case_id = ? AND type = ? AND norm IN ({placeholders})",
                [case_id, etype, *batch],
            ).fetchall()
            for r in rows:
                entity_map[(r["type"], r["norm"])] = (int(r["id"]), r["value"] or "")

    link_rows = []
    fts_rows = []
    for ent in entities:
        item = entity_map.get((ent["type"], ent["norm"]))
        if not item:
            continue
        entity_id, value = item
        link_rows.append((entity_id, evidence_id))
        if value and str(value).strip():
            fts_rows.append((case_id, "entity", entity_id, value))

    conn.executemany("INSERT OR IGNORE INTO entity_links (entity_id, evidence_id) VALUES (?, ?)", link_rows)
    if fts_rows:
        entity_ids = [r[2] for r in fts_rows]
        for i in range(0, len(entity_ids), 900):
            batch = entity_ids[i:i + 900]
            placeholders = ",".join("?" for _ in batch)
            conn.execute(f"DELETE FROM search_index WHERE ref_type = 'entity' AND ref_id IN ({placeholders})", batch)
        conn.executemany(
            "INSERT INTO search_index (case_id, ref_type, ref_id, content) VALUES (?, ?, ?, ?)",
            fts_rows,
        )
    conn.commit()
    return len(link_rows)


def _persist_transactions(conn, case_id: int, evidence_id: int, transactions: list[dict]) -> int:
    """Replace structured transaction rows using chunked bulk insert."""
    txns = transactions or []
    conn.execute("DELETE FROM transactions WHERE evidence_id = ?", (evidence_id,))
    if not txns:
        conn.commit()
        return 0

    sql = (
        "INSERT INTO transactions ("
        "case_id, evidence_id, source_file, source_ref, file_hash, layer, txn_date, utr, amount, "
        "disputed_amount, lien_amount, sender_account, receiver_account, account_no, ifsc, bank, upi, "
        "wallet, merchant, status, remarks, meta_json, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def row_values(t: dict) -> tuple:
        return (
            case_id,
            evidence_id,
            t.get("source_file"),
            t.get("source_ref"),
            t.get("file_hash"),
            t.get("layer"),
            t.get("txn_date"),
            financial_extract.norm_value("utr", t.get("utr", "")) if t.get("utr") else "",
            t.get("amount"),
            t.get("disputed_amount"),
            t.get("lien_amount"),
            financial_extract.norm_value("account", t.get("sender_account", "")) if t.get("sender_account") else "",
            financial_extract.norm_value("account", t.get("receiver_account", "")) if t.get("receiver_account") else "",
            financial_extract.norm_value("account", t.get("account_no", "")) if t.get("account_no") else "",
            financial_extract.norm_value("ifsc", t.get("ifsc", "")) if t.get("ifsc") else "",
            t.get("bank") or "",
            financial_extract.norm_value("upi", t.get("upi", "")) if t.get("upi") else "",
            t.get("wallet") or "",
            t.get("merchant") or "",
            t.get("status") or "",
            t.get("remarks") or "",
            json.dumps(t.get("metadata") or {}, ensure_ascii=False),
            _now(),
        )

    total = len(txns)
    batch_size = 1000
    for start_idx in range(0, total, batch_size):
        batch = txns[start_idx:start_idx + batch_size]
        conn.executemany(sql, [row_values(t) for t in batch])
        done = min(start_idx + len(batch), total)
        pct = 68.0 + min(4.0, (done / max(total, 1)) * 4.0)
        conn.execute(
            "UPDATE evidence SET progress_percent = ?, progress_current = ?, progress_total = ?, progress_detail = ? WHERE id = ?",
            (pct, done, total, f"Saving transaction rows {done:,}/{total:,}", evidence_id),
        )
        conn.commit()
    return total


def _persist_messages(conn, case_id: int, evidence_id: int, messages: list[dict]) -> int:
    """Replace communication/message records for one evidence item."""
    conn.execute("DELETE FROM communications WHERE evidence_id = ?", (evidence_id,))
    conn.commit()
    for idx, m in enumerate(messages or [], 1):
        conn.execute(
            "INSERT INTO communications ("
            "case_id, evidence_id, platform, sender, receiver, sender_handle, receiver_handle, "
            "message_text, timestamp, entities_json, attachments_json, urls_json, amounts_json, "
            "risk_flags_json, source_ref, confidence, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                evidence_id,
                m.get("platform") or "",
                m.get("sender") or "",
                m.get("receiver") or "",
                m.get("sender_handle") or "",
                m.get("receiver_handle") or "",
                m.get("message_text") or "",
                m.get("timestamp") or "",
                json.dumps(m.get("entities_found") or [], ensure_ascii=False),
                json.dumps(m.get("attachments") or [], ensure_ascii=False),
                json.dumps(m.get("urls") or [], ensure_ascii=False),
                json.dumps(m.get("amounts") or [], ensure_ascii=False),
                json.dumps(m.get("risk_flags") or [], ensure_ascii=False),
                m.get("source_ref") or "",
                float(m.get("confidence") or 0.0),
                _now(),
            ),
        )
        _commit_every(conn, idx)
    conn.commit()
    return len(messages or [])


def _persist_social_profiles(conn, case_id: int, evidence_id: int, profiles: list[dict]) -> int:
    """Replace social profile/handle records for one evidence item."""
    conn.execute("DELETE FROM social_profiles WHERE evidence_id = ?", (evidence_id,))
    conn.commit()
    for idx, p in enumerate(profiles or [], 1):
        conn.execute(
            "INSERT INTO social_profiles ("
            "case_id, evidence_id, platform, profile_name, username, profile_url, bio, "
            "metadata_json, source_ref, confidence, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                evidence_id,
                p.get("platform") or "",
                p.get("profile_name") or "",
                p.get("username") or "",
                p.get("profile_url") or "",
                p.get("bio") or "",
                json.dumps(p.get("metadata") or {}, ensure_ascii=False),
                p.get("source_ref") or "",
                float(p.get("confidence") or 0.0),
                _now(),
            ),
        )
        _commit_every(conn, idx)
    conn.commit()
    return len(profiles or [])


def _persist_technical_indicators(conn, case_id: int, evidence_id: int, indicators: list[dict]) -> int:
    """Replace universal technical indicators for one evidence item."""
    conn.execute("DELETE FROM technical_indicators WHERE evidence_id = ?", (evidence_id,))
    conn.commit()
    count = 0
    for i in indicators or []:
        norm = i.get("norm") or universal_intel.norm_value(i.get("type", "indicator"), i.get("value", ""))
        if not norm or not i.get("value"):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO technical_indicators ("
            "case_id, evidence_id, type, value, norm, source_ref, confidence, metadata_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                evidence_id,
                str(i.get("type") or "indicator"),
                str(i.get("value") or ""),
                str(norm),
                i.get("source_ref") or "",
                float(i.get("confidence") or 0.0),
                json.dumps(i.get("metadata") or {}, ensure_ascii=False),
                _now(),
            ),
        )
        count += 1
        _commit_every(conn, count)
    conn.commit()
    return count



def _case_still_open(conn, case_id: int) -> bool:
    row = conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    return bool(row and row["status"] != "closed")

def process_evidence(evidence_id: int) -> None:
    """Run the full pipeline for one evidence row. Safe to call in a worker."""
    conn = get_connection()
    case_uid: Optional[str] = None
    try:
        row = conn.execute(
            "SELECT e.id, e.case_id, e.original_name, e.stored_path, e.mime, e.sha256, c.uid AS case_uid "
            "FROM evidence e JOIN cases c ON c.id = e.case_id WHERE e.id = ?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            return

        case_id = row["case_id"]
        case_uid = row["case_uid"]
        if jobs.is_evidence_cancelled(evidence_id) or jobs.is_case_cancelled(case_uid):
            return
        path = row["stored_path"]
        sha256 = row["sha256"]
        original_name = row["original_name"] or f"evidence-{evidence_id}"

        _set_progress(evidence_id, 1, "Starting evidence processing", "processing", 0, 100, force=True)
        _logger.info("evidence %s pipeline start", evidence_id)

        collected = _run_stage(conn, evidence_id, "metadata", lambda: metadata.collect(path, row["mime"], filename_hint=original_name))
        kind, meta = collected if collected else ("binary", {})
        _set_progress(evidence_id, 8, "Metadata collected", "processing", 8, 100)

        def _text_progress(inner_pct: float, detail: str, current: int, total: int) -> None:
            outer = 10.0 + (max(0.0, min(100.0, float(inner_pct or 0.0))) * 0.35)
            _set_progress(evidence_id, outer, detail or "Extracting evidence text", "processing", current, total)

        text = _run_stage(conn, evidence_id, "text", lambda: metadata.extract_text(path, kind, filename_hint=original_name, progress_callback=_text_progress)) or ""
        _set_progress(evidence_id, 45, f"Text extracted ({len(text):,} chars)", "processing", 45, 100, force=True)
        if jobs.is_evidence_cancelled(evidence_id) or jobs.is_case_cancelled(case_uid) or not _case_still_open(conn, case_id):
            return

        fin_intel = _run_stage(
            conn,
            evidence_id,
            "financial_intel",
            lambda: financial_extract.build_intelligence(text, case_id, evidence_id, sha256, original_name),
        ) or {}
        _set_progress(evidence_id, 54, f"Structured transactions found: {len(fin_intel.get('transactions', []) or []):,}", "processing", 54, 100)
        intel = _run_stage(
            conn,
            evidence_id,
            "classification",
            lambda: universal_intel.enrich_intelligence(fin_intel, text, case_id, evidence_id, sha256, original_name, kind, meta),
        ) or fin_intel
        _set_progress(evidence_id, 60, "Evidence classified", "processing", 60, 100)
        if jobs.is_evidence_cancelled(evidence_id) or jobs.is_case_cancelled(case_uid) or not _case_still_open(conn, case_id):
            return

        large_structured = _is_large_structured_evidence(text, intel)

        # Entity extraction must be complete. Earlier speed patches sampled huge
        # workbooks here; that made the UI look fast but skipped valid phones,
        # emails, URLs, UPI IDs and other indicators beyond the sample window.
        # We now scan the full extracted text up to the extractor's global text
        # cap. Structured financial entities are still also added from rows.
        entity_text = text if len(text or "") <= _MAX_ENTITY_SCAN_CHARS else text[:_MAX_ENTITY_SCAN_CHARS]
        if len(text or "") > _MAX_ENTITY_SCAN_CHARS:
            _set_progress(evidence_id, 61, f"Scanning entities from first {_MAX_ENTITY_SCAN_CHARS:,} chars; raise IIE_MAX_TEXT_CHARS for more", "processing", 61, 100)
        else:
            _set_progress(evidence_id, 61, "Scanning complete text for entities", "processing", 61, 100)
        text_entities = _run_stage(conn, evidence_id, "entities", lambda: extraction.extract(entity_text)) or []
        _set_progress(evidence_id, 64, f"Entities extracted from full text: {len(text_entities):,}", "processing", 64, 100)
        entities = _dedupe_entities(
            text_entities
            + _entities_from_metadata(meta)
            + financial_extract.entities_from_intelligence(intel)
            + universal_intel.entities_from_intelligence(intel)
        )

        if jobs.is_evidence_cancelled(evidence_id) or jobs.is_case_cancelled(case_uid) or not _case_still_open(conn, case_id):
            return
        _set_progress(evidence_id, 68, "Saving transactions", "processing", 68, 100)
        _run_stage(conn, evidence_id, "transactions", lambda: _write_stage(conn, lambda: _persist_transactions(conn, case_id, evidence_id, intel.get("transactions", []))))
        _set_progress(evidence_id, 72, "Saving messages and profiles", "processing", 72, 100)
        _run_stage(conn, evidence_id, "messages", lambda: _write_stage(conn, lambda: _persist_messages(conn, case_id, evidence_id, intel.get("messages", []))))
        _run_stage(conn, evidence_id, "social_profiles", lambda: _write_stage(conn, lambda: _persist_social_profiles(conn, case_id, evidence_id, intel.get("social_profiles", []))))
        _run_stage(conn, evidence_id, "technical_indicators", lambda: _write_stage(conn, lambda: _persist_technical_indicators(conn, case_id, evidence_id, intel.get("technical_indicators_universal", []))))

        _set_progress(evidence_id, 76, "Saving entity links", "processing", 76, 100)
        _run_stage(conn, evidence_id, "correlation", lambda: _write_stage(conn, lambda: _persist_entities(conn, case_id, evidence_id, entities)))

        profile_text = "\n".join(p for p in (financial_extract.profile_to_index_text(intel), universal_intel.profile_to_index_text(intel)) if p)
        raw_for_fts = _sample_text(text, _MAX_FTS_TEXT_CHARS) if large_structured else text
        indexed_text = raw_for_fts
        if profile_text:
            indexed_text = (raw_for_fts or "") + "\n\n--- Structured Intelligence ---\n" + profile_text
        _set_progress(evidence_id, 82, "Indexing searchable text", "processing", 82, 100)
        _run_stage(conn, evidence_id, "fts", lambda: _write_stage(conn, lambda: indexing.index_evidence_text(conn, case_id, evidence_id, indexed_text)))

        _set_progress(evidence_id, 86, "Preparing AI-search chunks", "processing", 86, 100)
        rag_text = _sample_text(indexed_text, _MAX_RAG_TEXT_CHARS) if large_structured else indexed_text
        chunk_count = _run_stage(conn, evidence_id, "embeddings", lambda: _write_stage(conn, lambda: embeddings.index_evidence(conn, case_id, evidence_id, sha256, rag_text))) or 0

        _set_progress(evidence_id, 91, "Building fast evidence links", "processing", 91, 100)
        _run_stage(conn, evidence_id, "graph", lambda: _write_stage(conn, lambda: relationships.link_entities(conn, case_id, evidence_id)))
        _run_stage(conn, evidence_id, "duplicates", lambda: _write_stage(conn, lambda: relationships.analyse_duplicates(conn, case_id, evidence_id)))
        conn.commit()

        _set_progress(evidence_id, 96, "Rebuilding timeline", "processing", 96, 100)
        _run_stage(conn, evidence_id, "timeline", lambda: _write_stage(conn, lambda: timeline_service.rebuild(case_uid)))

        final_status = "AI_READY" if chunk_count else "COMPLETED"
        with db_write_lock():
            conn.execute(
                "UPDATE evidence SET status = ?, progress_percent = 100, progress_current = 100, progress_total = 100, progress_detail = 'Complete', meta_json = ?, intel_json = ? WHERE id = ?",
                (final_status, json.dumps(meta, ensure_ascii=False), json.dumps(intel, ensure_ascii=False), evidence_id),
            )
            conn.execute(
                "INSERT INTO activity (case_id, msg, at) VALUES (?, ?, ?)",
                (case_id, f"Evidence #{evidence_id} processed ({len(entities)} entities, {len(intel.get('transactions', []))} transactions, {len(intel.get('messages', []))} messages)", _now()),
            )
            conn.commit()
        audit.record("evidence.processed", id=evidence_id, kind=kind, entities=len(entities), transactions=len(intel.get("transactions", [])))
        _logger.info("evidence %s pipeline completed", evidence_id)
    except Exception:
        _logger.exception("Pipeline failed for evidence %s", evidence_id)
        try:
            with db_write_lock():
                conn.execute("UPDATE evidence SET status = 'FAILED', progress_percent = 100, progress_detail = 'Processing failed - check logs' WHERE id = ?", (evidence_id,))
                conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
