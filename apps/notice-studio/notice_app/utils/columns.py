"""Canonical Excel column mapping and normalisation.

The importer is tolerant of header variations (case, spacing, punctuation).
Each internal field maps to a set of accepted header aliases. The Wallet/PG
account column is intentionally NOT mapped, so Account No (Column G) is always
used in the notice table.
"""

# internal_field -> list of accepted aliases (compared after normalisation).
COLUMN_ALIASES = {
    "acknowledgement_no": ["acknowledgement no", "acknowledgement no.", "ack no", "acknowledgment no"],
    "bank": ["bank/fis", "bank", "bank / fis", "bank fis"],
    "layer": ["layer"],
    "account_no": ["account no", "account no.", "account number"],
    "ifsc": ["ifsc code", "ifsc"],
    "transaction_date": ["transaction date"],
    "transaction_id": ["transaction id / utr number", "transaction id", "utr number", "transaction id/utr number"],
    "transaction_amount": ["transaction amount"],
    "reference_no": ["reference no", "reference no.", "reference number"],
    "company_email": [
        "company email", "bank email", "recipient email", "email", "email address"
    ],
    "remarks": ["remarks"],
    "action_taken": ["action taken by bank", "action taken"],
    "date_of_action": ["date of action"],
}

# Fields that MUST be present for the import to be usable.
REQUIRED_FIELDS = ["bank", "layer", "account_no", "ifsc", "transaction_amount"]


def normalise_header(header: str) -> str:
    if header is None:
        return ""
    h = str(header).strip().lower()
    h = h.replace("\u00a0", " ")
    while "  " in h:
        h = h.replace("  ", " ")
    return h


def build_header_map(headers):
    """Map internal fields to the actual column index in the sheet.

    Returns (field_to_index, missing_required, unmatched_headers).
    """
    normalised = [normalise_header(h) for h in headers]
    field_to_index = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                field_to_index[field] = normalised.index(alias)
                break
    missing_required = [f for f in REQUIRED_FIELDS if f not in field_to_index]
    matched_indexes = set(field_to_index.values())
    unmatched = [
        headers[i] for i in range(len(headers)) if i not in matched_indexes and normalised[i]
    ]
    return field_to_index, missing_required, unmatched
