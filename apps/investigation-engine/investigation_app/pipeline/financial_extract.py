"""Structured financial intelligence extraction.

This module is deliberately offline and dependency-free. It converts the plain
text already produced by the evidence pipeline into a structured investigation
profile: case/participant fields, money-trail transaction rows, high-value entities
and first-pass leads. The heuristics are layout-tolerant: they work with
incident PDFs, account reports, CSV/XLSX text dumps, emails and OCR
text without hard-coding one sample document.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

_IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")
_UPI_RE = re.compile(r"\b[a-zA-Z0-9._\-]{2,256}@[a-zA-Z]{2,64}\b(?!\.[A-Za-z])")
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE)
_MAC_RE = re.compile(r"\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b", re.IGNORECASE)
_HASH_RE = re.compile(r"\b(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\b")
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?|"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\b"
)
_CURRENCY_AMOUNT_RE = re.compile(
    r"(?:rs\.?|inr|₹)\s*([0-9]{1,3}(?:,[0-9]{2,3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_LABELED_AMOUNT_RE = re.compile(
    r"(?:amount|amt|disputed|lien|frozen|freeze|hold|debit|credit|transaction\s*amount)"
    r"\s*[:#\-]?\s*(?:rs\.?|inr|₹)?\s*([0-9]{1,3}(?:,[0-9]{2,3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
# Contextual UTR/reference matching avoids treating arbitrary prose as UTRs.
_UTR_CONTEXT_RE = re.compile(
    r"(?:UTR|RRN|Transaction\s*(?:ID|No\.?|Number)(?:\s*/\s*UTR\s*(?:ID|No\.?|Number)?)?|"
    r"Txn\s*(?:ID|No\.?|Number)(?:\s*/\s*UTR\s*(?:ID|No\.?|Number)?)?|"
    r"Ref(?:erence)?\s*(?:ID|No\.?|Number)?)"
    r"\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-/]{7,34})",
    re.IGNORECASE,
)
_ACCOUNT_CONTEXT_RE = re.compile(
    r"(?:account|a/c|acct|beneficiary|receiver|sender|participant|mule)\s*(?:no\.?|number|id)?\s*[:#\-]?\s*(\d[\d\s\-]{7,24}\d)",
    re.IGNORECASE,
)
_ACCOUNT_GENERIC_RE = re.compile(r"(?<![A-Za-z0-9])\d{9,18}(?![A-Za-z0-9])")
_LAYER_RE = re.compile(r"\b(?:layer|level)\s*[-:]?\s*(\d{1,2})\b", re.IGNORECASE)
_BANK_RE = re.compile(
    r"(?:bank\s*(?:name)?|bank\s*/\s*fis?|bank\s*/\s*fi|branch\s*bank|beneficiary\s*bank|action\s*taken\s*by\s*bank)\s*[:#\-]?\s*([A-Za-z][A-Za-z0-9 &().,\-/]{2,80})",
    re.IGNORECASE,
)
_BANK_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z&. ]{2,60}\b(?:Bank|BANK)(?:\s+of\s+[A-Za-z]+)?|State Bank of India|HDFC Bank|ICICI Bank|Axis Bank|Kotak Mahindra Bank|Punjab National Bank|Union Bank of India|Bank of Baroda)\b",
    re.IGNORECASE,
)

_FIELD_PATTERNS: Dict[str, List[re.Pattern]] = {
    "acknowledgement_number": [
        re.compile(r"\b(?:acknowledgement|acknowledgment|ack)\b\s*(?:no\.?|number)?\s*[:#\-]?\s*([A-Z0-9\-/]{6,40})", re.I),
        re.compile(r"\b(REF[\-/]?[A-Z0-9\-/]{4,40})\b", re.I),
    ],
    "reference_number": [
        re.compile(r"\b(?:incident|reference|case)\b\s*(?:no\.?|number)?\s*[:#\-]?\s*([A-Z0-9\-/]{3,40})", re.I),
    ],
    "category": [re.compile(r"(?:category)\s*[:#\-]?\s*([^\n\r]{3,120})", re.I)],
    "sub_category": [re.compile(r"(?:sub[-\s]?category)\s*[:#\-]?\s*([^\n\r]{3,120})", re.I)],
    "source_location": [re.compile(r"(?:source|office|location)\s*[:#\-]?\s*([^\n\r]{3,120})", re.I)],
    "district": [re.compile(r"(?:district)\s*[:#\-]?\s*([^\n\r]{2,80})", re.I)],
    "state": [re.compile(r"(?:state)\s*[:#\-]?\s*([^\n\r]{2,80})", re.I)],
    "reporting_datetime": [re.compile(r"(?:report(?:ed|ing)?\s*(?:date|time|on)?)\s*[:#\-]?\s*([^\n\r]{6,60})", re.I)],
    "accepted_datetime": [re.compile(r"(?:accepted|acknowledged)\s*(?:date|time|on)?\s*[:#\-]?\s*([^\n\r]{6,60})", re.I)],
    "current_status": [re.compile(r"(?:status)\s*[:#\-]?\s*([^\n\r]{3,100})", re.I)],
    "description": [re.compile(r"(?:description|summary|incident details|request details)\s*[:#\-]?\s*([\s\S]{20,800})", re.I)],
}

_PARTICIPANT_PATTERNS: Dict[str, List[re.Pattern]] = {
    "name": [re.compile(r"(?:requester|participant|applicant)\s*(?:name)?\s*[:#\-]?\s*([A-Za-z][A-Za-z .]{2,80})", re.I)],
    "mobile": [re.compile(r"(?:mobile|phone|contact)\s*(?:no\.?|number)?\s*[:#\-]?\s*((?:\+?91[-\s]?)?[6-9]\d{9})", re.I)],
    "gender": [re.compile(r"(?:gender)\s*[:#\-]?\s*(male|female|other)", re.I)],
    "dob_or_age": [re.compile(r"(?:dob|date\s*of\s*birth|age)\s*[:#\-]?\s*([^\n\r]{1,40})", re.I)],
    "address": [re.compile(r"(?:address)\s*[:#\-]?\s*([^\n\r]{8,250})", re.I)],
    "city": [re.compile(r"(?:city)\s*[:#\-]?\s*([^\n\r]{2,80})", re.I)],
    "pin": [re.compile(r"(?:pin|pincode|postal\s*code)\s*[:#\-]?\s*(\d{6})", re.I)],
}

_PLATFORM_PATTERNS = {
    "Telegram": re.compile(r"\btelegram\b|t\.me/|telegram\.me/", re.I),
    "WhatsApp": re.compile(r"\bwhats\s*app\b|\bwhatsapp\b|wa\.me/", re.I),
    "Instagram": re.compile(r"\binstagram\b|instagram\.com/", re.I),
    "Facebook": re.compile(r"\bfacebook\b|fb\.com/", re.I),
    "Website": re.compile(r"\bhttps?://|\bwebsite\b|\bdomain\b", re.I),
    "UPI": re.compile(r"\bUPI\b|@[a-zA-Z]{2,64}\b", re.I),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_value(etype: str, value: str) -> str:
    raw = str(value or "").strip()
    if etype in {"email", "upi", "url", "domain"}:
        return raw.lower()
    if etype == "phone":
        digits = re.sub(r"\D", "", raw)
        return digits[-10:] if len(digits) >= 10 else digits
    if etype in {"ifsc", "utr", "mac", "vehicle", "file_hash"}:
        return re.sub(r"\s", "", raw).upper()
    if etype == "account":
        return re.sub(r"\D", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _clean_field(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip(" :#-\t\r\n")
    return value[:limit]


def _first(patterns: Iterable[re.Pattern], text: str, limit: int = 240) -> Optional[str]:
    for pat in patterns:
        m = pat.search(text or "")
        if m:
            return _clean_field(m.group(1), limit)
    return None


def _amount_to_float(raw: str) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def _amounts(text: str) -> List[float]:
    vals: List[float] = []
    for pat in (_CURRENCY_AMOUNT_RE, _LABELED_AMOUNT_RE):
        for m in pat.findall(text or ""):
            val = _amount_to_float(m)
            if val is not None and val not in vals:
                vals.append(val)
    return vals


def _unique(items: Iterable[str], etype: str = "") -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        raw = str(item).strip()
        norm = norm_value(etype, raw) if etype else raw.lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(raw)
    return out


def _sample_large_text(text: str, head: int = 260_000, tail: int = 80_000) -> str:
    """Return a representative sample for broad regex scans on huge workbooks.

    Structured Excel extraction emits every row as labelled text. The
    structured transaction parser must see the full text, but broad global
    regex passes over multi-megabyte sheets are expensive and duplicate what
    the transaction parser already extracts. Sampling preserves document-level
    fields from the top and miscellaneous footer/header signals without making
    every global regex walk the entire workbook.
    """
    text = text or ""
    if len(text) <= head + tail:
        return text
    marker = "\n\n--- middle rows omitted for fast structured processing ---\n\n"
    return text[:head] + marker + text[-tail:]

def _looks_like_large_structured_sheet(text: str) -> bool:
    if not text or len(text) < 750_000:
        return False
    sample = text[:120_000].lower()
    return "sheet " in sample and " row " in sample and ("account" in sample or "transaction" in sample or "ifsc" in sample or "amount" in sample)


def _extract_accounts(text: str) -> List[str]:
    found: List[str] = []
    found.extend(_ACCOUNT_CONTEXT_RE.findall(text or ""))
    # Generic account numbers, but avoid phone numbers and UTR-embedded digits.
    for m in _ACCOUNT_GENERIC_RE.finditer(text or ""):
        raw = m.group(0)
        if _PHONE_RE.fullmatch(raw):
            continue
        found.append(raw)
    return _unique([norm_value("account", a) for a in found], "account")


def _extract_utrs(text: str) -> List[str]:
    found = []
    for raw in _UTR_CONTEXT_RE.findall(text or ""):
        val = raw.strip().strip(".,;:()[]{}")
        if len(re.sub(r"[^A-Za-z0-9]", "", val)) >= 8:
            found.append(norm_value("utr", val))
    return _unique(found, "utr")


def _extract_banks(text: str) -> List[str]:
    found = []
    for pat in (_BANK_RE, _BANK_NAME_RE):
        for raw in pat.findall(text or ""):
            val = _clean_field(raw, 80)
            # Stop at table-like separators.
            val = re.split(r"\s{2,}|\||,\s*(?:IFSC|Account|A/c)", val, maxsplit=1)[0].strip()
            if val and len(val) >= 3 and val.lower() not in {"ltd", "limited", "of india", "of baroda"}:
                found.append(val)
    return _unique(found, "bank")


def _status_from_text(line: str) -> str:
    l = (line or "").lower()
    for key in ("lien", "freeze", "frozen", "hold", "blocked", "credited", "debited", "success", "failed", "pending", "recovered"):
        if key in l:
            return key.upper()
    return ""


def _classify_account(line: str, accounts: List[str]) -> Tuple[str, str, str]:
    if not accounts:
        return "", "", ""
    l = (line or "").lower()
    first = accounts[0]
    if any(k in l for k in ("beneficiary", "receiver", "credited to", "to account", "mule")):
        return "", first, first
    if any(k in l for k in ("sender", "participant", "debited from", "from account", "requester")):
        return first, "", first
    return "", "", first


def _parse_labeled_row(line: str) -> Dict[str, str]:
    """Parse spreadsheet text lines emitted by extractors._extract_office.

    Example line:
    ``Sheet Money Transfer to row 2 | Account No: 123 | IFSC Code: HDFC...``
    Keeping header/value pairs prevents the generic regex layer from confusing
    serial numbers, timestamps, acknowledgement numbers and phone-like values.
    """
    if not re.match(r"^Sheet .+ row \d+ \|", line or "", re.I):
        return {}
    parts = [p.strip() for p in (line or "").split("|")]
    out: Dict[str, str] = {"_sheet_row": parts[0] if parts else ""}
    for part in parts[1:]:
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        key = re.sub(r"\s+", " ", key).strip().lower()
        val = str(val or "").strip()
        if key and val:
            out[key] = val
    return out


def _field(fields: Dict[str, str], *needles: str, exact: bool = False) -> str:
    for k, v in fields.items():
        if k.startswith("_"):
            continue
        nk = re.sub(r"[^a-z0-9]+", " ", k.lower()).strip()
        for needle in needles:
            nn = re.sub(r"[^a-z0-9]+", " ", needle.lower()).strip()
            if (exact and nk == nn) or (not exact and nn in nk):
                return v
    return ""


def _amount_field(fields: Dict[str, str], *needles: str) -> Optional[float]:
    raw = _field(fields, *needles)
    if not raw:
        return None
    return _amount_to_float(str(raw).replace("₹", "").replace("INR", "").replace("Rs.", "").replace("Rs", ""))


def _int_field(fields: Dict[str, str], *needles: str) -> Optional[int]:
    raw = _field(fields, *needles)
    if not raw:
        return None
    try:
        return int(float(str(raw).strip()))
    except ValueError:
        return None


def _transaction_from_labeled_row(
    fields: Dict[str, str],
    case_id: int,
    evidence_id: int,
    sha256: str,
    filename: str,
    line_idx: int,
    raw_line: str,
) -> Optional[Dict[str, Any]]:
    """Create a structured transaction from one Excel/CSV row.

    This is the high-confidence path for structured account workbooks. It uses the
    column names directly instead of regexing the whole row, which fixes large
    sheets where times were detected as IPv6, emails as UPI fragments, and
    acknowledgement/serial numbers as accounts.
    """
    if not fields:
        return None
    sheet_ref = fields.get("_sheet_row", f"line {line_idx}")
    sheet_l = sheet_ref.lower()

    source_account = norm_value("account", _field(fields, "account no wallet pg pa id", "wallet pg pa", "source account"))
    beneficiary_account = norm_value("account", _field(fields, "account no", exact=True) or _field(fields, "beneficiary account", "receiver account"))
    if not beneficiary_account and ("hold" in sheet_l or "atm" in sheet_l or "pos" in sheet_l or "cheque" in sheet_l or "aeps" in sheet_l):
        beneficiary_account = source_account

    utr = norm_value("utr", _field(fields, "transaction id utr number", "transaction id", "utr number", "reference no"))
    ifsc = norm_value("ifsc", _field(fields, "ifsc code", "ifsc"))
    bank = _field(fields, "bank/fis", "bank fis", "bank fi", "action taken by bank", "bank")
    txn_date = _field(fields, "transaction date", "withdrawal date time", "put on hold date", "date of transaction")
    amount = _amount_field(fields, "transaction amount", "withdrawal amount", "put on hold amount", "amount")
    disputed_amount = _amount_field(fields, "disputed amount")
    lien_amount = _amount_field(fields, "put on hold amount", "lien amount", "frozen amount") if ("hold" in sheet_l or "lien" in raw_line.lower() or "freeze" in raw_line.lower()) else None
    layer = _int_field(fields, "layer")
    upis = _unique(_UPI_RE.findall(raw_line or ""), "upi")

    signal_count = sum(1 for x in (beneficiary_account, source_account, utr, ifsc, bank, amount, upis) if x not in (None, "", []))
    if signal_count < 2:
        return None

    remarks = _field(fields, "remarks", "place location of atm", "atm id", "reference no") or raw_line
    status = _status_from_text(raw_line)
    if not status and ("hold" in sheet_l or lien_amount is not None):
        status = "HOLD"

    return {
        "case_id": case_id,
        "evidence_id": evidence_id,
        "source_file": filename,
        "source_ref": sheet_ref.replace("Sheet ", "sheet "),
        "file_hash": sha256,
        "layer": layer,
        "txn_date": txn_date,
        "utr": utr,
        "amount": amount,
        "disputed_amount": disputed_amount,
        "lien_amount": lien_amount,
        "sender_account": source_account if source_account and source_account != beneficiary_account else "",
        "receiver_account": beneficiary_account,
        "account_no": beneficiary_account or source_account,
        "ifsc": ifsc,
        "bank": _clean_field(bank, 100),
        "upi": upis[0].lower() if upis else "",
        "wallet": _field(fields, "wallet", "pg", "pa") if not beneficiary_account else "",
        "merchant": _field(fields, "merchant"),
        "status": status,
        "remarks": _clean_field(remarks, 300),
        "metadata": {"source_window": raw_line[:1200], "parsed_from": "labeled_spreadsheet_row"},
    }


def _extract_transactions(text: str, case_id: int, evidence_id: int, sha256: str, filename: str) -> List[Dict[str, Any]]:
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text or "") if ln.strip()]
    rows: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        if len(line) < 8:
            continue
        if re.match(r"^Sheet .+ header \|", line, re.I) or re.match(r"^\[Sheet:", line, re.I):
            continue
        # High-confidence structured row path for Excel/CSV extraction.
        labeled_fields = _parse_labeled_row(line)
        labeled_txn = _transaction_from_labeled_row(labeled_fields, case_id, evidence_id, sha256, filename, idx, line)
        if labeled_txn is not None:
            rows.append(labeled_txn)
            continue

        # Pull in a short neighbouring window because OCR/PDF table text often wraps.
        prev_line = lines[idx - 2] if idx >= 2 else ""
        next_line = lines[idx] if idx < len(lines) else ""
        window = " | ".join([prev_line, line, next_line])
        accounts = _extract_accounts(window)
        ifscs = _unique(_IFSC_RE.findall(window), "ifsc")
        utrs = _extract_utrs(window)
        amounts = _amounts(window)
        dates = _DATE_RE.findall(window)
        banks = _extract_banks(window)
        upis = _unique(_UPI_RE.findall(window), "upi")
        signal_count = sum(1 for x in (accounts, ifscs, utrs, amounts, banks, upis) if x)
        key_words = re.search(r"\b(utr|transaction|txn|amount|disputed|lien|freeze|beneficiary|sender|receiver|layer|ifsc|bank|account|wallet|merchant)\b", window, re.I)
        if signal_count < 2 and not (key_words and (accounts or utrs or amounts)):
            continue
        layer_match = _LAYER_RE.search(window)
        sender, receiver, account_no = _classify_account(window, accounts)
        lower = window.lower()
        lien_amount = None
        disputed_amount = None
        if "lien" in lower or "freeze" in lower or "frozen" in lower:
            lien_amount = amounts[0] if amounts else None
        if "disputed" in lower:
            disputed_amount = amounts[0] if amounts else None
        row = {
            "case_id": case_id,
            "evidence_id": evidence_id,
            "source_file": filename,
            "source_ref": f"line {idx}",
            "file_hash": sha256,
            "layer": int(layer_match.group(1)) if layer_match else None,
            "txn_date": dates[0] if dates else "",
            "utr": utrs[0] if utrs else "",
            "amount": amounts[0] if amounts else None,
            "disputed_amount": disputed_amount,
            "lien_amount": lien_amount,
            "sender_account": sender,
            "receiver_account": receiver,
            "account_no": account_no,
            "ifsc": ifscs[0].upper() if ifscs else "",
            "bank": banks[0] if banks else "",
            "upi": upis[0].lower() if upis else "",
            "wallet": _clean_field(re.search(r"(?:wallet|payment\s*gateway)\s*[:#\-]?\s*([^|,;]{2,60})", window, re.I).group(1), 80) if re.search(r"(?:wallet|payment\s*gateway)\s*[:#\-]?\s*([^|,;]{2,60})", window, re.I) else "",
            "merchant": _clean_field(re.search(r"(?:merchant)\s*[:#\-]?\s*([^|,;]{2,80})", window, re.I).group(1), 80) if re.search(r"(?:merchant)\s*[:#\-]?\s*([^|,;]{2,80})", window, re.I) else "",
            "status": _status_from_text(window),
            "remarks": _clean_field(line, 300),
            "metadata": {"source_window": window[:800]},
        }
        rows.append(row)

    # If the document has global values but no line-level table, create one
    # coarse row so the AI/report still sees structured money trail signals.
    if not rows:
        accounts = _extract_accounts(text)
        ifscs = _unique(_IFSC_RE.findall(text or ""), "ifsc")
        utrs = _extract_utrs(text)
        amounts = _amounts(text)
        banks = _extract_banks(text)
        if accounts or ifscs or utrs or amounts:
            sender, receiver, account_no = _classify_account(text, accounts)
            rows.append({
                "case_id": case_id,
                "evidence_id": evidence_id,
                "source_file": filename,
                "source_ref": "document",
                "file_hash": sha256,
                "layer": None,
                "txn_date": (_DATE_RE.findall(text or "") or [""])[0],
                "utr": utrs[0] if utrs else "",
                "amount": amounts[0] if amounts else None,
                "disputed_amount": None,
                "lien_amount": None,
                "sender_account": sender,
                "receiver_account": receiver,
                "account_no": account_no,
                "ifsc": ifscs[0].upper() if ifscs else "",
                "bank": banks[0] if banks else "",
                "upi": (_unique(_UPI_RE.findall(text or ""), "upi") or [""])[0],
                "wallet": "",
                "merchant": "",
                "status": _status_from_text(text),
                "remarks": "Document-level financial signals extracted.",
                "metadata": {"source_window": (text or "")[:800]},
            })

    # Deduplicate wrapped table rows.
    dedup: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for r in rows:
        key = (
            norm_value("utr", r.get("utr", "")),
            norm_value("account", r.get("account_no") or r.get("receiver_account") or r.get("sender_account") or ""),
            norm_value("ifsc", r.get("ifsc", "")),
            r.get("amount"),
            r.get("txn_date"),
            r.get("layer"),
        )
        if key not in dedup:
            dedup[key] = r
    return list(dedup.values())


def build_intelligence(
    text: str,
    case_id: int,
    evidence_id: int,
    sha256: str = "",
    filename: str = "",
) -> Dict[str, Any]:
    text = text or ""
    large_structured_sheet = _looks_like_large_structured_sheet(text)
    scan_text = _sample_large_text(text) if large_structured_sheet else text

    case_info: Dict[str, Any] = {}
    for key, patterns in _FIELD_PATTERNS.items():
        val = _first(patterns, scan_text, 800 if key == "description" else 240)
        if val:
            case_info[key] = val
    platforms = [name for name, pat in _PLATFORM_PATTERNS.items() if pat.search(scan_text)]
    if platforms:
        case_info["fraud_platform_source"] = platforms

    participant: Dict[str, Any] = {}
    for key, patterns in _PARTICIPANT_PATTERNS.items():
        val = _first(patterns, scan_text)
        if val:
            participant[key] = val
    if "mobile" not in participant:
        phones = _PHONE_RE.findall(scan_text)
        if phones:
            participant["mobile"] = phones[0]

    # Full text is required here: this is the high-confidence transaction table
    # parser. The expensive global regex passes below use scan_text on large
    # sheets because transaction-derived values already contain the full money trail.
    transactions = _extract_transactions(text, case_id, evidence_id, sha256, filename)
    structured_accounts = [a for t in transactions for a in (t.get("sender_account"), t.get("receiver_account"), t.get("account_no")) if a]
    accounts = _unique(
        structured_accounts + ([] if large_structured_sheet and transactions else _extract_accounts(scan_text)),
        "account",
    )
    case_numbers = {
        norm_value("account", case_info.get("acknowledgement_number", "")),
        norm_value("account", case_info.get("reference_number", "")),
    }
    accounts = [a for a in accounts if norm_value("account", a) not in case_numbers]
    utrs = _unique([t.get("utr", "") for t in transactions if t.get("utr")] + ([] if large_structured_sheet and transactions else _extract_utrs(scan_text)), "utr")
    ifscs = _unique([t.get("ifsc", "") for t in transactions if t.get("ifsc")] + ([] if large_structured_sheet and transactions else _IFSC_RE.findall(scan_text)), "ifsc")
    banks = _unique([t.get("bank", "") for t in transactions if t.get("bank")] + ([] if large_structured_sheet and transactions else _extract_banks(scan_text)), "bank")
    upis = _unique([t.get("upi", "") for t in transactions if t.get("upi")] + ([] if large_structured_sheet and transactions else _UPI_RE.findall(scan_text)), "upi")
    phones = _unique(_PHONE_RE.findall(scan_text), "phone")
    emails = _unique(_EMAIL_RE.findall(scan_text), "email")
    urls = _unique(_URL_RE.findall(scan_text), "url")
    macs = _unique(_MAC_RE.findall(scan_text), "mac")
    hashes = _unique(_HASH_RE.findall(scan_text), "file_hash")

    txn_amounts = [t.get("amount") for t in transactions if t.get("amount") is not None]
    total_amount = sum(float(x) for x in txn_amounts) if txn_amounts else None
    if total_amount is not None and "total_amount_detected" not in case_info:
        case_info["total_amount_detected"] = round(total_amount, 2)

    leads: List[str] = []
    for label, values in (("UTR", utrs), ("account", accounts), ("IFSC", ifscs)):
        if values:
            leads.append(f"Extracted {len(values)} {label}(s) for correlation across evidence.")
    if transactions:
        leads.append(f"Structured {len(transactions)} financial transaction row(s) from this evidence.")
    repeated_accounts = [a for a, c in Counter(t.get("account_no") for t in transactions if t.get("account_no")).items() if c > 1]
    if repeated_accounts:
        leads.append("Repeated account(s) inside this evidence: " + ", ".join(repeated_accounts[:8]))

    summary_bits = []
    if case_info.get("acknowledgement_number"):
        summary_bits.append(f"Ack {case_info['acknowledgement_number']}")
    if transactions:
        summary_bits.append(f"{len(transactions)} transaction rows")
    if total_amount:
        summary_bits.append(f"approx amount ₹{total_amount:,.2f}")
    if banks:
        summary_bits.append("banks: " + ", ".join(banks[:5]))
    summary = "; ".join(summary_bits) if summary_bits else "No structured financial profile could be confidently extracted."

    return {
        "case_info": case_info,
        "participant": participant,
        "related_parties": [],
        "transactions": transactions,
        "accounts": accounts,
        "banks": banks,
        "communications": {
            "phones": phones,
            "emails": emails,
            "upis": upis,
            "urls": urls,
        },
        "technical_indicators": {
            "ifscs": ifscs,
            "utrs": utrs,
            "macs": macs,
            "file_hashes": hashes,
            "platforms": platforms,
        },
        "timeline_events": [
            {
                "timestamp": t.get("txn_date"),
                "event_type": "transaction",
                "description": f"Transaction {t.get('utr') or ''} amount {t.get('amount') or ''}".strip(),
                "source_file": filename,
                "source_ref": t.get("source_ref", ""),
                "confidence": 0.72,
            }
            for t in transactions if t.get("txn_date")
        ],
        "entities": [],
        "relationships": [],
        "duplicates": [],
        "similar_evidence": [],
        "summary": summary,
        "important_leads": leads,
        "processing_errors": [],
        "source": {"case_id": case_id, "evidence_id": evidence_id, "file_hash": sha256, "source_file": filename},
        "generated_at": _now(),
    }


def entities_from_intelligence(intel: Dict[str, Any]) -> List[Dict[str, str]]:
    """Convert a structured profile into entity rows for the existing engine."""
    out: List[Dict[str, str]] = []
    seen = set()

    def add(etype: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(etype, item)
            return
        raw = str(value).strip()
        norm = norm_value(etype, raw)
        if not raw or not norm:
            return
        key = (etype, norm)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": etype, "value": raw, "norm": norm})

    case_info = intel.get("case_info") or {}
    participant = intel.get("participant") or {}
    comms = intel.get("communications") or {}
    tech = intel.get("technical_indicators") or {}

    add("acknowledgement", case_info.get("acknowledgement_number"))
    add("reference", case_info.get("reference_number"))
    add("participant", participant.get("name"))
    add("phone", participant.get("mobile"))
    add("location", participant.get("address"))
    add("location", participant.get("city"))
    add("location", case_info.get("source_location"))
    add("location", case_info.get("district"))
    add("organization", case_info.get("source_location"))
    add("bank", intel.get("banks") or [])
    add("account", intel.get("accounts") or [])
    add("phone", comms.get("phones") or [])
    add("email", comms.get("emails") or [])
    add("upi", comms.get("upis") or [])
    add("url", comms.get("urls") or [])
    add("ifsc", tech.get("ifscs") or [])
    add("utr", tech.get("utrs") or [])
    add("mac", tech.get("macs") or [])
    add("file_hash", tech.get("file_hashes") or [])
    for platform in tech.get("platforms") or []:
        p = platform.lower()
        add(p if p in {"telegram", "whatsapp", "instagram"} else "platform", platform)
    for txn in intel.get("transactions") or []:
        add("utr", txn.get("utr"))
        add("account", txn.get("sender_account"))
        add("account", txn.get("receiver_account"))
        add("account", txn.get("account_no"))
        add("ifsc", txn.get("ifsc"))
        add("bank", txn.get("bank"))
        add("upi", txn.get("upi"))
        add("wallet", txn.get("wallet"))
        add("merchant", txn.get("merchant"))
        add("date", txn.get("txn_date"))
    return out


def profile_to_index_text(intel: Dict[str, Any]) -> str:
    """Compact text representation for FTS/RAG indexing."""
    if not intel:
        return ""
    parts = [intel.get("summary", "")]
    case_info = intel.get("case_info") or {}
    participant = intel.get("participant") or {}
    for label, data in (("case", case_info), ("participant", participant)):
        for k, v in data.items():
            parts.append(f"{label} {k}: {v}")
    for t in intel.get("transactions") or []:
        parts.append(
            "transaction "
            f"utr {t.get('utr','')} account {t.get('account_no','')} "
            f"sender {t.get('sender_account','')} receiver {t.get('receiver_account','')} "
            f"ifsc {t.get('ifsc','')} bank {t.get('bank','')} amount {t.get('amount','')} "
            f"date {t.get('txn_date','')} layer {t.get('layer','')} status {t.get('status','')} "
            f"source {t.get('source_file','')} {t.get('source_ref','')}"
        )
    for lead in intel.get("important_leads") or []:
        parts.append(f"lead: {lead}")
    return "\n".join(p for p in parts if p)
