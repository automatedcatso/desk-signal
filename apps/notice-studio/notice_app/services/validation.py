"""Record validation and duplicate detection."""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import List


def validate_record(record) -> List[str]:
    """Return a list of validation error messages for one record."""
    errors = []
    if not (record.bank or "").strip():
        errors.append("Missing Bank")
    if not (record.ifsc or "").strip():
        errors.append("Missing IFSC")
    if not (record.layer or "").strip():
        errors.append("Invalid/Missing Layer")
    if not (record.account_no or "").strip():
        errors.append("Missing Account No")
    if not (record.transaction_amount or "").strip():
        errors.append("Missing Transaction Amount")
    email = (record.company_email or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors.append("Invalid Company Email")
    return errors


def compute_status(record) -> str:
    """Derive the workflow status colour.

    missing -> red (reference name missing)
    ready   -> yellow (complete, awaiting generation)
    generated-> green (handled after generation)
    Validation errors keep a record out of 'ready'.
    """
    if validate_record(record):
        return "error"
    if not (record.reference_name or "").strip():
        return "missing"
    if record.status == "generated":
        return "generated"
    return "ready"


def detect_duplicates(records) -> dict:
    """Flag duplicate Account No + Transaction ID combinations.

    Returns {record_id: 'Duplicate ...'} for affected records.
    """
    keys = [
        (r.account_no.strip(), r.transaction_id.strip())
        for r in records
        if r.account_no
    ]
    counts = Counter(keys)
    flagged = {}
    for r in records:
        key = (r.account_no.strip(), r.transaction_id.strip())
        if r.account_no and counts.get(key, 0) > 1:
            flagged[r.id] = "Duplicate Account No + Transaction ID"
    return flagged


def apply_validation(records) -> dict:
    """Validate all records in place, set status + validation_errors.

    Returns a report dict summarising issues.
    """
    duplicates = detect_duplicates(records)
    report = {
        "errors": [], "duplicates": [], "missing_names": 0,
        "missing_emails": 0, "ready": 0,
    }
    for r in records:
        errs = validate_record(r)
        if r.id in duplicates:
            errs = errs + [duplicates[r.id]]
        r.validation_errors = json.dumps(errs)
        r.status = compute_status(r)
        if errs:
            report["errors"].append({"row": r.row_index, "account_no": r.account_no,
                                      "issues": errs})
        if r.id in duplicates:
            report["duplicates"].append(r.row_index)
        if r.status == "missing":
            report["missing_names"] += 1
        if not (r.company_email or "").strip():
            report["missing_emails"] += 1
        if r.status == "ready":
            report["ready"] += 1
    return report
