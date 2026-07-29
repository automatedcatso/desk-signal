"""Regex-based entity engine.

Deterministic, dependency-free extraction of the investigative entity types
required by the spec. Each match is normalised so the same real-world entity
collapses to one row (store once, cross-reference automatically).

The patterns favour precision over recall for local Indian digital-evidence data
(phones, UPI, IFSC, vehicle numbers) while keeping the universal ones (email,
IP, URL, IMEI) broad. This is intentionally rule-based: it is instant, uses
no RAM to speak of, and never needs the LLM.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

# Each pattern yields (type, regex). Order matters only for readability.
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ipv4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("url", re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE)),
    ("upi", re.compile(r"\b[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}\b(?!\.[A-Za-z])")),
    ("ifsc", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    ("imei", re.compile(r"\bimei\s*[:#\-]?\s*(\d{15,16})\b", re.IGNORECASE)),
    ("iccid", re.compile(r"\b89\d{17,18}\b")),
    ("gps", re.compile(r"[-+]?\d{1,2}\.\d{4,},\s*[-+]?\d{1,3}\.\d{4,}")),
    ("wallet_btc", re.compile(r"\b(?:bc1[a-z0-9]{20,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")),
    ("wallet_eth", re.compile(r"\b0x[a-fA-F0-9]{40}\b")),
    ("file_hash", re.compile(r"\b(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\b")),
    ("mac", re.compile(r"\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b", re.IGNORECASE)),
    ("telegram", re.compile(r"(?:\bt\.me/[A-Za-z0-9_+/\-]+|\btelegram\.me/[A-Za-z0-9_+/\-]+|\btelegram\s*(?:group|id|channel)?\s*[:#-]?\s*@?[A-Za-z0-9_]{4,})", re.IGNORECASE)),
    ("whatsapp", re.compile(r"(?:\bwa\.me/\d{8,15}|\bwhatsapp\s*(?:number|group|id)?\s*[:#-]?\s*(?:\+?91[-\s]?)?[6-9]\d{9})", re.IGNORECASE)),
    ("instagram", re.compile(r"(?:instagram\.com/[A-Za-z0-9_.]+|\binstagram\s*(?:id|profile|handle)?\s*[:#-]?\s*@?[A-Za-z0-9_.]{3,})", re.IGNORECASE)),
    ("utr", re.compile(r"(?:UTR|RRN|Transaction\s*(?:ID|No\.?|Number)|Txn\s*(?:ID|No\.?|Number)|Ref(?:erence)?\s*(?:ID|No\.?|Number)?)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-/]{7,34})", re.IGNORECASE)),
    ("vehicle", re.compile(r"\b[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4}\b")),
    ("phone", re.compile(r"(?<!\d)(?:\+?91[\-\s]?)?[6-9]\d{9}(?!\d)")),
    ("account", re.compile(r"(?<![A-Za-z0-9])\d{9,18}(?![A-Za-z0-9])")),
    ("date", re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")),
]

# Domain is derived from URLs/emails rather than matched blindly.
_DOMAIN_RE = re.compile(r"\b([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)\b")


def _normalise(etype: str, value: str) -> str:
    v = value.strip()
    if etype in {"email", "upi", "url", "domain"}:
        return v.lower()
    if etype == "phone":
        digits = re.sub(r"\D", "", v)
        return digits[-10:] if len(digits) >= 10 else digits
    if etype in {"wallet_eth", "ifsc", "vehicle", "utr", "mac", "file_hash"}:
        return re.sub(r"\s", "", v).upper()
    if etype == "account":
        return re.sub(r"\D", "", v)
    if etype == "gps":
        return re.sub(r"\s", "", v)
    return v


def extract(text: str) -> List[Dict[str, str]]:
    """Return a de-duplicated list of ``{type, value, norm}`` entities.

    A single (type, norm) pair appears at most once. ``upi`` and ``email``
    can overlap textually; both are kept because their normalised forms and
    investigative meaning differ, and downstream storage is keyed on
    (type, norm) so no true duplicate is created.
    """
    if not text:
        return []
    seen: Set[Tuple[str, str]] = set()
    out: List[Dict[str, str]] = []

    def _add(etype: str, raw: str) -> None:
        norm = _normalise(etype, raw)
        if not norm:
            return
        key = (etype, norm)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": etype, "value": raw.strip(), "norm": norm})

    emails: Set[str] = set()
    urls: List[str] = []
    for etype, pattern in _PATTERNS:
        for match in pattern.findall(text):
            raw = match if isinstance(match, str) else match[0]
            if etype == "account" and re.fullmatch(r"(?:\+?91[-\s]?)?[6-9]\d{9}", raw):
                continue
            if etype == "email":
                emails.add(raw.lower())
            if etype == "url":
                urls.append(raw)
            _add(etype, raw)

    # Derive domains from emails and URLs only (avoids matching prose words).
    for email in emails:
        _add("domain", email.split("@", 1)[-1])
    for url in urls:
        m = _DOMAIN_RE.search(re.sub(r"^https?://", "", url, flags=re.IGNORECASE))
        if m:
            _add("domain", m.group(1))

    return out
