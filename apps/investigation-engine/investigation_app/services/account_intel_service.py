"""Deterministic account timeline and transaction-risk intelligence.

This module answers investigator-style questions from the structured
``transactions`` table before the local LLM is used. It is deliberately
rule-based, offline and transparent: every flag includes the row/account/source
that caused it so the reviewer can verify the finding.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple

Transaction = Dict[str, Any]

_ACCOUNT_RE = re.compile(r"(?<!\d)\d{8,20}(?!\d)")
_AMOUNT_RE = re.compile(
    r"(?:above|over|more than|greater than|>=|>|rs\.?|inr|₹)?\s*"
    r"(\d+(?:,\d{2,3})*(?:\.\d+)?)\s*(lakh|lac|crore|cr|k|thousand)?",
    re.I,
)

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
)

_BANK_ALIASES = {
    "hdfc": ("hdfc", "hdfcbank", "okhdfcbank", "pthdfc", "hdfc bank"),
    "sbi": ("sbi", "state bank", "oksbi", "ptsbi", "sbin"),
    "icici": ("icici", "okicici", "icic"),
    "axis": ("axis", "okaxis", "axl", "ptaxis"),
    "kotak": ("kotak", "kotakpay"),
    "yes": ("yesbank", "ptyes", "ybl", "yes bank"),
    "paytm": ("paytm", "pty", "ptys"),
    "phonepe": ("phonepe",),
}


def _clean_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _money(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        val = float(str(value).replace(",", "").strip())
        if math.isnan(val) or math.isinf(val):
            return 0.0
        return val
    except (TypeError, ValueError):
        return 0.0


def _fmt_money(value: Any) -> str:
    val = _money(value)
    return f"₹{val:,.2f}" if val else "₹0.00"


def _parse_date(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "").replace("T", " ")
    # Excel serial date support, just in case the parser stores the raw serial.
    if re.fullmatch(r"\d{5}(?:\.\d+)?", raw):
        try:
            base = datetime(1899, 12, 30)
            return base + timedelta(days=float(raw))
        except Exception:
            return None
    # Keep only the first full-looking date/time from noisy cells.
    m = re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", raw)
    if m:
        raw = m.group(0)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def _txn_time(txn: Transaction) -> Optional[datetime]:
    for key in ("txn_date", "created_at"):
        dt = _parse_date(txn.get(key))
        if dt:
            return dt
    return None


def _accounts(txn: Transaction) -> List[str]:
    vals = []
    for key in ("sender_account", "receiver_account", "account_no"):
        norm = _clean_digits(txn.get(key))
        if len(norm) >= 8:
            vals.append(norm)
    out, seen = [], set()
    for v in vals:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _primary_account(txn: Transaction) -> str:
    for key in ("receiver_account", "account_no", "sender_account"):
        norm = _clean_digits(txn.get(key))
        if len(norm) >= 8:
            return norm
    return "UNKNOWN"


def _direction(txn: Transaction, account: str) -> str:
    account = _clean_digits(account)
    if account and account == _clean_digits(txn.get("sender_account")):
        return "outgoing/source"
    if account and account == _clean_digits(txn.get("receiver_account")):
        return "incoming/beneficiary"
    if account and account == _clean_digits(txn.get("account_no")):
        return "account mentioned"
    return "related"


def _source(txn: Transaction) -> str:
    ev = f"Evidence #{txn.get('evidence_id')}"
    name = txn.get("original_name") or txn.get("source_file") or ""
    ref = txn.get("source_ref") or ""
    if name:
        ev += f" ({name})"
    if ref:
        ev += f" {ref}"
    return ev


def _txn_blob(txn: Transaction) -> str:
    keys = (
        "bank", "ifsc", "upi", "utr", "account_no", "sender_account", "receiver_account",
        "sender_account", "status", "remarks", "source_file", "original_name", "source_ref", "merchant", "wallet",
    )
    return " ".join(str(txn.get(k) or "") for k in keys).lower()


def _query_accounts(query: str) -> List[str]:
    return [m.group(0) for m in _ACCOUNT_RE.finditer(query or "")]


def _query_amount_threshold(query: str, amounts: List[float]) -> float:
    q = query or ""
    for m in _AMOUNT_RE.finditer(q):
        raw, scale = m.group(1), (m.group(2) or "").lower()
        if not raw:
            continue
        value = float(raw.replace(",", ""))
        if scale in {"lakh", "lac"}:
            value *= 100000
        elif scale in {"crore", "cr"}:
            value *= 10000000
        elif scale in {"k", "thousand"}:
            value *= 1000
        if value >= 1000:
            return value
    positives = sorted(x for x in amounts if x > 0)
    if not positives:
        return 50000.0
    if len(positives) >= 20:
        p90 = positives[int(len(positives) * 0.90)]
        return max(50000.0, p90)
    return max(50000.0, median(positives) * 3)


def _query_terms(query: str) -> List[str]:
    stop = {
        "timeline", "time", "line", "account", "accounts", "transaction", "transactions", "show", "create", "make",
        "give", "me", "find", "all", "for", "of", "the", "and", "or", "suspicious", "flag", "risk", "profile",
        "money", "amount", "withdrawal", "withdraw", "withdrawing", "withdraws", "same", "short", "duration",
        "repeated", "repeat", "large", "big", "above", "over", "more", "than", "greater", "lakh", "lac",
        "crore", "cr", "thousand", "pattern", "patterns", "within", "hour", "hours", "minutes",
    }
    terms = []
    q = (query or "").lower()
    for key, aliases in _BANK_ALIASES.items():
        if key in q or any(a in q for a in aliases):
            terms.extend(aliases)
    for tok in re.findall(r"[a-zA-Z0-9@._-]{3,}", q):
        if tok not in stop and not tok.isdigit():
            terms.append(tok)
    # unique while preserving order
    seen, out = set(), []
    for t in terms:
        t = t.lower()
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def filter_transactions(txns: List[Transaction], query: str) -> List[Transaction]:
    """Filter transactions for explicit account numbers or text terms."""
    if not txns:
        return []
    accounts = _query_accounts(query)
    if accounts:
        wanted = {_clean_digits(a) for a in accounts}
        return [t for t in txns if wanted & set(_accounts(t))]
    terms = _query_terms(query)
    if not terms:
        return txns
    strict = [t for t in txns if all(term in _txn_blob(t) for term in terms)]
    if strict:
        return strict
    return [t for t in txns if any(term in _txn_blob(t) for term in terms)]


def _account_groups(txns: List[Transaction]) -> Dict[str, List[Transaction]]:
    groups: Dict[str, List[Transaction]] = defaultdict(list)
    for t in txns:
        for account in _accounts(t) or [_primary_account(t)]:
            if account and account != "UNKNOWN":
                groups[account].append(t)
    return groups


def account_timeline_answer(txns: List[Transaction], query: str) -> Optional[str]:
    q = (query or "").lower()
    if not ("timeline" in q or "chronology" in q or "sequence" in q or ("history" in q and "account" in q)):
        return None
    if not txns:
        return "No structured transactions are stored yet, so an account timeline cannot be built."

    filtered = filter_transactions(txns, query)
    if not filtered:
        return "No transactions matched that account/timeline filter. Try an exact account number, bank, IFSC, UPI, or UTR."

    accounts = _query_accounts(query)
    if accounts:
        account = _clean_digits(accounts[0])
        rows = [t for t in filtered if account in _accounts(t)]
        accounts_to_show = [(account, rows)]
    else:
        grouped = _account_groups(filtered)
        accounts_to_show = sorted(grouped.items(), key=lambda kv: (-sum(_money(t.get("amount")) for t in kv[1]), kv[0]))[:8]

    lines = ["Account transaction timeline from structured evidence:"]
    for account, rows in accounts_to_show:
        rows = sorted(rows, key=lambda t: (_txn_time(t) is None, _txn_time(t) or datetime.max, t.get("id") or 0))
        total = sum(_money(t.get("amount")) for t in rows)
        lien = sum(_money(t.get("lien_amount")) for t in rows)
        banks = sorted({str(t.get("bank") or "").strip() for t in rows if t.get("bank")})
        ifscs = sorted({str(t.get("ifsc") or "").strip() for t in rows if t.get("ifsc")})
        lines.append(f"\nAccount `{account}` — {len(rows)} transaction row(s), total {_fmt_money(total)}" + (f", lien/hold {_fmt_money(lien)}" if lien else ""))
        if banks:
            lines.append(f"- Bank(s): {', '.join(banks[:6])}")
        if ifscs:
            lines.append(f"- IFSC(s): {', '.join(ifscs[:6])}")
        for t in rows[:80]:
            dt = t.get("txn_date") or "undated"
            left = t.get("sender_account") or "source"
            right = t.get("receiver_account") or t.get("account_no") or "beneficiary"
            lines.append(
                f"- {dt} | {_direction(t, account)} | `{left}` → `{right}` | amount {_fmt_money(t.get('amount'))} | "
                f"UTR `{t.get('utr') or 'N/A'}` | bank {t.get('bank') or 'N/A'} | status {t.get('status') or 'N/A'} | {_source(t)}"
            )
        if len(rows) > 80:
            lines.append(f"  ... {len(rows) - 80} more rows omitted. Ask for a date range, UTR, amount, or export for full detail.")
    return "\n".join(lines)


def _flag(label: str, severity: str, reason: str, txns: Iterable[Transaction], account: str = "") -> Dict[str, Any]:
    rows = list(txns)
    score = {"critical": 95, "high": 80, "medium": 55, "low": 30}.get(severity.lower(), 40)
    return {
        "label": label,
        "severity": severity.upper(),
        "score": score,
        "reason": reason,
        "account": account,
        "transactions": rows,
        "total_amount": sum(_money(t.get("amount")) for t in rows),
    }


def find_suspicious_patterns(txns: List[Transaction], query: str = "") -> List[Dict[str, Any]]:
    if not txns:
        return []
    filtered = filter_transactions(txns, query)
    if not filtered:
        filtered = txns
    amounts = [_money(t.get("amount")) for t in filtered]
    high_threshold = _query_amount_threshold(query, amounts)
    flags: List[Dict[str, Any]] = []

    # 1. High-value transactions.
    for t in filtered:
        amt = _money(t.get("amount"))
        if amt >= high_threshold:
            flags.append(_flag("high_value_transaction", "high", f"Amount {_fmt_money(amt)} is above threshold {_fmt_money(high_threshold)}.", [t], _primary_account(t)))

    # 2. Repeated accounts and high aggregate mule-style accounts.
    by_account = _account_groups(filtered)
    for account, rows in by_account.items():
        total = sum(_money(t.get("amount")) for t in rows)
        unique_utrs = {str(t.get("utr") or "").upper() for t in rows if t.get("utr")}
        unique_senders = {_clean_digits(t.get("sender_account")) for t in rows if _clean_digits(t.get("sender_account"))}
        unique_ifscs = {str(t.get("ifsc") or "").upper() for t in rows if t.get("ifsc")}
        if len(rows) >= 5 or total >= max(high_threshold, 100000):
            sev = "critical" if len(rows) >= 10 or total >= 500000 else "high"
            flags.append(_flag(
                "repeated_or_high_aggregate_account",
                sev,
                f"Account appears in {len(rows)} transaction row(s), total {_fmt_money(total)}, {len(unique_utrs)} UTR(s), {len(unique_senders)} source account(s).",
                rows[:12],
                account,
            ))
        if len(unique_ifscs) >= 2:
            flags.append(_flag("same_account_multiple_ifsc", "medium", f"Same account is linked with multiple IFSC values: {', '.join(sorted(unique_ifscs)[:5])}.", rows[:10], account))

        # Rapid repeated same amount for the same account.
        by_amount: Dict[float, List[Transaction]] = defaultdict(list)
        for t in rows:
            amt = round(_money(t.get("amount")), 2)
            if amt > 0:
                by_amount[amt].append(t)
        for amt, same_rows in by_amount.items():
            if len(same_rows) < 2:
                continue
            dated = [(t, _txn_time(t)) for t in same_rows if _txn_time(t)]
            dated.sort(key=lambda x: x[1])
            rapid: List[Transaction] = []
            for i in range(len(dated)):
                window = [dated[i][0]]
                start = dated[i][1]
                for j in range(i + 1, len(dated)):
                    if dated[j][1] - start <= timedelta(minutes=60):
                        window.append(dated[j][0])
                    else:
                        break
                if len(window) >= 2 and len(window) > len(rapid):
                    rapid = window
            if rapid:
                flags.append(_flag(
                    "rapid_same_amount_repetition",
                    "high",
                    f"Account has {len(rapid)} transaction(s) of the same amount {_fmt_money(amt)} within 60 minutes.",
                    rapid[:10],
                    account,
                ))
            elif len(same_rows) >= 3:
                flags.append(_flag("same_amount_repeated", "medium", f"Account has the same amount {_fmt_money(amt)} repeated {len(same_rows)} times.", same_rows[:10], account))

        # Structuring / smurfing: many small transactions in short windows.
        small = [(t, _txn_time(t)) for t in rows if 0 < _money(t.get("amount")) <= 5000 and _txn_time(t)]
        small.sort(key=lambda x: x[1])
        for i, (start_txn, start_dt) in enumerate(small):
            window = [start_txn]
            for next_txn, next_dt in small[i + 1:]:
                if next_dt - start_dt <= timedelta(hours=2):
                    window.append(next_txn)
                else:
                    break
            if len(window) >= 5:
                flags.append(_flag("rapid_many_small_transactions", "medium", f"{len(window)} small transactions occurred within 2 hours for the same account.", window[:12], account))
                break

    # 3. Repeated UTRs across rows.
    by_utr: Dict[str, List[Transaction]] = defaultdict(list)
    for t in filtered:
        utr = str(t.get("utr") or "").upper().strip()
        if utr:
            by_utr[utr].append(t)
    for utr, rows in by_utr.items():
        if len(rows) >= 2:
            flags.append(_flag("duplicate_utr_reference", "critical", f"UTR/reference `{utr}` appears in {len(rows)} transaction rows.", rows[:12], _primary_account(rows[0])))

    # 4. Same amount hits multiple accounts in short periods.
    by_amount_all: Dict[float, List[Transaction]] = defaultdict(list)
    for t in filtered:
        amt = round(_money(t.get("amount")), 2)
        if amt > 0:
            by_amount_all[amt].append(t)
    for amt, rows in by_amount_all.items():
        accounts = {_primary_account(t) for t in rows if _primary_account(t) != "UNKNOWN"}
        if len(accounts) < 2 or len(rows) < 3:
            continue
        dated = [(t, _txn_time(t)) for t in rows if _txn_time(t)]
        dated.sort(key=lambda x: x[1])
        best: List[Transaction] = []
        for i, (_t, start) in enumerate(dated):
            window = [r for r, dt in dated[i:] if dt - start <= timedelta(hours=2)]
            if len({_primary_account(w) for w in window}) >= 2 and len(window) > len(best):
                best = window
        if best:
            flags.append(_flag("same_amount_multiple_accounts_short_window", "medium", f"Amount {_fmt_money(amt)} appears across {len({_primary_account(w) for w in best})} accounts within 2 hours.", best[:12]))

    # 5. Round-number / withdrawal / lien gaps / high layer.
    for t in filtered:
        amt = _money(t.get("amount"))
        text = _txn_blob(t)
        account = _primary_account(t)
        if amt >= 10000 and amt % 10000 == 0:
            flags.append(_flag("large_round_amount", "medium", f"Large round transaction amount {_fmt_money(amt)}.", [t], account))
        elif amt >= 5000 and amt % 1000 == 0:
            flags.append(_flag("round_amount", "low", f"Round transaction amount {_fmt_money(amt)}.", [t], account))
        if any(k in text for k in ("atm", "cash", "withdraw", "withdrawal")):
            flags.append(_flag("cash_or_atm_withdrawal_indicator", "high" if amt >= 10000 else "medium", f"Transaction/status/remarks indicate ATM/cash withdrawal with amount {_fmt_money(amt)}.", [t], account))
        lien = _money(t.get("lien_amount"))
        if amt > 0 and 0 < lien < amt:
            flags.append(_flag("partial_lien_or_hold_gap", "medium", f"Only {_fmt_money(lien)} lien/hold against amount {_fmt_money(amt)}; possible remaining exposure {_fmt_money(amt - lien)}.", [t], account))
        try:
            layer = int(t.get("layer") or 0)
        except (TypeError, ValueError):
            layer = 0
        if layer >= 3:
            flags.append(_flag("deep_layer_account", "medium", f"Transaction appears at Layer {layer}, indicating downstream movement.", [t], account))
        if amt >= high_threshold and (not t.get("utr") or not (t.get("ifsc") or t.get("upi"))):
            flags.append(_flag("large_transaction_missing_key_reference", "medium", "High-value row is missing UTR or IFSC/UPI reference; verify source sheet columns.", [t], account))

    # De-duplicate nearly identical single-row flags, keeping highest severity first.
    seen = set()
    unique_flags: List[Dict[str, Any]] = []
    flags.sort(key=lambda f: (-int(f.get("score") or 0), -float(f.get("total_amount") or 0), f.get("label") or ""))
    for f in flags:
        ids = tuple(sorted(int(t.get("id") or 0) for t in f.get("transactions") or []))[:5]
        key = (f.get("label"), f.get("account"), ids)
        if key in seen:
            continue
        seen.add(key)
        unique_flags.append(f)
    return unique_flags


def suspicious_answer(txns: List[Transaction], query: str) -> Optional[str]:
    q = (query or "").lower()
    wants = bool(re.search(r"\b(suspicious|flag|risk|red flag|mule|anomal|unusual|large|big|withdraw|same amount|short duration|rapid|repeated)\b", q))
    if not wants:
        return None
    if not txns:
        return "No structured transactions are stored yet, so suspicious-transaction analysis cannot be performed."

    filtered = filter_transactions(txns, query)
    if not filtered:
        return "No transactions matched that risk-analysis filter. Try an account number, bank, IFSC, UPI, UTR, or ask `flag suspicious accounts`."
    flags = find_suspicious_patterns(filtered, query)
    if not flags:
        return "No suspicious transaction patterns crossed the current rule thresholds for the selected filter. I checked high value, repeated accounts, duplicate UTRs, same-amount repetition, short-window activity, round amounts, lien gaps, cash/ATM remarks, and deep layers."

    account_summary: Dict[str, Dict[str, Any]] = {}
    for f in flags:
        acct = f.get("account") or "MULTIPLE/UNKNOWN"
        s = account_summary.setdefault(acct, {"score": 0, "flags": set(), "amount": 0.0, "rows": set()})
        s["score"] = max(s["score"], int(f.get("score") or 0))
        s["flags"].add(f.get("label"))
        s["amount"] += float(f.get("total_amount") or 0)
        for t in f.get("transactions") or []:
            if t.get("id"):
                s["rows"].add(t.get("id"))

    title = "Suspicious account / transaction flags"
    if _query_accounts(query) or _query_terms(query):
        title += " for the selected filter"
    lines = [title + ":"]
    lines.append(f"- Transactions checked: {len(filtered)} of {len(txns)} stored rows")
    lines.append(f"- Rules checked: high-value, repeated account, duplicate UTR, rapid same-amount, many small transfers, same amount across accounts, cash/ATM withdrawal, lien gap, deep layer, missing references")
    ranked_accounts = sorted(account_summary.items(), key=lambda kv: (-kv[1]["score"], -len(kv[1]["rows"]), kv[0]))
    if ranked_accounts:
        lines.append("Top flagged accounts:")
        for acct, s in ranked_accounts[:15]:
            sev = "CRITICAL" if s["score"] >= 90 else "HIGH" if s["score"] >= 70 else "MEDIUM" if s["score"] >= 50 else "LOW"
            lines.append(f"- `{acct}` | severity {sev} | {len(s['rows'])} related row(s) | flags: {', '.join(sorted(str(x) for x in s['flags']))}")
    lines.append("Most important flagged transactions/patterns:")
    for f in flags[:30]:
        rows = f.get("transactions") or []
        lines.append(f"- [{f.get('severity')}] {f.get('label')}" + (f" | account `{f.get('account')}`" if f.get("account") else "") + f" | {f.get('reason')}")
        for t in rows[:4]:
            lines.append(
                f"  - {t.get('txn_date') or 'undated'} | account `{_primary_account(t)}` | amount {_fmt_money(t.get('amount'))} | "
                f"UTR `{t.get('utr') or 'N/A'}` | bank {t.get('bank') or 'N/A'} | status {t.get('status') or 'N/A'} | {_source(t)}"
            )
    if len(flags) > 30:
        lines.append(f"... {len(flags) - 30} more flags omitted. Ask for a specific account timeline or export the analysis for full details.")
    return "\n".join(lines)


def investigator_question_answer(txns: List[Transaction], query: str) -> Optional[str]:
    """Route real-world account-risk questions to deterministic handlers."""
    timeline = account_timeline_answer(txns, query)
    if timeline is not None:
        return timeline
    suspicious = suspicious_answer(txns, query)
    if suspicious is not None:
        return suspicious
    return None
