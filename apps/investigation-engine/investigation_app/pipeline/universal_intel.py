"""Universal digital evidence intelligence extraction.

Adds lightweight, offline parsing for screenshots/OCR text, social-media/chat
exports, code/config/log evidence, emails and technical artifacts. It does not
execute files and does not call any cloud service; it enriches the existing
financial profile so older reports/UI keep working.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlparse

_MAX_MESSAGES = 800
_MAX_PROFILES = 200
_MAX_INDICATORS = 800

_URL_RE = re.compile(r"\bhttps?://[^\s<>'\")]+", re.I)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")
_UPI_RE = re.compile(r"\b[a-zA-Z0-9._\-]{2,256}@[a-zA-Z]{2,64}\b(?!\.[A-Za-z])")
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_IPV6_RE = re.compile(r"(?<![A-F0-9:])(?:[A-F0-9]{1,4}:){3,7}[A-F0-9]{1,4}(?![A-F0-9:])", re.I)
_MAC_RE = re.compile(r"\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b", re.I)
_IMEI_RE = re.compile(r"\bimei\s*[:#-]?\s*(\d{15,16})\b", re.I)
_IMSI_RE = re.compile(r"\bimsi\s*[:#-]?\s*(\d{14,16})\b", re.I)
_ICCID_RE = re.compile(r"\b89\d{17,18}\b")
_ANDROID_ID_RE = re.compile(r"\bandroid[_\s-]*id\s*[:#=-]?\s*([a-f0-9]{16})\b", re.I)
_DEVICE_ID_RE = re.compile(r"\b(?:device\s*id|serial(?:\s*number)?)\s*[:#=-]?\s*([A-Za-z0-9_.:-]{6,64})\b", re.I)
_HASH_RE = re.compile(r"\b(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\b")
_CRYPTO_RE = re.compile(r"\b(?:bc1[a-z0-9]{20,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|0x[a-fA-F0-9]{40})\b")
_COORD_RE = re.compile(r"[-+]?\d{1,2}\.\d{4,}\s*,\s*[-+]?\d{1,3}\.\d{4,}")
_QR_RE = re.compile(r"\b(?:qr\s*(?:code|payload)|upi\s*qr|scanned\s*qr)\s*[:#=-]?\s*(.{4,240})", re.I)
_HANDLE_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{3,40})")
# Social/profile regexes intentionally require either a URL or an explicit label
# separator (``instagram handle: foo``). The previous Instagram pattern matched
# OCR fragments like "insta lled" and invented usernames such as "lled".
_INSTAGRAM_RE = re.compile(
    r"(?:https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{3,40})|"
    r"\b(?:instagram|insta)\b\s*(?:id|handle|profile|username)?\s*[:#=-]\s*@?([A-Za-z0-9_.]{3,40}))",
    re.I,
)
_TELEGRAM_RE = re.compile(
    r"(?:https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_+/\-]{3,80})|"
    r"\btelegram\b\s*(?:id|group|channel|handle|username)?\s*[:#=-]\s*@?([A-Za-z0-9_+/\-]{3,80}))",
    re.I,
)
_FACEBOOK_RE = re.compile(
    r"(?:https?://(?:www\.)?(?:facebook\.com|fb\.com)/([A-Za-z0-9_.\-/]{3,80})|"
    r"\bfacebook\b\s*(?:id|profile|username)?\s*[:#=-]\s*([A-Za-z0-9_.\-/]{3,80}))",
    re.I,
)
_YOUTUBE_RE = re.compile(r"(?:https?://(?:www\.)?(?:youtube\.com/(?:@|channel/|c/)?|youtu\.be/))([A-Za-z0-9_.\-]{3,80})", re.I)
_WA_LINK_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\d{8,15})", re.I)
_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.\d+)?|[0-9]+(?:\.\d+)?)\s*(?:rupees|rs|inr)?", re.I)

_WHATSAPP_LINE = re.compile(r"^\[?(\d{1,2}[/-]\d{1,2}[/-]\d{2,4},?\s+\d{1,2}:\d{2}(?:\s?[AP]M)?)\]?\s*[-–]\s*([^:]{1,120}):\s*(.+)$", re.I)
_TELEGRAM_HEAD = re.compile(r"^([^,\[][^\[] {0,120}|[^\[] {1,120}),\s*\[(\d{1,2}[/-]\d{1,2}[/-]\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$", re.I)
_SIMPLE_SENDER = re.compile(r"^([@A-Za-z0-9_. +\-]{3,80})\s*:\s*(.{3,})$")

_RISK_RULES: List[Tuple[str, re.Pattern]] = [
    ("payment_request", re.compile(r"\b(pay|payment|transfer|send money|deposit|upi|account|bank|qr|amount|wallet)\b", re.I)),
    ("otp_request", re.compile(r"\b(otp|one time password|verification code|code share|share code)\b", re.I)),
    ("threat", re.compile(r"\b(threat|blackmail|expose|viral)\b", re.I)),
    ("blackmail", re.compile(r"\b(blackmail|leak|viral|private photo|video call recording)\b", re.I)),
    ("sextortion", re.compile(r"\b(nude|sextortion|video call|screen recording|private video)\b", re.I)),
    ("investment_promise", re.compile(r"\b(invest|profit|return|double|trading|crypto|guaranteed)\b", re.I)),
    ("task_scam", re.compile(r"\b(task|like and subscribe|rating|telegram task|commission)\b", re.I)),
    ("loan_app_pressure", re.compile(r"\b(loan|repay|recovery agent|late fee)\b", re.I)),
    ("fake_support", re.compile(r"\b(customer support|helpline|kyc|bank official|refund support)\b", re.I)),
    ("identity_impersonation", re.compile(r"\b(fake profile|impersonat|pretending|using my photo|profile cloned)\b", re.I)),
    ("urgent_transfer", re.compile(r"\b(urgent|immediately|today only|last chance|fast transfer)\b", re.I)),
    ("crypto_payment", re.compile(r"\b(bitcoin|btc|ethereum|usdt|crypto|wallet address)\b", re.I)),
    ("phishing_link", re.compile(r"\b(login|verify|kyc|short link|bit\.ly|tinyurl|apk|download)\b", re.I)),
    ("malware_link", re.compile(r"\b(apk|exe|payload|rat|trojan|download app|install this)\b", re.I)),
]

_PLATFORM_WORDS = {
    "instagram": re.compile(r"\b(instagram|instagram\.com|insta\s*(?:id|handle|profile|username)|reels?|followers|following|verified badge)\b", re.I),
    "whatsapp": re.compile(r"\b(whatsapp|wa\.me|media omitted|message deleted|end-to-end encrypted)\b", re.I),
    "telegram": re.compile(r"\b(telegram|t\.me|channel|group admin|bot)\b", re.I),
    "facebook": re.compile(r"\b(facebook|fb\.com|messenger)\b", re.I),
    "email": re.compile(r"\b(from:|to:|subject:|message-id:|received:)\b", re.I),
}

_CLASS_RULES = [
    ("financial_fraud_record", re.compile(r"\b(acknowledg|incident|fraud report|fraud amount)\b", re.I)),
    ("bank_action_report", re.compile(r"\b(action taken|lien|freeze|frozen|bank nodal|beneficiary bank)\b", re.I)),
    ("transaction_sheet", re.compile(r"\b(utr|transaction id|ifsc|account no|debit|credit|amount|layer)\b", re.I)),
    ("social_media_profile", re.compile(r"\b(profile|followers|following|bio|verified badge|username)\b", re.I)),
    ("social_media_chat", re.compile(r"\b(dm|direct message|chat|message|sender|receiver)\b", re.I)),
    ("whatsapp_chat", _PLATFORM_WORDS["whatsapp"]),
    ("telegram_chat", _PLATFORM_WORDS["telegram"]),
    ("instagram_chat", _PLATFORM_WORDS["instagram"]),
    ("facebook_chat", _PLATFORM_WORDS["facebook"]),
    ("qr_code_evidence", re.compile(r"\b(qr\s*(?:code|payload)|upi\s*qr|scan\s*(?:and|&)\s*pay|bharat\s*qr)\b", re.I)),
    ("email_evidence", _PLATFORM_WORDS["email"]),
    ("ipdr_cdr", re.compile(r"\b(ipdr|cdr|imei|imsi|iccid|cell id|tower|msisdn|lac|cgi)\b", re.I)),
    ("device_artifact", re.compile(r"\b(device id|android id|imei|imsi|mac address|serial number|browser|operating system)\b", re.I)),
    ("url_domain_evidence", re.compile(r"\b(https?://|domain|url|website|phishing link)\b", re.I)),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unique(values: Iterable[Any], key: str | None = None) -> List[str]:
    seen, out = set(), []
    for v in values:
        s = str(v or "").strip()
        if not s:
            continue
        k = norm_value(key or "generic", s)
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out



_NOISY_SOCIAL_USERNAMES = {
    "lled", "installed", "signin", "sign", "aws", "amazon", "services",
    "calculator", "pricing", "optimization", "profile", "username", "handle",
    "followers", "following", "verified", "button", "login", "signin"
}


def _first_match_group(match: re.Match) -> str:
    """Return the first non-empty capture group from an alternation regex."""
    for value in match.groups():
        if value:
            return value
    return ""


def _valid_social_username(username: Any, platform: str = "social") -> bool:
    user = str(username or "").strip().strip("@/").lower()
    if len(user) < 3 or len(user) > 40:
        return False
    if user in _NOISY_SOCIAL_USERNAMES:
        return False
    if not re.fullmatch(r"[a-z0-9_.][a-z0-9_.-]{1,78}[a-z0-9_]", user, re.I):
        return False
    # OCR fragments from prose often have no digit/underscore/dot and are very short.
    if len(user) <= 4 and not re.search(r"[0-9_.]", user):
        return False
    return True


def _has_qr_payload(text: str, meta: Dict[str, Any] | None = None) -> bool:
    blob = text or ""
    extracted = (meta or {}).get("extracted") or {}
    if isinstance(extracted, dict) and (extracted.get("qr_payload") or extracted.get("qr_payloads")):
        return True
    return bool(re.search(r"\b(qr\s*(?:code|payload)|upi\s*qr|scan\s*(?:and|&)\s*pay|bharat\s*qr)\b", blob, re.I))


def _sample_large_text(text: str, head: int = 180_000, tail: int = 60_000) -> str:
    """Bound broad universal/social scans on huge structured workbooks."""
    text = text or ""
    if len(text) <= head + tail:
        return text
    marker = "\n\n--- middle rows omitted for fast universal scan ---\n\n"
    return text[:head] + marker + text[-tail:]

def _large_financial_sheet(text: str, base: Dict[str, Any] | None = None) -> bool:
    if len(text or "") < 750_000:
        return False
    txns = len((base or {}).get("transactions") or [])
    sample = (text or "")[:120_000].lower()
    return txns >= 500 and "sheet " in sample and " row " in sample

def norm_value(etype: str, value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    etype = (etype or "").lower()
    if etype in {"phone", "whatsapp_number"}:
        d = re.sub(r"\D", "", s)
        return d[-10:] if len(d) >= 10 else d
    if etype in {"email", "upi", "url", "website_url", "domain", "social_handle", "username", "alias", "instagram_username", "telegram_username", "facebook_profile", "youtube_channel", "qr_payload"}:
        return s.lower().strip("@/")
    if etype in {"ifsc", "utr", "mac", "imei", "imsi", "iccid", "device_id", "android_id", "file_hash", "crypto_wallet"}:
        return re.sub(r"\s+", "", s).upper()
    if etype in {"account", "account_number", "card"}:
        return re.sub(r"\D", "", s)
    return s.lower()


def classify(text: str, filename: str = "", kind: str = "", meta: Dict[str, Any] | None = None) -> List[str]:
    blob = f"{filename}\n{kind}\n{text[:20000]}"
    cats = [name for name, pat in _CLASS_RULES if pat.search(blob)]
    # Prevent random OCR fragments (for example the letters "qr" in an image) from
    # turning ordinary screenshots into QR evidence. QR is added only when a real
    # QR phrase or decoded payload exists.
    if "qr_code_evidence" in cats and not _has_qr_payload(text, meta):
        cats = [c for c in cats if c != "qr_code_evidence"]
    ext = os.path.splitext(filename or "")[1].lower()
    if kind == "image" and "screenshot" not in cats:
        cats.append("screenshot")
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"} and "image_document" not in cats:
        cats.append("image_document")
    if ext in {".csv", ".xls", ".xlsx"} and "transaction_sheet" not in cats and re.search(r"\b(amount|account|ifsc|utr|transaction)\b", blob, re.I):
        cats.append("transaction_sheet")
    if not cats:
        cats.append("mixed_document" if text.strip() else "unknown")
    return _unique(cats)


def _domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.split("@")[-1].split(":")[0]
    except Exception:
        return ""


def _amounts(text: str) -> List[float]:
    vals: List[float] = []
    for m in _AMOUNT_RE.findall(text or ""):
        try:
            vals.append(float(str(m).replace(",", "")))
        except ValueError:
            pass
    # Avoid treating years/tiny numbers as amounts unless context exists.
    return [v for v in vals if v >= 1][:20]


def _risk_flags(text: str) -> List[str]:
    return [name for name, pat in _RISK_RULES if pat.search(text or "")]


def extract_messages(text: str, filename: str = "") -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    platform_hint = "whatsapp" if _PLATFORM_WORDS["whatsapp"].search(text or "") else "telegram" if _PLATFORM_WORDS["telegram"].search(text or "") else "social/chat"

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        ts = sender = msg = ""
        platform = platform_hint
        m = _WHATSAPP_LINE.match(line.strip())
        if m:
            ts, sender, msg = m.group(1), m.group(2).strip(), m.group(3).strip()
            platform = "whatsapp"
        else:
            mt = _TELEGRAM_HEAD.match(line.strip())
            if mt:
                sender, ts, msg = mt.group(1).strip(), mt.group(2).strip(), mt.group(3).strip()
                platform = "telegram"
            else:
                ms = _SIMPLE_SENDER.match(line.strip())
                if ms and (_risk_flags(ms.group(2)) or _URL_RE.search(ms.group(2)) or _UPI_RE.search(ms.group(2)) or _PHONE_RE.search(ms.group(2))):
                    sender, msg = ms.group(1).strip(), ms.group(2).strip()
        if not msg or len(msg) < 2:
            continue
        urls = _unique(_URL_RE.findall(msg), "url")
        handles = _unique(_HANDLE_RE.findall(msg), "social_handle")
        flags = _risk_flags(msg)
        amounts = _amounts(msg)
        entities = []
        for etype, vals in (
            ("phone", _PHONE_RE.findall(msg)),
            ("email", _EMAIL_RE.findall(msg)),
            ("upi", _UPI_RE.findall(msg)),
            ("url", urls),
            ("social_handle", handles),
            ("crypto_wallet", _CRYPTO_RE.findall(msg)),
        ):
            for val in _unique(vals, etype):
                entities.append({"type": etype, "value": val, "norm": norm_value(etype, val)})
        messages.append({
            "platform": platform,
            "sender": sender,
            "receiver": "",
            "sender_handle": handles[0] if handles else "",
            "receiver_handle": "",
            "message_text": msg[:2000],
            "timestamp": ts,
            "entities_found": entities,
            "attachments": [],
            "urls": urls,
            "amounts": amounts,
            "risk_flags": flags,
            "source_ref": f"line {idx}",
            "confidence": 0.88 if ts and sender else 0.58,
        })
        if len(messages) >= _MAX_MESSAGES:
            break
    return messages


def extract_social_profiles(text: str, filename: str = "") -> List[Dict[str, Any]]:
    profiles: List[Dict[str, Any]] = []
    seen = set()

    def add(platform: str, username: str, url: str = "", source: str = "text", confidence: float = 0.74) -> None:
        user = (username or "").strip().strip("@/")
        if not user:
            return
        key = (platform, user.lower(), (url or "").lower())
        if key in seen:
            return
        seen.add(key)
        profiles.append({
            "platform": platform,
            "profile_name": "",
            "username": user,
            "profile_url": url,
            "bio": _bio_hint(text),
            "metadata": {
                "source": source,
                "followers": _first_number_after(text, r"followers"),
                "following": _first_number_after(text, r"following"),
                "verified_badge_visible": bool(re.search(r"\bverified\b|✓", text or "", re.I)),
                "business_category": _first_after(text, r"business category|category", 80),
            },
            "source_ref": source,
            "confidence": confidence,
        })

    for m in _INSTAGRAM_RE.finditer(text or ""):
        user = _first_match_group(m)
        if _valid_social_username(user, "instagram"):
            add("instagram", user, source="instagram pattern", confidence=0.86)
    for m in _TELEGRAM_RE.finditer(text or ""):
        user = _first_match_group(m)
        if _valid_social_username(user, "telegram"):
            add("telegram", user, source="telegram pattern", confidence=0.86)
    for m in _FACEBOOK_RE.finditer(text or ""):
        user = _first_match_group(m)
        if _valid_social_username(user, "facebook"):
            add("facebook", user, source="facebook pattern", confidence=0.80)
    for m in _YOUTUBE_RE.finditer(text or ""):
        user = _first_match_group(m)
        if _valid_social_username(user, "youtube"):
            add("youtube", user, source="youtube pattern", confidence=0.75)
    for m in _WA_LINK_RE.finditer(text or ""):
        add("whatsapp", m.group(1), source="wa.me link", confidence=0.84)

    # Generic @handles from social screenshots/chats. They remain lower confidence
    # and are not duplicated when a platform-specific profile was already found.
    existing_handles = {norm_value("social_handle", p.get("username")) for p in profiles}
    if any(p.search(text or "") for p in _PLATFORM_WORDS.values()):
        for h in _unique(_HANDLE_RE.findall(text or ""), "social_handle")[:80]:
            if _valid_social_username(h) and norm_value("social_handle", h) not in existing_handles:
                add("social", h, source="@handle", confidence=0.56)
    return profiles[:_MAX_PROFILES]


def _first_after(text: str, label: str, limit: int = 160) -> str:
    m = re.search(rf"(?:{label})\s*[:#=-]?\s*(.{{2,{limit}}})", text or "", re.I)
    return (m.group(1).splitlines()[0].strip() if m else "")[:limit]


def _first_number_after(text: str, label: str) -> str:
    m = re.search(rf"([0-9][0-9,\.KMkm ]{{0,15}})\s+{label}|{label}\s*[:#=-]?\s*([0-9][0-9,\.KMkm ]{{0,15}})", text or "", re.I)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def _bio_hint(text: str) -> str:
    val = _first_after(text, r"bio|about", 220)
    if val:
        return val
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    social_lines = [ln for ln in lines[:40] if len(ln) <= 180 and not re.search(r"\b(followers|following|posts|message|call)\b", ln, re.I)]
    return " | ".join(social_lines[:3])[:400]


def extract_technical_indicators(text: str, meta: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    meta = meta or {}
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(itype: str, value: Any, source: str = "text", confidence: float = 0.82, extra: Dict[str, Any] | None = None) -> None:
        raw = str(value or "").strip().strip(".,;)\"")
        if not raw:
            return
        norm = norm_value(itype, raw)
        if not norm:
            return
        key = (itype, norm)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": itype, "value": raw, "norm": norm, "source_ref": source, "confidence": confidence, "metadata": extra or {}})

    for url in _unique(_URL_RE.findall(text or ""), "url"):
        add("website_url", url, "url", 0.90)
        dom = _domain(url)
        if dom:
            add("domain", dom, "derived from url", 0.88)
    for etype, pat in (
        ("email", _EMAIL_RE), ("phone", _PHONE_RE), ("UPI", _UPI_RE), ("IP", _IPV4_RE),
        ("MAC", _MAC_RE), ("ICCID", _ICCID_RE), ("file_hash", _HASH_RE), ("crypto_wallet", _CRYPTO_RE), ("GPS", _COORD_RE),
    ):
        for val in _unique(pat.findall(text or ""), etype):
            add(etype, val, etype, 0.86)
    for val in _unique(_IPV6_RE.findall(text or ""), "IPv6"):
        # Avoid false IPv6 extraction from timestamps such as 01:10:24.
        if str(val).count(":") >= 3:
            add("IPv6", val, "IPv6", 0.86)
    for val in _unique(_IMEI_RE.findall(text or ""), "IMEI"):
        add("IMEI", val, "IMEI", 0.78)
    for val in _unique(_IMSI_RE.findall(text or ""), "IMSI"):
        # IMSI can overlap generic 15-digit account/IMEI; only keep if label exists nearby.
        if re.search(r"imsi", text or "", re.I):
            add("IMSI", val, "IMSI", 0.80)
    for val in _unique(_ANDROID_ID_RE.findall(text or ""), "android_id"):
        add("android_id", val, "android id", 0.86)
    for val in _unique(_DEVICE_ID_RE.findall(text or ""), "device_id"):
        add("device_id", val, "device id", 0.74)
    for val in _unique(_QR_RE.findall(text or ""), "qr_payload"):
        add("qr_payload", val[:220], "qr text", 0.70)

    extracted = meta.get("extracted") or {}
    gps = extracted.get("gps") if isinstance(extracted, dict) else None
    if isinstance(gps, dict) and "lat" in gps and "lon" in gps:
        add("GPS", f"{gps['lat']},{gps['lon']}", "EXIF GPS", 0.94, {"exif": True})
    for key in ("Make", "Model", "DateTimeOriginal", "DateTime", "created", "modified", "author", "last_modified_by"):
        val = extracted.get(key) if isinstance(extracted, dict) else None
        if val:
            add("metadata", f"{key}: {val}", "metadata", 0.65)
    return out[:_MAX_INDICATORS]


def _lead_summary(messages: List[Dict[str, Any]], profiles: List[Dict[str, Any]], indicators: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    leads: List[str] = []
    actions: List[str] = []
    flags = Counter(flag for m in messages for flag in (m.get("risk_flags") or []))
    if messages:
        leads.append(f"Structured {len(messages)} communication/message record(s) for review.")
        actions.append("Review message records for payment requests, OTP requests, threats and suspicious links.")
    if profiles:
        platforms = _unique([p.get("platform") for p in profiles])
        leads.append("Extracted social profile/handle evidence from: " + ", ".join(platforms[:8]))
        actions.append("Verify extracted social handles against platform profile URLs/screenshots before notice generation.")
    if flags:
        leads.append("Message risk flags detected: " + ", ".join(f"{k}({v})" for k, v in flags.most_common(10)))
    risky_urls = [i.get("value") for i in indicators if i.get("type") in {"website_url", "domain", "qr_payload", "crypto_wallet"}]
    if risky_urls:
        leads.append(f"Technical/link/payment indicators extracted: {len(risky_urls)} item(s).")
        actions.append("Preserve screenshots/original files and run URL/domain/QR indicators through approved offline verification workflow.")
    return leads, actions


def enrich_intelligence(base: Dict[str, Any] | None, text: str, case_id: int, evidence_id: int, sha256: str = "", filename: str = "", kind: str = "", meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    intel: Dict[str, Any] = dict(base or {})
    scan_text = _sample_large_text(text or "") if _large_financial_sheet(text or "", intel) else (text or "")
    messages = extract_messages(scan_text, filename)
    profiles = extract_social_profiles(scan_text, filename)
    indicators = extract_technical_indicators(scan_text, meta)
    categories = classify(scan_text, filename, kind, meta)
    leads, actions = _lead_summary(messages, profiles, indicators)

    # Backward-compatible shape plus the plural keys requested by the universal spec.
    intel["evidence_types"] = categories
    intel.setdefault("participants", [])
    if intel.get("participant") and intel["participant"] not in intel["participants"]:
        intel["participants"].append(intel["participant"])
    intel["messages"] = messages
    intel["social_profiles"] = profiles
    intel["technical_indicators_universal"] = indicators
    intel.setdefault("media_artifacts", [])
    intel.setdefault("relationships", [])
    intel.setdefault("duplicates", [])
    intel.setdefault("similar_evidence", [])
    intel.setdefault("communications", intel.get("communications") or {})
    if isinstance(intel["communications"], dict):
        intel["communications"].setdefault("messages", len(messages))
        intel["communications"].setdefault("social_profiles", len(profiles))
    intel.setdefault("important_leads", [])
    intel["important_leads"] = _unique(list(intel.get("important_leads") or []) + leads)
    intel["recommended_actions"] = _unique(list(intel.get("recommended_actions") or []) + actions)

    summary_bits = [intel.get("summary") or ""]
    if categories:
        summary_bits.append("types: " + ", ".join(categories[:6]))
    if messages:
        summary_bits.append(f"{len(messages)} messages")
    if profiles:
        summary_bits.append(f"{len(profiles)} social profiles/handles")
    if indicators:
        summary_bits.append(f"{len(indicators)} technical indicators")
    intel["summary"] = "; ".join([s for s in summary_bits if s and not s.startswith("No structured")]) or "Universal evidence profile created; no high-confidence structured values found."
    intel["source"] = {"case_id": case_id, "evidence_id": evidence_id, "file_hash": sha256, "source_file": filename}
    intel["generated_at"] = _now()
    return intel


def entities_from_intelligence(intel: Dict[str, Any]) -> List[Dict[str, str]]:
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
        out.append({"type": etype.lower(), "value": raw, "norm": norm})

    # Evidence categories are metadata for filtering/reporting, not investigative
    # entities. Storing them as entities polluted relationship graphs with nodes
    # like "evidence_type: screenshot" and created misleading links.
    for p in intel.get("social_profiles") or []:
        platform = p.get("platform") or "social"
        username = p.get("username")
        add("social_handle", username)
        add(f"{platform}_username", username)
        add("platform", platform)
        add("url", p.get("profile_url"))
    for m in intel.get("messages") or []:
        add("person", m.get("sender"))
        add("social_handle", m.get("sender_handle"))
        for ent in m.get("entities_found") or []:
            add(ent.get("type", "entity"), ent.get("value"))
        for url in m.get("urls") or []:
            add("url", url)
    for i in intel.get("technical_indicators_universal") or []:
        add(i.get("type", "indicator"), i.get("value"))
    return out


def profile_to_index_text(intel: Dict[str, Any]) -> str:
    if not intel:
        return ""
    parts: List[str] = []
    if intel.get("evidence_types"):
        parts.append("evidence types: " + ", ".join(intel.get("evidence_types") or []))
    for p in intel.get("social_profiles") or []:
        parts.append("social profile " + " ".join(str(p.get(k, "")) for k in ("platform", "profile_name", "username", "profile_url", "bio")))
    for m in intel.get("messages") or []:
        flags = ",".join(m.get("risk_flags") or [])
        urls = ",".join(m.get("urls") or [])
        parts.append(f"message platform {m.get('platform','')} sender {m.get('sender','')} time {m.get('timestamp','')} flags {flags} urls {urls}: {m.get('message_text','')}")
    for i in intel.get("technical_indicators_universal") or []:
        parts.append(f"technical indicator {i.get('type','')}: {i.get('value','')} source {i.get('source_ref','')}")
    for lead in intel.get("important_leads") or []:
        parts.append(f"lead: {lead}")
    for action in intel.get("recommended_actions") or []:
        parts.append(f"recommended action: {action}")
    return "\n".join(p for p in parts if p)
