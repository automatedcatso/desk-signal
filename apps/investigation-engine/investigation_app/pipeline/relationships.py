"""Knowledge-graph relationships + duplicate/similarity analysis.

Fast, bounded relationship building for offline investigations. Exact/high-value
financial links remain prioritised; expensive all-vs-all graph work is capped so
large Excel uploads do not stall evidence processing.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Tuple

from investigation_app.pipeline import embeddings

_NEAR_DUP = 0.92
_SIMILAR = 0.60
_HIGH_VALUE_TYPES = {"utr", "account", "account_number", "upi", "phone", "email", "ifsc", "bank", "social_handle", "instagram_username", "telegram_username", "whatsapp_number", "url", "website_url", "domain", "qr_payload", "crypto_wallet", "ip", "ipv4", "ipv6", "mac", "imei", "imsi", "iccid", "device_id", "android_id"}
_GRAPH_TYPES_PRIORITY = {
    "utr": 1, "account": 2, "account_number": 2, "upi": 3, "ifsc": 4,
    "phone": 5, "email": 6, "bank": 7, "url": 8, "domain": 9, "ipv4": 10,
    "ip": 10, "social_handle": 11, "instagram_username": 11, "telegram_username": 11,
}
_MAX_GRAPH_ENTITIES_PER_EVIDENCE = 180
_MAX_DUP_CANDIDATES = 80


def link_entities(conn: sqlite3.Connection, case_id: int, evidence_id: int) -> int:
    """Rebuild current case co-occurrence edges from entity_links, bounded.

    Full clique generation across thousands of entities creates massive O(n²)
    work. For investigator value, graph edges should focus on high-signal
    entities: accounts, UTRs, IFSCs, UPI, phones, emails, URLs and banks.
    """
    rows = conn.execute(
        "SELECT el.evidence_id, el.entity_id, e.type "
        "FROM entity_links el "
        "JOIN entities e ON e.id = el.entity_id "
        "JOIN evidence ev ON ev.id = el.evidence_id "
        "WHERE ev.case_id = ? AND e.type != 'evidence_type' "
        "ORDER BY el.evidence_id, e.type, el.entity_id",
        (case_id,),
    ).fetchall()
    by_evidence: Dict[int, List[Tuple[int, str]]] = {}
    for r in rows:
        by_evidence.setdefault(int(r["evidence_id"]), []).append((int(r["entity_id"]), str(r["type"] or "")))

    weights: Dict[Tuple[int, int], int] = {}
    for items in by_evidence.values():
        # Prefer investigative/high-value entity types, then cap.
        filtered = [(eid, etype) for eid, etype in items if etype in _HIGH_VALUE_TYPES]
        if not filtered:
            filtered = items
        filtered = sorted(set(filtered), key=lambda x: (_GRAPH_TYPES_PRIORITY.get(x[1], 99), x[0]))[:_MAX_GRAPH_ENTITIES_PER_EVIDENCE]
        ids = sorted({eid for eid, _ in filtered})
        for i in range(len(ids)):
            a = ids[i]
            for b in ids[i + 1:]:
                weights[(a, b)] = weights.get((a, b), 0) + 1

    conn.execute("DELETE FROM relationships WHERE case_id = ?", (case_id,))
    conn.executemany(
        "INSERT INTO relationships (case_id, src_id, dst_id, weight) VALUES (?, ?, ?, ?)",
        [(case_id, a, b, weight) for (a, b), weight in weights.items()],
    )
    return len(weights)


def _shared_entities(conn: sqlite3.Connection, a_id: int, b_id: int) -> List[Dict[str, str]]:
    rows = conn.execute(
        "SELECT e.type, e.value, e.norm FROM entities e "
        "JOIN entity_links la ON la.entity_id = e.id "
        "JOIN entity_links lb ON lb.entity_id = e.id "
        "WHERE la.evidence_id = ? AND lb.evidence_id = ? "
        "ORDER BY e.type, e.value LIMIT 100",
        (a_id, b_id),
    ).fetchall()
    return [{"type": r["type"], "value": r["value"], "norm": r["norm"]} for r in rows]


def _shared_entity_count(conn: sqlite3.Connection, a_id: int, b_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM entity_links la "
        "JOIN entity_links lb ON lb.entity_id = la.entity_id "
        "WHERE la.evidence_id = ? AND lb.evidence_id = ?",
        (a_id, b_id),
    ).fetchone()
    return int(row["c"] or 0)


def _mean_vector(conn: sqlite3.Connection, evidence_id: int) -> List[float]:
    rows = conn.execute("SELECT vec FROM embeddings WHERE evidence_id = ? LIMIT 300", (evidence_id,)).fetchall()
    vecs = [embeddings.unpack(r["vec"]) for r in rows if r["vec"]]
    vecs = [v for v in vecs if v]
    if not vecs:
        return []
    dim = len(vecs[0])
    acc = [0.0] * dim
    for v in vecs:
        for i in range(dim):
            acc[i] += v[i]
    norm = sum(x * x for x in acc) ** 0.5
    return [x / norm for x in acc] if norm > 0 else acc


def _values(conn: sqlite3.Connection, evidence_id: int, field: str) -> set:
    rows = conn.execute(
        f"SELECT {field} AS v FROM transactions WHERE evidence_id = ? AND {field} IS NOT NULL AND {field} != '' LIMIT 5000",
        (evidence_id,),
    ).fetchall()
    return {str(r["v"]).strip().upper() for r in rows if str(r["v"]).strip()}


def _financial_reasons(conn: sqlite3.Connection, a_id: int, b_id: int) -> Tuple[float, List[Dict[str, object]]]:
    reasons: List[Dict[str, object]] = []
    score = 0.0
    fields = [
        ("UTR", "utr", 0.96),
        ("account", "account_no", 0.92),
        ("sender account", "sender_account", 0.88),
        ("receiver account", "receiver_account", 0.92),
        ("IFSC", "ifsc", 0.78),
        ("UPI", "upi", 0.86),
        ("bank", "bank", 0.55),
    ]
    for label, field, weight in fields:
        shared = sorted(_values(conn, a_id, field) & _values(conn, b_id, field))
        if shared:
            reasons.append({"type": f"shared_{field}", "label": f"Shared {label}", "values": shared[:20]})
            score = max(score, weight)

    shared_ents = _shared_entities(conn, a_id, b_id)
    high = [e for e in shared_ents if e["type"] in _HIGH_VALUE_TYPES]
    if high:
        grouped: Dict[str, List[str]] = {}
        for e in high:
            grouped.setdefault(e["type"], []).append(e["value"])
        for etype, vals in grouped.items():
            reasons.append({"type": f"shared_entity_{etype}", "label": f"Shared {etype}", "values": vals[:20]})
        score = max(score, 0.72)

    amount_a = {round(float(v), 2) for v in _values(conn, a_id, "amount") if _looks_float(v)}
    amount_b = {round(float(v), 2) for v in _values(conn, b_id, "amount") if _looks_float(v)}
    amounts = sorted(amount_a & amount_b)
    if amounts:
        reasons.append({"type": "shared_amount", "label": "Shared transaction amount", "values": amounts[:20]})
        score = max(score, 0.50)
    return score, reasons


def _looks_float(value: object) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _candidate_evidence(conn: sqlite3.Connection, case_id: int, evidence_id: int) -> List[int]:
    candidates: set[int] = set()
    # Shared high-value entities.
    rows = conn.execute(
        "SELECT DISTINCT el2.evidence_id AS id FROM entity_links el1 "
        "JOIN entity_links el2 ON el2.entity_id = el1.entity_id "
        "JOIN entities e ON e.id = el1.entity_id "
        "WHERE el1.evidence_id = ? AND el2.evidence_id != ? AND e.case_id = ? "
        "AND e.type IN (%s) LIMIT ?" % ",".join("?" for _ in _HIGH_VALUE_TYPES),
        [evidence_id, evidence_id, case_id, *_HIGH_VALUE_TYPES, _MAX_DUP_CANDIDATES],
    ).fetchall()
    candidates.update(int(r["id"]) for r in rows)

    # Shared transaction values, using indexed fields.
    for field in ("utr", "account_no", "sender_account", "receiver_account", "ifsc", "upi"):
        vals = list(_values(conn, evidence_id, field))[:250]
        if not vals:
            continue
        placeholders = ",".join("?" for _ in vals)
        rows = conn.execute(
            f"SELECT DISTINCT evidence_id AS id FROM transactions WHERE case_id = ? AND evidence_id != ? AND {field} IN ({placeholders}) LIMIT ?",
            [case_id, evidence_id, *vals, _MAX_DUP_CANDIDATES],
        ).fetchall()
        candidates.update(int(r["id"]) for r in rows)
        if len(candidates) >= _MAX_DUP_CANDIDATES:
            break

    if not candidates:
        rows = conn.execute(
            "SELECT id FROM evidence WHERE case_id = ? AND id != ? ORDER BY id DESC LIMIT 25",
            (case_id, evidence_id),
        ).fetchall()
        candidates.update(int(r["id"]) for r in rows)
    return list(candidates)[:_MAX_DUP_CANDIDATES]


def analyse_duplicates(conn: sqlite3.Connection, case_id: int, evidence_id: int) -> int:
    """Record near-duplicate / similar / financial-link edges for one item."""
    other_ids = _candidate_evidence(conn, case_id, evidence_id)
    if not other_ids:
        return 0
    my_vec = _mean_vector(conn, evidence_id)
    recorded = 0
    rows_to_write = []
    for other_id in other_ids:
        a_id, b_id = sorted((evidence_id, other_id))
        text_score = 0.0
        reasons: List[Dict[str, object]] = []
        if my_vec:
            other_vec = _mean_vector(conn, other_id)
            text_score = embeddings.cosine(my_vec, other_vec) if other_vec else 0.0
        fin_score, fin_reasons = _financial_reasons(conn, evidence_id, other_id)
        reasons.extend(fin_reasons)

        if text_score >= _NEAR_DUP:
            kind = "near_duplicate"
            score = text_score
            reasons.append({"type": "text_similarity", "label": "Very high text/vector similarity", "score": round(text_score, 4)})
        elif fin_score >= 0.70:
            kind = "financial_link"
            score = fin_score
        elif text_score >= _SIMILAR:
            kind = "similar"
            score = text_score
            reasons.append({"type": "text_similarity", "label": "Similar text/vector content", "score": round(text_score, 4)})
        elif _shared_entity_count(conn, evidence_id, other_id) > 0:
            kind = "shared_entities"
            score = max(fin_score, 0.25)
            if not reasons:
                shared = _shared_entities(conn, evidence_id, other_id)[:15]
                reasons.append({"type": "shared_entities", "label": "Shared extracted entities", "values": [f"{e['type']}:{e['value']}" for e in shared]})
        else:
            continue
        rows_to_write.append((case_id, a_id, b_id, round(float(score), 4), kind, json.dumps(reasons, ensure_ascii=False)))
        recorded += 1

    if rows_to_write:
        conn.executemany(
            "INSERT OR REPLACE INTO evidence_similarity (case_id, a_id, b_id, score, kind, reasons) VALUES (?, ?, ?, ?, ?, ?)",
            rows_to_write,
        )
    return recorded
