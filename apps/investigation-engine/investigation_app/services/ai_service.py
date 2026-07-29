"""AI orchestration: retrieval-augmented, mode-aware and provider-aware.

Structured financial questions are answered deterministically from the
SQLite intelligence store first (transactions/entities/similarity/timeline), so
answers such as "list all UTRs" and "show money trail" work instantly in every
mode without relying on an LLM. SMART/DEEP use the same local retrieval layer,
then can call either the local assistant or optional Gemini provider.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from investigation_app.adapters.ai_assistant import AIAssistantAdapter
from investigation_app.adapters.gemini import GeminiAdapter
from investigation_app.extensions import db_write_lock, get_connection, run_with_db_retry
from investigation_app.pipeline import embeddings
from investigation_app.services import audit
from investigation_app.services import account_intel_service

_SMART_K = 12
_DEEP_K = 36
_VALID_PROVIDERS = {"local", "gemini"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _selected_provider(provider: Optional[str]) -> str:
    configured = str(
        current_app.config["IIE"].get("ai", {}).get("default_provider", "local")
    ).strip().lower()
    requested = str(provider or configured).strip().lower()
    return requested if requested in _VALID_PROVIDERS else "local"


def _provider_adapter(provider: str):
    cfg = current_app.config["IIE"]
    if provider == "gemini":
        gemini = cfg["gemini"]
        return GeminiAdapter(
            api_key=gemini.get("api_key", ""),
            model=gemini.get("model", "gemini-3.1-flash-lite"),
            base_url=gemini.get(
                "base_url",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            timeout=gemini.get("request_timeout_seconds", 180),
            max_output_tokens=gemini.get("max_output_tokens", 8192),
        )
    local = cfg["ai_assistant"]
    return AIAssistantAdapter(
        local["base_url"],
        local.get("request_timeout_seconds", 180),
    )


def provider_status() -> Dict[str, Any]:
    """Return non-secret readiness details for UI and diagnostics."""
    cfg = current_app.config["IIE"]
    default_provider = _selected_provider(None)
    providers: Dict[str, Any] = {}
    for provider in ("local", "gemini"):
        adapter = _provider_adapter(provider)
        available, detail = adapter.is_available()
        providers[provider] = {
            "available": available,
            "detail": detail,
            "model": (
                cfg["gemini"].get("model", "gemini-3.1-flash-lite")
                if provider == "gemini"
                else "local assistant"
            ),
        }
    return {
        "default_provider": default_provider,
        "providers": providers,
    }


def _case_id(conn, case_uid: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM cases WHERE uid = ?", (case_uid,)).fetchone()
    return row["id"] if row else None


def _safe_chat_insert(conn, case_id: int, role: str, content: str) -> None:
    """Best-effort chat persistence. Never block or break an AI answer."""
    try:
        with db_write_lock():
            conn.execute(
                "INSERT INTO ai_chats (case_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (case_id, role, content, _now()),
            )
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _retrieve(conn, case_id: int, query: str, k: int) -> List[str]:
    """Return up to k relevant context chunks across ALL evidence formats.

    The retrieval layer is intentionally format-agnostic: PDF/DOCX/PPTX, images
    after OCR, EML/HTML/TXT/CSV/logs, archives and Excel rows all flow into the
    same search_index/embeddings store. Each chunk is labelled with its Evidence
    number and original filename so the model can cite the source instead of
    behaving as if only spreadsheet rows exist.
    """
    ordered: List[str] = []
    seen: set = set()
    names = _evidence_names(conn, case_id)

    def _source_label(evidence_id: Any) -> str:
        try:
            eid = int(evidence_id)
            return f"Evidence #{eid} ({names.get(eid, 'unknown file')})"
        except Exception:
            return "Evidence source unknown"

    def _add(text: str, evidence_id: Any = None, label: str = "evidence text") -> None:
        if not text:
            return
        clean = str(text).strip()
        if not clean:
            return
        prefix = _source_label(evidence_id) + f" [{label}]: " if evidence_id else f"[{label}] "
        item = prefix + clean
        key = re.sub(r"\s+", " ", item[:260].lower())
        if key and key not in seen:
            seen.add(key)
            ordered.append(item)

    excerpt_len = 3600 if k >= 30 else 1800
    try:
        for chunk, _score, ev_id in embeddings.search(conn, case_id, query, k):
            _add(chunk[:excerpt_len], ev_id, "retrieved chunk")
    except Exception:
        pass

    terms = [t for t in re.findall(r"[A-Za-z0-9@._\-]+", query or "") if len(t) >= 2]
    if terms:
        fts = " ".join(f'"{t}"*' for t in terms[:8])
        try:
            rows = conn.execute(
                f"SELECT ref_type, ref_id, substr(content, 1, {excerpt_len}) AS c FROM search_index "
                "WHERE case_id = ? AND search_index MATCH ? LIMIT ?",
                (case_id, fts, k),
            ).fetchall()
            for r in rows:
                _add(r["c"], r["ref_id"] if r["ref_type"] == "evidence" else None, f"FTS {r['ref_type']}")
        except Exception:
            # Some user queries contain FTS special syntax. Fall back to LIKE
            # over extracted/indexed evidence text rather than returning no context.
            try:
                where = " AND ".join("content LIKE ?" for _ in terms[:4])
                rows = conn.execute(
                    f"SELECT ref_type, ref_id, substr(content, 1, {excerpt_len}) AS c FROM search_index "
                    f"WHERE case_id = ? AND {where} LIMIT ?",
                    (case_id, *[f"%{t}%" for t in terms[:4]], k),
                ).fetchall()
                for r in rows:
                    _add(r["c"], r["ref_id"] if r["ref_type"] == "evidence" else None, f"LIKE {r['ref_type']}")
            except Exception:
                pass

    if terms:
        try:
            clauses = []
            params: list[Any] = [case_id]
            for t in terms[:6]:
                clauses.append("(LOWER(value) LIKE ? OR LOWER(type) LIKE ?)")
                like = f"%{t.lower()}%"
                params.extend([like, like])
            rows = conn.execute(
                "SELECT type, value FROM entities WHERE case_id = ? AND (" + " OR ".join(clauses) + ") LIMIT ?",
                (*params, k * 2),
            ).fetchall()
            for r in rows:
                _add(f"{r['type']}: {r['value']}", None, "entity")
        except Exception:
            pass

    if not ordered:
        rows = conn.execute(
            f"SELECT ref_type, ref_id, substr(content, 1, {excerpt_len}) AS c FROM search_index WHERE case_id = ? ORDER BY rowid DESC LIMIT ?",
            (case_id, k),
        ).fetchall()
        for r in rows:
            _add(r["c"], r["ref_id"] if r["ref_type"] == "evidence" else None, f"{r['ref_type']} fallback")
    return ordered[:k]


def _case_ai_digest(conn, case_id: int, query: str, deep: bool = False) -> str:
    """Compact, database-grounded facts for the LLM.

    This makes SMART/DEEP less "dumb": the model receives exact counts,
    high-value entities and representative transaction rows before the fuzzy
    RAG chunks. The deterministic handlers still answer direct financial
    questions first; this digest helps open-ended briefing/explanation tasks.
    """
    q = (query or "").lower()
    max_entities = 220 if deep else 80
    max_txns = 90 if deep else 30
    lines: List[str] = ["DATABASE-GROUNDED CASE FACTS:"]
    try:
        ev = conn.execute("SELECT COUNT(*) AS c FROM evidence WHERE case_id = ?", (case_id,)).fetchone()["c"]
        ent = conn.execute("SELECT COUNT(*) AS c FROM entities WHERE case_id = ?", (case_id,)).fetchone()["c"]
        txn = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS amt FROM transactions WHERE case_id = ?", (case_id,)).fetchone()
        lines.append(f"- Evidence items: {ev}")
        lines.append(f"- Distinct entities stored: {ent}")
        lines.append(f"- Structured transaction rows: {txn['c']} | total amount: ₹{float(txn['amt'] or 0):,.2f}")
    except Exception:
        pass

    try:
        type_rows = conn.execute(
            "SELECT type, COUNT(*) AS c FROM entities WHERE case_id = ? GROUP BY type ORDER BY c DESC LIMIT 25",
            (case_id,),
        ).fetchall()
        if type_rows:
            lines.append("- Entity type counts: " + "; ".join(f"{r['type']}={r['c']}" for r in type_rows))
    except Exception:
        pass

    # Query-specific exact entity matches: useful when asking about a bank, UPI provider,
    # account fragment, handle, email/domain, phone fragment, etc.
    terms = [t for t in re.findall(r"[A-Za-z0-9@._-]{3,}", q) if t not in {"find", "show", "list", "all", "give", "case", "with", "from", "that", "this", "what", "which", "money", "amount", "transaction", "transactions", "account", "accounts", "bank", "banks"}]
    try:
        params: list[Any] = [case_id]
        where = "e.case_id = ?"
        if terms:
            clauses = []
            for t in terms[:6]:
                clauses.append("(LOWER(e.value) LIKE ? OR LOWER(e.type) LIKE ?)")
                like = f"%{t.lower()}%"
                params.extend([like, like])
            where += " AND (" + " OR ".join(clauses) + ")"
        rows = conn.execute(
            "SELECT e.type, e.value, COUNT(l.evidence_id) AS links "
            "FROM entities e LEFT JOIN entity_links l ON l.entity_id = e.id "
            f"WHERE {where} GROUP BY e.id ORDER BY links DESC, e.type LIMIT ?",
            (*params, max_entities),
        ).fetchall()
        if rows:
            lines.append(f"- Entity values available to the model ({len(rows)} shown):")
            for r in rows:
                lines.append(f"    - {r['type']}: {r['value']} (evidence links: {r['links']})")
    except Exception:
        pass

    try:
        txn_where = "t.case_id = ?"
        txn_params: list[Any] = [case_id]
        if terms:
            blobs = []
            for t in terms[:5]:
                blobs.append("LOWER(COALESCE(t.bank,'') || ' ' || COALESCE(t.ifsc,'') || ' ' || COALESCE(t.upi,'') || ' ' || COALESCE(t.utr,'') || ' ' || COALESCE(t.account_no,'') || ' ' || COALESCE(t.sender_account,'') || ' ' || COALESCE(t.receiver_account,'') || ' ' || COALESCE(t.remarks,'')) LIKE ?")
                txn_params.append(f"%{t.lower()}%")
            txn_where += " AND (" + " OR ".join(blobs) + ")"
        rows = conn.execute(
            "SELECT t.layer, t.txn_date, t.utr, t.amount, t.disputed_amount, t.lien_amount, "
            "t.sender_account, t.receiver_account, t.account_no, t.ifsc, t.bank, t.upi, t.status, e.original_name "
            "FROM transactions t LEFT JOIN evidence e ON e.id = t.evidence_id "
            f"WHERE {txn_where} ORDER BY COALESCE(t.amount,0) DESC, t.id LIMIT ?",
            (*txn_params, max_txns),
        ).fetchall()
        if rows:
            lines.append(f"- Representative transaction rows ({len(rows)} shown):")
            for r in rows:
                lines.append(
                    "    - "
                    f"date={r['txn_date'] or 'N/A'} layer={r['layer'] if r['layer'] is not None else 'N/A'} "
                    f"amount={_fmt_money(r['amount'])} disputed={_fmt_money(r['disputed_amount']) if r['disputed_amount'] else 'N/A'} "
                    f"lien={_fmt_money(r['lien_amount']) if r['lien_amount'] else 'N/A'} "
                    f"account={r['receiver_account'] or r['account_no'] or r['sender_account'] or 'N/A'} "
                    f"ifsc={r['ifsc'] or 'N/A'} bank={r['bank'] or 'N/A'} upi={r['upi'] or 'N/A'} utr={r['utr'] or 'N/A'} source={r['original_name'] or 'N/A'}"
                )
    except Exception:
        pass

    try:
        inventory = _evidence_inventory(conn, case_id, limit=120)
        if inventory:
            lines.append("- Evidence inventory across all formats:")
            for item in inventory[:30 if deep else 12]:
                lines.append("    - " + _format_evidence_line(item))
            if len(inventory) > (30 if deep else 12):
                lines.append(f"    - ... {len(inventory) - (30 if deep else 12)} more evidence items in case")

            # Query-aware snippets from non-Excel and Excel evidence alike. This
            # keeps the LLM grounded in PDFs, DOCX/PPTX, screenshots/OCR, emails,
            # logs, HTML, CSV and archive text instead of only transaction rows.
            terms = _query_terms(query)
            selected = []
            for item in inventory:
                hay = " ".join([item.get("name", ""), item.get("kind", ""), item.get("ext", ""), " ".join(item.get("evidence_types") or [])]).lower()
                content_probe = ""
                if terms and not any(t in hay for t in terms):
                    content_probe = _evidence_content(conn, case_id, item["id"], 9000).lower()
                    if not any(t in content_probe for t in terms):
                        continue
                selected.append(item)
                if len(selected) >= (16 if deep else 6):
                    break
            if not selected:
                selected = inventory[:10 if deep else 4]
            if selected:
                lines.append("- Relevant extracted evidence snippets available to the model:")
                for item in selected:
                    content = _evidence_content(conn, case_id, item["id"], 18000 if deep else 9000)
                    excerpt = _best_text_excerpt(content, query, 2400 if deep else 1200)
                    if excerpt:
                        compact = re.sub(r"\s+", " ", excerpt).strip()
                        lines.append(f"    - Evidence #{item['id']} ({item['name']}): {compact[:2400 if deep else 1200]}")
    except Exception:
        pass

    lines.append("Instruction: answer only from these database facts and retrieved chunks from every processed evidence format. Do not invent missing accounts, UTRs, names, amounts, banks, handles, messages, URLs, dates or filenames.")
    return "\n".join(lines)


def _rows(conn, sql: str, args: Tuple[Any, ...]) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _transactions(conn, case_id: int) -> List[Dict[str, Any]]:
    return _rows(
        conn,
        "SELECT t.*, e.original_name FROM transactions t "
        "LEFT JOIN evidence e ON e.id = t.evidence_id "
        "WHERE t.case_id = ? ORDER BY COALESCE(t.layer, 999), t.txn_date, t.id",
        (case_id,),
    )


def _messages(conn, case_id: int) -> List[Dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT m.*, e.original_name FROM communications m "
        "LEFT JOIN evidence e ON e.id = m.evidence_id "
        "WHERE m.case_id = ? ORDER BY COALESCE(m.timestamp, ''), m.id LIMIT 1000",
        (case_id,),
    )
    for m in rows:
        for key in ("risk_flags_json", "urls_json", "amounts_json"):
            try:
                m[key.replace("_json", "")] = json.loads(m.get(key) or "[]")
            except (TypeError, ValueError):
                m[key.replace("_json", "")] = []
    return rows


def _social_profiles(conn, case_id: int) -> List[Dict[str, Any]]:
    return _rows(
        conn,
        "SELECT sp.*, e.original_name FROM social_profiles sp "
        "LEFT JOIN evidence e ON e.id = sp.evidence_id "
        "WHERE sp.case_id = ? ORDER BY sp.platform, sp.username LIMIT 1000",
        (case_id,),
    )


def _technical_indicators(conn, case_id: int) -> List[Dict[str, Any]]:
    return _rows(
        conn,
        "SELECT ti.*, e.original_name FROM technical_indicators ti "
        "LEFT JOIN evidence e ON e.id = ti.evidence_id "
        "WHERE ti.case_id = ? ORDER BY ti.type, ti.value LIMIT 1000",
        (case_id,),
    )


def _image_evidence_records(conn, case_id: int) -> List[Dict[str, Any]]:
    """Return image/screenshot evidence with extracted OCR context."""
    rows = _rows(
        conn,
        "SELECT id, original_name, status, meta_json, intel_json FROM evidence WHERE case_id = ? ORDER BY id DESC LIMIT 200",
        (case_id,),
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            meta = json.loads(r.get("meta_json") or "{}")
        except (TypeError, ValueError):
            meta = {}
        try:
            intel = json.loads(r.get("intel_json") or "{}")
        except (TypeError, ValueError):
            intel = {}
        ext = str(meta.get("ext") or "").lower()
        types = [str(x).lower() for x in (intel.get("evidence_types") or [])]
        is_image = (
            meta.get("kind") == "image"
            or ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
            or any(x in types for x in ("screenshot", "image_document", "qr_code_evidence"))
        )
        if not is_image:
            continue
        content_row = conn.execute(
            "SELECT content FROM search_index WHERE case_id = ? AND ref_type = 'evidence' AND ref_id = ? LIMIT 1",
            (case_id, r["id"]),
        ).fetchone()
        content = str(content_row["content"] if content_row else "")
        stages = _rows(conn, "SELECT stage, state, detail FROM evidence_stages WHERE evidence_id = ? ORDER BY at", (r["id"],))
        out.append({"row": r, "meta": meta, "intel": intel, "content": content, "stages": stages})
    return out


def _excerpt_image_content(content: str) -> Tuple[str, str]:
    """Return (main extracted text, diagnostics) from indexed image content."""
    if not content:
        return "", ""
    diag = ""
    if "[Image processing diagnostics]" in content:
        before, after = content.split("[Image processing diagnostics]", 1)
        content = before.strip()
        diag = after.strip()[:900]
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    useful = [
        ln for ln in lines
        if not ln.startswith("[Image evidence]")
        and not ln.lower().startswith("evidence types:")
        and not ln.lower().startswith("recommended action:")
    ]
    return "\n".join(useful)[:2200], diag


def _evidence_names(conn, case_id: int) -> Dict[int, str]:
    rows = conn.execute("SELECT id, original_name FROM evidence WHERE case_id = ?", (case_id,)).fetchall()
    return {r["id"]: r["original_name"] for r in rows}


def _safe_json_obj(raw: Any) -> Dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _evidence_inventory(conn, case_id: int, limit: int = 400) -> List[Dict[str, Any]]:
    """Return evidence rows with cross-format counts and metadata.

    This is the primary source for AI/context answers that are not spreadsheet-
    specific. It works whether the evidence is Excel, PDF, DOCX, PPTX, EML,
    HTML, TXT/log, archive manifest, or image OCR.
    """
    rows = conn.execute(
        "SELECT e.id, e.original_name, e.mime, e.status, e.progress_percent, e.progress_detail, "
        "e.meta_json, e.intel_json, e.created_at, "
        "(SELECT COUNT(*) FROM entity_links l WHERE l.evidence_id = e.id) AS entity_count, "
        "(SELECT COUNT(*) FROM transactions t WHERE t.evidence_id = e.id) AS transaction_count, "
        "(SELECT COUNT(*) FROM communications m WHERE m.evidence_id = e.id) AS message_count, "
        "(SELECT COUNT(*) FROM social_profiles sp WHERE sp.evidence_id = e.id) AS social_profile_count, "
        "(SELECT COUNT(*) FROM technical_indicators ti WHERE ti.evidence_id = e.id) AS technical_indicator_count, "
        "(SELECT LENGTH(content) FROM search_index si WHERE si.case_id = e.case_id AND si.ref_type = 'evidence' AND si.ref_id = e.id LIMIT 1) AS indexed_chars "
        "FROM evidence e WHERE e.case_id = ? ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
        (case_id, limit),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        meta = _safe_json_obj(r["meta_json"])
        intel = _safe_json_obj(r["intel_json"])
        ext = str(meta.get("original_ext") or meta.get("ext") or "").lower()
        kind = str(meta.get("kind") or "unknown")
        evidence_types = intel.get("evidence_types") or []
        if not isinstance(evidence_types, list):
            evidence_types = []
        out.append({
            "id": int(r["id"]),
            "name": r["original_name"] or f"Evidence #{r['id']}",
            "mime": r["mime"] or meta.get("mime") or "",
            "status": r["status"] or "",
            "progress_percent": r["progress_percent"],
            "progress_detail": r["progress_detail"] or "",
            "kind": kind,
            "ext": ext,
            "evidence_types": [str(x) for x in evidence_types[:8]],
            "entities": int(r["entity_count"] or 0),
            "transactions": int(r["transaction_count"] or 0),
            "messages": int(r["message_count"] or 0),
            "social_profiles": int(r["social_profile_count"] or 0),
            "technical_indicators": int(r["technical_indicator_count"] or 0),
            "indexed_chars": int(r["indexed_chars"] or 0),
            "created_at": r["created_at"] or "",
        })
    return out


def _evidence_content(conn, case_id: int, evidence_id: int, max_chars: int = 4000) -> str:
    row = conn.execute(
        "SELECT content FROM search_index WHERE case_id = ? AND ref_type = 'evidence' AND ref_id = ? LIMIT 1",
        (case_id, evidence_id),
    ).fetchone()
    return str(row["content"] if row else "")[:max_chars]


def _query_terms(query: str) -> List[str]:
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "what", "which", "who", "where",
        "when", "why", "how", "show", "list", "give", "tell", "uploaded", "upload", "evidence",
        "evidences", "file", "files", "document", "documents", "data", "case", "all", "any", "about",
        "summary", "summarize", "summarise", "read", "analyse", "analyze", "explain", "details",
    }
    out = []
    for t in re.findall(r"[A-Za-z0-9@._-]{3,}", query or ""):
        tl = t.lower()
        if tl not in stop and tl not in out:
            out.append(tl)
    return out


def _best_text_excerpt(text: str, query: str, max_chars: int = 1800) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    terms = _query_terms(query)
    if not terms:
        return text[:max_chars]
    lower = text.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) >= 0]
    if not positions:
        return text[:max_chars]
    pos = max(0, min(positions) - max_chars // 3)
    excerpt = text[pos:pos + max_chars]
    if pos > 0:
        excerpt = "…" + excerpt
    if pos + max_chars < len(text):
        excerpt += "…"
    return excerpt


def _format_evidence_line(item: Dict[str, Any]) -> str:
    counts = []
    for key, label in (("entities", "entities"), ("transactions", "txns"), ("messages", "msgs"), ("social_profiles", "profiles"), ("technical_indicators", "tech")):
        if item.get(key):
            counts.append(f"{item[key]} {label}")
    kind = item.get("kind") or "unknown"
    ext = item.get("ext") or ""
    etypes = ", ".join(item.get("evidence_types") or [])
    parts = [f"Evidence #{item['id']} ({item['name']})", f"format={kind}{('/' + ext.lstrip('.')) if ext else ''}", f"status={item.get('status') or 'unknown'}"]
    if etypes:
        parts.append(f"type={etypes}")
    if counts:
        parts.append(", ".join(counts))
    if item.get("indexed_chars"):
        parts.append(f"indexed_text={item['indexed_chars']:,} chars")
    return " | ".join(parts)


def _universal_evidence_answer(conn, case_id: int, query: str) -> Optional[str]:
    """Deterministic cross-format evidence answer for standard mode and fallbacks.

    This prevents the engine from acting Excel-only. For generic questions like
    'what did I upload', 'summarize the PDFs/images/chats', 'answer from this
    evidence', or 'find this value in any file', it reads the unified evidence
    inventory + indexed extracted text, regardless of source format.
    """
    q = (query or "").lower()
    general = bool(re.search(r"\b(evidence|evidences|uploaded|upload|file|files|document|documents|pdf|docx|ppt|image|screenshot|photo|email|eml|html|txt|log|csv|archive|ocr|read|summari[sz]e|analyse|analyze|what.*contains?|what.*inside)\b", q))
    # Do not steal dedicated structured financial/social/technical direct handlers.
    dedicated = bool(re.search(r"\b(money trail|transaction|utr|ifsc|bank account|account timeline|suspicious account|same amount|similar transaction|common upi|common account)\b", q))
    if dedicated and not re.search(r"\b(evidence|file|uploaded|document|pdf|image|screenshot|email|all formats?)\b", q):
        return None
    if not general:
        return None

    items = _evidence_inventory(conn, case_id)
    if not items:
        return "No evidence is uploaded in this case yet."

    terms = _query_terms(query)
    filtered = items
    # Format/name filters, e.g. 'summarize pdf', 'read screenshot', 'show eml'.
    format_words = {"pdf", "doc", "docx", "ppt", "pptx", "excel", "xlsx", "xls", "image", "screenshot", "photo", "email", "eml", "html", "txt", "log", "csv", "archive", "zip"}
    requested_formats = [w for w in format_words if w in q]
    if requested_formats:
        def match_format(item: Dict[str, Any]) -> bool:
            hay = " ".join([item.get("kind", ""), item.get("ext", ""), item.get("mime", ""), item.get("name", ""), " ".join(item.get("evidence_types") or [])]).lower()
            return any(w in hay for w in requested_formats)
        filtered = [it for it in filtered if match_format(it)]

    if terms:
        # Keep files whose name/kind/types match terms OR whose indexed content contains a term.
        matched = []
        for it in filtered:
            hay = " ".join([it.get("name", ""), it.get("kind", ""), it.get("ext", ""), " ".join(it.get("evidence_types") or [])]).lower()
            if any(t in hay for t in terms):
                matched.append(it)
                continue
            sample = _evidence_content(conn, case_id, it["id"], 12000).lower()
            if any(t in sample for t in terms):
                matched.append(it)
        if matched:
            filtered = matched

    if not filtered:
        return "No uploaded evidence matched that file/content filter. The case has evidence, but none matched the requested format or terms."

    wants_summary = bool(re.search(r"\b(summari[sz]e|summary|brief|what.*inside|what.*contains?|read|analyse|analyze|explain)\b", q))
    wants_list_only = bool(re.search(r"\b(list|show all|inventory|what.*uploaded|uploaded files|evidence list)\b", q)) and not wants_summary

    lines: List[str] = []
    if wants_list_only:
        lines.append("Uploaded evidence inventory across all supported formats:")
        for it in filtered[:120]:
            lines.append("- " + _format_evidence_line(it))
        if len(filtered) > 120:
            lines.append(f"... {len(filtered) - 120} more evidence items omitted. Ask for a specific format/name/value to narrow it.")
        return "\n".join(lines)

    lines.append("Cross-format evidence answer from processed OCR/text/entities:")
    for it in filtered[:18]:
        lines.append("- " + _format_evidence_line(it))
        content = _evidence_content(conn, case_id, it["id"], 12000)
        excerpt = _best_text_excerpt(content, query, 1800 if wants_summary else 1200)
        if excerpt:
            clean_lines = [ln.strip() for ln in excerpt.splitlines() if ln.strip()]
            lines.append("  Relevant extracted text:")
            for ln in clean_lines[:10]:
                lines.append(f"    {ln[:260]}")
        else:
            detail = it.get("progress_detail") or "No readable text was indexed; evidence may be binary/unsupported or still processing."
            lines.append(f"  Indexed text: not available. Processing detail: {detail}")
    if len(filtered) > 18:
        lines.append(f"... {len(filtered) - 18} more matching evidence items exist. Ask for a specific filename, value, entity, date, phone, URL, account, or format for a narrower answer.")
    lines.append("Source basis: unified extracted text/OCR, entities and metadata. The answer is not limited to Excel files.")
    return "\n".join(lines)


def _fmt_money(v: Any) -> str:
    if v in (None, ""):
        return "N/A"
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _unique(values: List[Any]) -> List[str]:
    seen, out = set(), []
    for v in values:
        s = str(v or "").strip()
        if s and s.upper() not in seen:
            seen.add(s.upper())
            out.append(s)
    return out


def _compare_ids(conn, case_id: int, query: str) -> Tuple[Optional[int], Optional[int]]:
    nums = [int(x) for x in re.findall(r"\b(?:evidence\s*)?#?(\d+)\b", query, re.I)]
    existing = [r["id"] for r in conn.execute("SELECT id FROM evidence WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()]
    chosen = [n for n in nums if n in existing]
    if len(chosen) >= 2:
        return chosen[0], chosen[1]
    if len(existing) >= 2:
        return existing[0], existing[1]
    return (existing[0], None) if existing else (None, None)


def _similarity_lines(conn, case_id: int, only_pair: Optional[Tuple[int, int]] = None) -> List[str]:
    names = _evidence_names(conn, case_id)
    sql = "SELECT * FROM evidence_similarity WHERE case_id = ? ORDER BY score DESC LIMIT 25"
    args: Tuple[Any, ...] = (case_id,)
    rows = conn.execute(sql, args).fetchall()
    lines: List[str] = []
    for r in rows:
        a, b = r["a_id"], r["b_id"]
        if only_pair and set((a, b)) != set(only_pair):
            continue
        try:
            reasons = json.loads(r["reasons"] or "[]")
        except (TypeError, ValueError):
            reasons = []
        reason_text = []
        for item in reasons[:6]:
            vals = item.get("values")
            if vals:
                reason_text.append(f"{item.get('label', item.get('type'))}: {', '.join(map(str, vals[:6]))}")
            elif "score" in item:
                reason_text.append(f"{item.get('label', item.get('type'))}: {item.get('score')}")
            else:
                reason_text.append(str(item.get("label") or item.get("type") or item))
        lines.append(
            f"- Evidence #{a} ({names.get(a, 'unknown')}) ↔ Evidence #{b} ({names.get(b, 'unknown')}): "
            f"score {round(float(r['score']) * 100, 1)}%, kind `{r['kind']}`"
            + (f"; reasons: {'; '.join(reason_text)}" if reason_text else "")
        )
    return lines


_COMMON_INTENTS: Dict[str, Dict[str, Any]] = {
    "ip": {
        "aliases": ("ip", "ipv4", "ipv6"),
        "entity_types": ("ip", "ipv4", "ipv6"),
        "tech_types": ("ip", "ipv4", "ipv6"),
        "txn_fields": (),
        "label": "IP address",
    },
    "account": {
        "aliases": ("account", "bank account", "beneficiary", "mule account"),
        "entity_types": ("account", "account_number", "bank_account"),
        "tech_types": (),
        "txn_fields": ("account_no", "sender_account", "receiver_account"),
        "label": "bank account",
    },
    "utr": {
        "aliases": ("utr", "transaction id", "transaction reference", "rrn", "reference number"),
        "entity_types": ("utr", "transaction_reference"),
        "tech_types": (),
        "txn_fields": ("utr",),
        "label": "UTR / transaction reference",
    },
    "phone": {
        "aliases": ("phone", "mobile", "number", "whatsapp number"),
        "entity_types": ("phone", "mobile", "whatsapp_number"),
        "tech_types": (),
        "txn_fields": (),
        "label": "phone / WhatsApp number",
    },
    "email": {
        "aliases": ("email", "mail"),
        "entity_types": ("email",),
        "tech_types": (),
        "txn_fields": (),
        "label": "email",
    },
    "upi": {
        "aliases": ("upi", "upi id", "vpa"),
        "entity_types": ("upi",),
        "tech_types": (),
        "txn_fields": ("upi",),
        "label": "UPI ID",
    },
    "ifsc": {
        "aliases": ("ifsc",),
        "entity_types": ("ifsc", "IFSC"),
        "tech_types": (),
        "txn_fields": ("ifsc",),
        "label": "IFSC",
    },
    "bank": {
        "aliases": ("bank", "branch"),
        "entity_types": ("bank", "branch"),
        "tech_types": (),
        "txn_fields": ("bank",),
        "label": "bank / branch",
    },
    "url": {
        "aliases": ("url", "link", "website", "short link"),
        "entity_types": ("url", "website_url", "short_link"),
        "tech_types": ("url", "website_url", "short_link"),
        "txn_fields": (),
        "label": "URL / link",
    },
    "domain": {
        "aliases": ("domain", "website domain"),
        "entity_types": ("domain",),
        "tech_types": ("domain",),
        "txn_fields": (),
        "label": "domain",
    },
    "handle": {
        "aliases": ("handle", "username", "instagram", "telegram", "social"),
        "entity_types": ("social_handle", "username", "instagram_username", "telegram_username", "telegram_group", "telegram_channel", "instagram_profile", "facebook_profile", "whatsapp_number"),
        "tech_types": (),
        "txn_fields": (),
        "label": "social handle / username",
    },
    "device": {
        "aliases": ("device", "device id", "android", "android id", "imei", "imsi", "iccid", "mac"),
        "entity_types": ("device_id", "android_id", "imei", "imsi", "iccid", "mac"),
        "tech_types": ("device_id", "android_id", "imei", "imsi", "iccid", "mac"),
        "txn_fields": (),
        "label": "device indicator",
    },
    "qr": {
        "aliases": ("qr", "qr code", "qr payload"),
        "entity_types": ("qr_payload",),
        "tech_types": ("qr_payload",),
        "txn_fields": (),
        "label": "QR payload",
    },
    "crypto": {
        "aliases": ("crypto", "wallet address", "bitcoin", "ethereum"),
        "entity_types": ("crypto_wallet",),
        "tech_types": ("crypto_wallet",),
        "txn_fields": (),
        "label": "crypto wallet",
    },
}


def _norm_for_compare(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    return re.sub(r"\s+", "", s).upper()


def _query_common_kinds(q: str) -> List[str]:
    kinds: List[str] = []
    for kind, spec in _COMMON_INTENTS.items():
        if any(alias in q for alias in spec["aliases"]):
            kinds.append(kind)
    if not kinds and re.search(r"\b(entity|entities|object|objects|value|values|indicator|indicators)\b", q):
        kinds = list(_COMMON_INTENTS.keys())
    return kinds


def _explicit_pair_requested(q: str) -> bool:
    """True only when the user asks for a specific two-evidence comparison."""
    nums = re.findall(r"\b(?:evidence\s*)?#?(\d+)\b", q, re.I)
    return len(nums) >= 2 or bool(re.search(r"\b(compare|between)\b.*\b(evidence|file|document|screenshot)s?\b", q))


def _add_common_hit(bucket: Dict[str, Dict[str, Any]], label: str, value: Any, evidence_id: Any, evidence_name: Any, source: str) -> None:
    val = str(value or "").strip()
    norm = _norm_for_compare(val)
    if not norm or len(norm) < 2:
        return
    key = f"{label}:{norm}"
    item = bucket.setdefault(key, {"label": label, "value": val, "evidence": {}, "sources": set()})
    # Prefer the first human-readable original value, but keep normalized grouping stable.
    if len(str(item.get("value") or "")) < len(val):
        item["value"] = val
    if evidence_id is not None:
        item["evidence"][int(evidence_id)] = str(evidence_name or f"Evidence #{evidence_id}")
    item["sources"].add(source)


def _common_values_answer(
    conn,
    case_id: int,
    query: str,
    txns: List[Dict[str, Any]],
    msgs: List[Dict[str, Any]],
    profiles: List[Dict[str, Any]],
    tech: List[Dict[str, Any]],
) -> Optional[str]:
    """Answer case-wide 'common/shared/find repeated X' queries without forcing two file selection."""
    q = (query or "").lower()
    wants_common = bool(re.search(r"\b(common|shared|matching|same|repeated|repeat|duplicate|linked|correlat)\b", q))
    kinds = _query_common_kinds(q)
    if not wants_common or not kinds:
        return None

    specs = [_COMMON_INTENTS[k] for k in kinds]
    wanted_entity_types = {t.lower() for spec in specs for t in spec.get("entity_types", ())}
    wanted_tech_types = {t.lower() for spec in specs for t in spec.get("tech_types", ())}
    bucket: Dict[str, Dict[str, Any]] = {}

    # Structured transaction fields: account/UTR/IFSC/bank/UPI and related financial values.
    for t in txns:
        ev_id = t.get("evidence_id")
        ev_name = t.get("original_name") or t.get("source_file")
        for spec in specs:
            for field in spec.get("txn_fields", ()):
                _add_common_hit(bucket, spec["label"], t.get(field), ev_id, ev_name, f"transaction.{field}")

    # Technical indicator table: URLs, IPs, device IDs, QR payloads, crypto wallets, etc.
    for i in tech:
        typ = str(i.get("type") or "").lower()
        if any(w == typ or w in typ for w in wanted_tech_types):
            _add_common_hit(bucket, i.get("type") or "technical indicator", i.get("value"), i.get("evidence_id"), i.get("original_name"), "technical_indicators")

    # Social profiles and communication handles/URLs.
    if "handle" in kinds:
        for p in profiles:
            label = (p.get("platform") or "social handle").strip() or "social handle"
            _add_common_hit(bucket, label, p.get("username"), p.get("evidence_id"), p.get("original_name"), "social_profiles.username")
            _add_common_hit(bucket, "social profile URL", p.get("profile_url"), p.get("evidence_id"), p.get("original_name"), "social_profiles.profile_url")
        for m in msgs:
            _add_common_hit(bucket, "message sender", m.get("sender_handle") or m.get("sender"), m.get("evidence_id"), m.get("original_name"), "communications.sender")
            _add_common_hit(bucket, "message receiver", m.get("receiver_handle") or m.get("receiver"), m.get("evidence_id"), m.get("original_name"), "communications.receiver")
    if "url" in kinds or "domain" in kinds:
        for m in msgs:
            for u in m.get("urls") or []:
                _add_common_hit(bucket, "URL / link", u, m.get("evidence_id"), m.get("original_name"), "communications.urls")

    # Generic entity table is the broadest fallback and covers phones/emails/social handles found by any parser.
    if wanted_entity_types:
        placeholders = ",".join("?" for _ in wanted_entity_types)
        try:
            rows = conn.execute(
                "SELECT en.type, en.value, l.evidence_id, ev.original_name "
                "FROM entities en "
                "JOIN entity_links l ON l.entity_id = en.id "
                "LEFT JOIN evidence ev ON ev.id = l.evidence_id "
                f"WHERE en.case_id = ? AND lower(en.type) IN ({placeholders})",
                (case_id, *sorted(wanted_entity_types)),
            ).fetchall()
            for r in rows:
                _add_common_hit(bucket, r["type"], r["value"], r["evidence_id"], r["original_name"], "entities")
        except Exception:
            pass

    items = list(bucket.values())
    common = [x for x in items if len(x["evidence"]) >= 2]
    common.sort(key=lambda x: (-len(x["evidence"]), x["label"], str(x["value"])))

    kind_labels = ", ".join(_COMMON_INTENTS[k]["label"] for k in kinds[:5])
    if common:
        lines = [f"Common/shared {kind_labels} across processed evidence:"]
        for item in common[:40]:
            evs = [f"Evidence #{eid} ({name})" for eid, name in sorted(item["evidence"].items())]
            lines.append(
                f"- {item['label']}: `{item['value']}` appears in {len(item['evidence'])} evidence item(s): "
                + "; ".join(evs[:6])
                + (f" | sources: {', '.join(sorted(item['sources']))}" if item.get("sources") else "")
            )
        return "\n".join(lines)

    # Do not ask for two files. Give a useful case-wide status and show what exists if any.
    all_items = [x for x in items if x["evidence"]]
    all_items.sort(key=lambda x: (x["label"], str(x["value"])))
    if all_items:
        lines = [
            f"No common/shared {kind_labels} was found across multiple evidence files yet.",
            "Extracted values currently stored in this case:",
        ]
        for item in all_items[:40]:
            evs = [f"Evidence #{eid} ({name})" for eid, name in sorted(item["evidence"].items())]
            lines.append(f"- {item['label']}: `{item['value']}` — {'; '.join(evs[:4])}")
        return "\n".join(lines)

    return f"No stored {kind_labels} values were found in the processed evidence yet. Upload/process evidence containing those indicators, then ask again."


def _txn_blob(t: Dict[str, Any]) -> str:
    fields = (
        "bank", "ifsc", "upi", "utr", "account_no", "sender_account", "receiver_account",
        "status", "remarks", "source_file", "original_name", "source_ref",
    )
    return " ".join(str(t.get(f) or "") for f in fields).lower()


def _specific_filter_terms(q: str) -> List[str]:
    stop = {
        "find", "show", "list", "all", "the", "a", "an", "and", "or", "with", "for", "from", "in", "of",
        "bank", "banks", "account", "accounts", "money", "amount", "transaction", "transactions", "details",
        "give", "me", "get", "please", "related", "matching", "similar", "common", "shared", "repeated",
    }
    terms = []
    for token in re.findall(r"[A-Za-z0-9@._-]{3,}", q.lower()):
        if token not in stop and not token.isdigit():
            terms.append(token)
    # Common bank/UPI provider abbreviations are important even if short.
    for short in ("hdfc", "sbi", "pnb", "bob", "axis", "ybl", "ibl", "axl", "okhdfc", "okhdfcbank"):
        if short in q.lower() and short not in terms:
            terms.append(short)
    return _unique(terms)


def _financial_filter_answer(txns: List[Dict[str, Any]], query: str) -> Optional[str]:
    """Answer direct case-wide financial filters like 'find all HDFC bank account and money'.

    These are not 'common across two evidence' questions. They should return the
    matching transaction/account/amount rows from the structured transaction
    table, even when there is only one uploaded workbook in the case.
    """
    q = (query or "").lower()
    if not txns:
        return None
    asks_finance = any(w in q for w in ("account", "bank", "ifsc", "money", "amount", "transaction", "utr", "upi"))
    asks_direct = any(w in q for w in ("find", "show", "list", "all", "get", "give"))
    if not asks_finance or not asks_direct:
        return None
    # Let dedicated common/repeated/similar handlers handle those intents.
    if re.search(r"\b(common|shared|same|repeated|repeat|duplicate|similar)\b", q):
        return None

    terms = _specific_filter_terms(q)
    filtered = txns
    if terms:
        filtered = [t for t in txns if all(term in _txn_blob(t) for term in terms)]
        if not filtered:
            # If the user supplies multiple words, a bank/UPI provider may only
            # require any one of them (e.g. hdfc bank account money).
            filtered = [t for t in txns if any(term in _txn_blob(t) for term in terms)]
    if not filtered:
        return "No structured transactions matched that financial filter. Try a bank name, IFSC, account number, UPI provider, UTR, or ask `show money trail`."

    total = sum(float(t.get("amount") or 0) for t in filtered)
    disputed = sum(float(t.get("disputed_amount") or 0) for t in filtered)
    lien = sum(float(t.get("lien_amount") or 0) for t in filtered)

    by_account: Dict[str, Dict[str, Any]] = {}
    for t in filtered:
        account = str(t.get("receiver_account") or t.get("account_no") or t.get("sender_account") or "UNKNOWN").strip()
        item = by_account.setdefault(account, {"rows": 0, "amount": 0.0, "disputed": 0.0, "lien": 0.0, "banks": set(), "ifscs": set(), "utrs": [], "evidence": set()})
        item["rows"] += 1
        item["amount"] += float(t.get("amount") or 0)
        item["disputed"] += float(t.get("disputed_amount") or 0)
        item["lien"] += float(t.get("lien_amount") or 0)
        if t.get("bank"): item["banks"].add(str(t.get("bank")))
        if t.get("ifsc"): item["ifscs"].add(str(t.get("ifsc")))
        if t.get("utr") and len(item["utrs"]) < 5: item["utrs"].append(str(t.get("utr")))
        item["evidence"].add(f"Evidence #{t.get('evidence_id')} ({t.get('original_name') or t.get('source_file')})")

    title_filter = (" for filter: " + ", ".join(terms)) if terms else ""
    lines = [
        f"Financial transactions/accounts{title_filter}:",
        f"- Matching transaction rows: {len(filtered)} of {len(txns)} stored rows",
        f"- Total amount: {_fmt_money(total)}",
        f"- Total disputed amount: {_fmt_money(disputed) if disputed else 'N/A'}",
        f"- Total lien/hold/frozen amount: {_fmt_money(lien) if lien else 'N/A'}",
        "Accounts / money summary:",
    ]
    ranked = sorted(by_account.items(), key=lambda kv: (-kv[1]["amount"], kv[0]))
    for account, item in ranked[:80]:
        lines.append(
            f"- Account `{account}` | rows {item['rows']} | amount {_fmt_money(item['amount'])}"
            + (f" | disputed {_fmt_money(item['disputed'])}" if item["disputed"] else "")
            + (f" | lien/hold {_fmt_money(item['lien'])}" if item["lien"] else "")
            + (f" | bank(s): {', '.join(sorted(item['banks'])[:4])}" if item["banks"] else "")
            + (f" | IFSC(s): {', '.join(sorted(item['ifscs'])[:4])}" if item["ifscs"] else "")
            + (f" | UTR sample: {', '.join(item['utrs'][:3])}" if item["utrs"] else "")
        )
    if len(ranked) > 80:
        lines.append(f"... {len(ranked) - 80} more account groups omitted from chat. Use JSON/Excel export or a narrower filter.")
    return "\n".join(lines)


def _similar_transactions_answer(txns: List[Dict[str, Any]], query: str) -> Optional[str]:
    q = (query or "").lower()
    if "similar" not in q or "transaction" not in q:
        return None
    if not txns:
        return "No structured transactions are stored yet, so similar transaction grouping cannot be built."

    groups: List[Tuple[str, str, List[Dict[str, Any]]]] = []
    for label, key_fn in (
        ("same UTR", lambda t: str(t.get("utr") or "").upper()),
        ("same receiver/account", lambda t: str(t.get("receiver_account") or t.get("account_no") or "").upper()),
        ("same UPI", lambda t: str(t.get("upi") or "").lower()),
        ("same IFSC", lambda t: str(t.get("ifsc") or "").upper()),
        ("same amount", lambda t: str(t.get("amount") or "")),
    ):
        bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in txns:
            key = key_fn(t).strip()
            if key and key not in {"0", "0.0", "N/A", "NONE"}:
                bucket[key].append(t)
        for key, rows in bucket.items():
            if len(rows) >= 2:
                groups.append((label, key, rows))

    if not groups:
        return "No similar transaction groups were found inside the structured transaction table. The workbook has transactions, but no repeated UTR/account/UPI/IFSC/amount groups crossed the current thresholds."

    priority = {"same UTR": 0, "same receiver/account": 1, "same UPI": 2, "same IFSC": 3, "same amount": 9}
    groups.sort(key=lambda g: (priority.get(g[0], 5), -len(g[2]), g[1]))
    lines = ["Similar transaction groups from the processed transaction table:"]
    for label, key, rows in groups[:40]:
        total = sum(float(t.get("amount") or 0) for t in rows)
        examples = []
        for t in rows[:5]:
            examples.append(
                f"Evidence #{t.get('evidence_id')} {t.get('source_ref') or ''} | account `{t.get('receiver_account') or t.get('account_no') or 'N/A'}` | UTR `{t.get('utr') or 'N/A'}` | amount {_fmt_money(t.get('amount'))} | bank {t.get('bank') or 'N/A'}"
            )
        lines.append(f"- {label}: `{key}` appears in {len(rows)} transaction row(s), total {_fmt_money(total)}")
        for ex in examples:
            lines.append(f"  - {ex}")
    if len(groups) > 40:
        lines.append(f"... {len(groups) - 40} more groups omitted. Ask for a specific account, UTR, bank, IFSC, or amount to narrow it.")
    return "\n".join(lines)


def _structured_answer(conn, case_id: int, query: str) -> Optional[str]:
    """Return deterministic structured answer for known investigation intents."""
    q = (query or "").lower()
    txns = _transactions(conn, case_id)
    msgs = _messages(conn, case_id)
    profiles = _social_profiles(conn, case_id)
    tech = _technical_indicators(conn, case_id)

    investigator_financial = account_intel_service.investigator_question_answer(txns, query)
    if investigator_financial is not None:
        return investigator_financial

    similar_txns = _similar_transactions_answer(txns, query)
    if similar_txns is not None:
        return similar_txns

    financial_filter = _financial_filter_answer(txns, query)
    if financial_filter is not None:
        return financial_filter

    common_answer = _common_values_answer(conn, case_id, query, txns, msgs, profiles, tech)
    if common_answer is not None and not _explicit_pair_requested(q):
        return common_answer

    if any(word in q for word in ("screenshot", "image", "photo", "picture", "visual", "ocr", "scan this", "read this")):
        images = _image_evidence_records(conn, case_id)
        if not images:
            return "No image/screenshot evidence is stored in this case yet."
        lines = ["Image / screenshot evidence analysis from processed OCR text:"]
        for item in images[:12]:
            r = item["row"]
            extracted, diag = _excerpt_image_content(item.get("content") or "")
            types = ", ".join(item.get("intel", {}).get("evidence_types") or []) or item.get("meta", {}).get("kind", "image")
            lines.append(f"- Evidence #{r['id']} ({r.get('original_name')}) | status: {r.get('status')} | type: {types}")
            if extracted:
                lines.append("  Extracted content:")
                for ln in extracted.splitlines()[:18]:
                    lines.append(f"    {ln[:220]}")
            else:
                lines.append("  No readable OCR text was produced for this image.")
            if diag:
                lines.append("  Processing diagnostics:")
                for ln in diag.splitlines()[:6]:
                    lines.append(f"    {ln[:220]}")
            stage_errors = [st for st in item.get("stages") or [] if st.get("state") == "error"]
            if stage_errors:
                lines.append("  Stage errors: " + "; ".join(f"{st.get('stage')}: {st.get('detail')}" for st in stage_errors[:3]))
        lines.append("Source: processed evidence OCR output and image metadata; no raw image is re-scanned inside AI chat.")
        return "\n".join(lines)

    universal_answer = _universal_evidence_answer(conn, case_id, query)
    if universal_answer is not None:
        return universal_answer

    if re.search(r"\b(utr|transaction id|rrn|reference)\b", q) and re.search(r"\b(list|show|all|which)\b", q):
        utrs = _unique([t.get("utr") for t in txns])
        if not utrs:
            return "No UTR / transaction reference values are stored yet. Upload/process financial evidence first."
        lines = ["Stored UTR / transaction references:"]
        for utr in utrs:
            sources = _unique([f"Evidence #{t['evidence_id']} ({t.get('original_name') or t.get('source_file')}) {t.get('source_ref') or ''}" for t in txns if str(t.get("utr") or "").upper() == utr.upper()])
            lines.append(f"- `{utr}` — source: {'; '.join(sources[:4])}")
        return "\n".join(lines)

    if "money trail" in q or ("trail" in q and ("account" in q or "transaction" in q)) or "layer" in q:
        if not txns:
            return "No structured transactions are stored yet, so a money trail cannot be built."
        lines = ["Structured money trail / transaction chronology:"]
        for t in txns:
            left = t.get("sender_account") or "Participant/source account"
            right = t.get("receiver_account") or t.get("account_no") or "Beneficiary/account"
            layer = f"Layer {t['layer']}" if t.get("layer") is not None else "Layer N/A"
            lines.append(
                f"- {layer}: `{left}` → `{right}` | UTR `{t.get('utr') or 'N/A'}` | "
                f"Amount {_fmt_money(t.get('amount'))} | IFSC `{t.get('ifsc') or 'N/A'}` | "
                f"Bank {t.get('bank') or 'N/A'} | Date {t.get('txn_date') or 'N/A'} | "
                f"Source Evidence #{t['evidence_id']} ({t.get('original_name') or t.get('source_file')}) {t.get('source_ref') or ''}"
            )
        return "\n".join(lines)

    if "repeated" in q or "duplicate" in q or "repeat" in q or "mule" in q:
        values = []
        for t in txns:
            for field in ("account_no", "receiver_account", "sender_account", "utr", "upi"):
                if t.get(field):
                    values.append((field, str(t[field]).upper()))
        counts = Counter(values)
        repeated = [(field, value, c) for (field, value), c in counts.items() if c > 1]
        lines = []
        if repeated:
            lines.append("Repeated structured financial values:")
            for field, value, count in repeated[:25]:
                lines.append(f"- {field}: `{value}` appears {count} times")
        sim = _similarity_lines(conn, case_id)
        if sim:
            lines.append("Similar/linked evidence:")
            lines.extend(sim[:10])
        return "\n".join(lines) if lines else "No repeated accounts/UTRs/UPIs or similarity links are stored yet."

    if "similar" in q or "compare" in q or "related evidence" in q or "linked evidence" in q or ("common" in q and _explicit_pair_requested(q)):
        pair = _compare_ids(conn, case_id, query) if "compare" in q or _explicit_pair_requested(q) else None
        lines = _similarity_lines(conn, case_id, pair if pair and pair[1] else None)
        if lines:
            return "Evidence similarity / comparison result:\n" + "\n".join(lines)
        if pair and pair[0] and pair[1]:
            return f"No stored similarity edge exists yet between Evidence #{pair[0]} and Evidence #{pair[1]}. Processing may still be pending or they share no strong signals."
        return "No similar evidence links are stored yet. I can still answer case-wide common-value questions like `find common IP`, `find common account`, `find common UPI`, or `find common phone` from the processed evidence."

    if "message" in q or "chat" in q or "communication" in q or "payment request" in q or "otp" in q or "threat" in q:
        if not msgs:
            return "No structured chat/message records are stored yet. Upload WhatsApp/Telegram/Instagram/email text or screenshot OCR evidence first."
        lines = ["Structured communication/message records:"]
        filtered = msgs
        if "payment" in q:
            filtered = [m for m in msgs if "payment_request" in (m.get("risk_flags") or [])]
        elif "otp" in q:
            filtered = [m for m in msgs if "otp_request" in (m.get("risk_flags") or [])]
        elif "threat" in q or "blackmail" in q:
            filtered = [m for m in msgs if set(m.get("risk_flags") or []) & {"threat", "blackmail", "sextortion"}]
        for m in filtered[:40]:
            flags = ", ".join(m.get("risk_flags") or []) or "none"
            text = str(m.get("message_text") or "").replace("\n", " ")[:220]
            lines.append(
                f"- {m.get('timestamp') or 'undated'} | {m.get('platform') or 'message'} | "
                f"{m.get('sender') or 'unknown'}: {text} | flags: {flags} | "
                f"Source Evidence #{m['evidence_id']} ({m.get('original_name')}) {m.get('source_ref') or ''}"
            )
        return "\n".join(lines) if len(lines) > 1 else "No matching message records found for that filter."

    if "instagram" in q or "telegram" in q or "whatsapp" in q or "social" in q or "profile" in q or "username" in q or "handle" in q:
        if not profiles:
            return "No structured social profile/handle records are stored yet. Upload social screenshots/chat exports first."
        lines = ["Extracted social profiles / handles:"]
        for p in profiles[:80]:
            if "instagram" in q and str(p.get("platform") or "").lower() != "instagram":
                continue
            if "telegram" in q and str(p.get("platform") or "").lower() != "telegram":
                continue
            if "whatsapp" in q and str(p.get("platform") or "").lower() != "whatsapp":
                continue
            lines.append(
                f"- {p.get('platform') or 'social'}: `{p.get('username') or 'N/A'}` | "
                f"URL {p.get('profile_url') or 'N/A'} | Source Evidence #{p['evidence_id']} ({p.get('original_name')})"
            )
        return "\n".join(lines) if len(lines) > 1 else "No matching social handles found for that platform."

    if "url" in q or "domain" in q or "ip" in q or "imei" in q or "imsi" in q or "qr" in q or "technical" in q or "indicator" in q or "crypto" in q:
        if not tech:
            return "No technical/forensic indicators are stored yet. Upload URL/domain/device/IP/QR/code/log evidence first."
        lines = ["Technical / forensic indicators:"]
        wanted = None
        for key in ("url", "domain", "ip", "imei", "imsi", "qr", "crypto", "mac", "device"):
            if key in q:
                wanted = key
                break
        for i in tech[:120]:
            typ = str(i.get("type") or "").lower()
            val = str(i.get("value") or "")
            if wanted and wanted not in typ and wanted not in val.lower():
                continue
            lines.append(f"- {i.get('type')}: `{val}` | Source Evidence #{i['evidence_id']} ({i.get('original_name')}) {i.get('source_ref') or ''}")
        return "\n".join(lines) if len(lines) > 1 else "No matching technical indicators found for that filter."

    if "bank" in q or "ifsc" in q:
        banks = _unique([t.get("bank") for t in txns])
        ifscs = _unique([t.get("ifsc") for t in txns])
        lines = ["Banks / IFSCs involved:"]
        if banks:
            lines.append("- Banks: " + ", ".join(banks))
        if ifscs:
            lines.append("- IFSCs: " + ", ".join(f"`{x}`" for x in ifscs))
        return "\n".join(lines) if len(lines) > 1 else "No bank or IFSC values are stored yet."

    if "lead" in q or "next step" in q or "next action" in q or "reviewer" in q:
        leads: List[str] = []
        if txns:
            accounts = _unique([t.get("account_no") or t.get("receiver_account") for t in txns])
            if accounts:
                leads.append("Send/verify bank notices for beneficiary/mule account(s): " + ", ".join(accounts[:12]))
            utrs = _unique([t.get("utr") for t in txns])
            if utrs:
                leads.append("Use UTR(s) for bank/API reconciliation: " + ", ".join(utrs[:12]))
            ifscs = _unique([t.get("ifsc") for t in txns])
            if ifscs:
                leads.append("Map IFSC(s) to branch nodal contacts: " + ", ".join(ifscs[:12]))
        if msgs:
            risky = [m for m in msgs if m.get("risk_flags")]
            leads.append(f"Review {len(risky) or len(msgs)} message(s) with risk flags/payment/OTP/link context.")
        if profiles:
            handles = _unique([p.get("username") for p in profiles])
            leads.append("Verify social handle(s): " + ", ".join(handles[:12]))
        if tech:
            indicators = _unique([f"{i.get('type')}:{i.get('value')}" for i in tech])
            leads.append("Correlate technical indicator(s): " + ", ".join(indicators[:10]))
        for line in _similarity_lines(conn, case_id)[:5]:
            leads.append("Review linked evidence: " + line.lstrip("- "))
        if not leads:
            leads.append("Upload/process financial evidence, then verify extracted entities before generating notices.")
        return "Important investigation leads / next actions:\n" + "\n".join(f"- {x}" for x in leads)

    if "summar" in q or "brief" in q or "reference" in q:
        ev = conn.execute("SELECT COUNT(*) AS c FROM evidence WHERE case_id = ?", (case_id,)).fetchone()["c"]
        ent = conn.execute("SELECT COUNT(*) AS c FROM entities WHERE case_id = ?", (case_id,)).fetchone()["c"]
        total = sum(float(t.get("amount") or 0) for t in txns)
        banks = _unique([t.get("bank") for t in txns])
        utrs = _unique([t.get("utr") for t in txns])
        platforms = _unique([p.get("platform") for p in profiles])
        tech_types = _unique([i.get("type") for i in tech])
        return "\n".join([
            "Investigation summary from processed evidence:",
            f"- Evidence items: {ev}",
            f"- Distinct entities: {ent}",
            f"- Structured transaction rows: {len(txns)}",
            f"- Message/communication records: {len(msgs)}",
            f"- Social profiles/handles: {len(profiles)}" + (f" ({', '.join(platforms[:8])})" if platforms else ""),
            f"- Technical indicators: {len(tech)}" + (f" ({', '.join(tech_types[:8])})" if tech_types else ""),
            f"- Total detected transaction amount: {_fmt_money(total) if total else 'N/A'}",
            f"- UTRs: {', '.join(utrs[:12]) if utrs else 'N/A'}",
            f"- Banks: {', '.join(banks[:12]) if banks else 'N/A'}",
            f"- Similar evidence links: {len(_similarity_lines(conn, case_id))}",
        ])

    return None


def _standard_answer(conn, case_id: int, query: str) -> str:
    """Deterministic summary from the knowledge store - no LLM."""
    ev = conn.execute("SELECT COUNT(*) AS c FROM evidence WHERE case_id = ?", (case_id,)).fetchone()["c"]
    ent = conn.execute("SELECT COUNT(*) AS c FROM entities WHERE case_id = ?", (case_id,)).fetchone()["c"]
    txn = conn.execute("SELECT COUNT(*) AS c FROM transactions WHERE case_id = ?", (case_id,)).fetchone()["c"]
    msg = conn.execute("SELECT COUNT(*) AS c FROM communications WHERE case_id = ?", (case_id,)).fetchone()["c"]
    social = conn.execute("SELECT COUNT(*) AS c FROM social_profiles WHERE case_id = ?", (case_id,)).fetchone()["c"]
    tech = conn.execute("SELECT COUNT(*) AS c FROM technical_indicators WHERE case_id = ?", (case_id,)).fetchone()["c"]
    top = conn.execute(
        "SELECT e.type, e.value, COUNT(l.evidence_id) AS links "
        "FROM entities e JOIN entity_links l ON l.entity_id = e.id "
        "WHERE e.case_id = ? GROUP BY e.id HAVING links > 1 "
        "ORDER BY links DESC LIMIT 8",
        (case_id,),
    ).fetchall()
    lines = [
        f"Standard mode summary for query: {query!r}",
        f"- Evidence items: {ev}",
        f"- Structured transaction rows: {txn}",
        f"- Communication/message records: {msg}",
        f"- Social profiles/handles: {social}",
        f"- Technical indicators: {tech}",
        f"- Distinct entities: {ent}",
    ]
    if top:
        lines.append("- Top cross-referenced entities:")
        for r in top:
            lines.append(f"    - {r['type']}: {r['value']} (in {r['links']} items)")
    else:
        lines.append("- No entities appear in more than one evidence item yet.")
    lines.append("Try: 'timeline for account 123...', 'flag suspicious accounts', 'same account same amount short duration', 'list all UTRs', 'show money trail', 'show Instagram handles', or 'generate next steps'.")
    return "\n".join(lines)


def ask(
    case_uid: str,
    query: str,
    mode: Optional[str] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Answer a query in the case's (or overridden) AI mode. Persists the turn."""
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return {"error": "case not found"}

        if mode is None:
            row = conn.execute("SELECT ai_mode FROM cases WHERE id = ?", (case_id,)).fetchone()
            mode = row["ai_mode"] if row else "standard"
        mode = mode if mode in {"standard", "smart", "deep"} else "standard"
        requested_provider = _selected_provider(provider)
        used_provider = "deterministic"
        warning = None

        _safe_chat_insert(conn, case_id, "user", query)

        structured = _structured_answer(conn, case_id, query)
        used_mode = "structured"
        if structured is not None:
            answer = structured
        elif mode == "standard":
            used_mode = "standard"
            answer = _standard_answer(conn, case_id, query)
        else:
            adapter = _provider_adapter(requested_provider)
            available, detail = adapter.is_available()
            if not available:
                used_mode = "standard (fallback)"
                answer = _standard_answer(conn, case_id, query)
                warning = f"{requested_provider.title()} AI unavailable: {detail}. Used deterministic analysis."
            else:
                used_mode = mode
                used_provider = requested_provider
                k = _DEEP_K if mode == "deep" else _SMART_K
                chunks = [_case_ai_digest(conn, case_id, query, deep=(mode == "deep"))]
                chunks.extend(_retrieve(conn, case_id, query, k))
                reply = adapter.generate(query, context=chunks, case=case_uid)
                if reply is None:
                    used_mode = "standard (fallback)"
                    used_provider = "deterministic"
                    answer = _standard_answer(conn, case_id, query)
                    warning = (
                        f"{requested_provider.title()} AI did not return an answer. "
                        "Used deterministic analysis."
                    )
                else:
                    answer = reply

        _safe_chat_insert(conn, case_id, "assistant", answer)
        audit.record(
            "ai.ask",
            case=case_uid,
            mode=used_mode,
            provider=used_provider,
        )
        result = {
            "mode": used_mode,
            "provider": used_provider,
            "requested_provider": requested_provider,
            "answer": answer,
        }
        if warning:
            result["warning"] = warning
        return result
    finally:
        conn.close()


def clear_history(case_uid: str) -> Dict[str, Any]:
    """Delete all AI chat turns for a case. Returns the number removed."""
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return {"error": "case not found"}
        def _delete_history():
            cur = conn.execute("DELETE FROM ai_chats WHERE case_id = ?", (case_id,))
            conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        removed = run_with_db_retry(_delete_history, attempts=8, base_sleep=0.2)
    finally:
        conn.close()
    audit.record("ai.clear_history", case=case_uid, removed=removed)
    return {"ok": True, "removed": removed}


def history(case_uid: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        case_id = _case_id(conn, case_uid)
        if case_id is None:
            return []
        rows = conn.execute(
            "SELECT role, content, created_at FROM ai_chats WHERE case_id = ? ORDER BY id DESC LIMIT ?",
            (case_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in reversed(rows)]
