"""Excel import service: read .xlsx, map columns, persist records."""
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Tuple

import openpyxl

from notice_app.models import Record
from notice_app.utils.columns import build_header_map

logger = logging.getLogger(__name__)


class ExcelImportError(Exception):
    """Raised when the workbook cannot be used (missing required columns)."""

    def __init__(self, message: str, missing=None, unmatched=None):
        super().__init__(message)
        self.missing = missing or []
        self.unmatched = unmatched or []


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_workbook(file_path: str) -> Tuple[list, dict]:
    """Parse the workbook and return (records, summary).

    Raises ExcelImportError if required columns are missing.
    """
    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI.
        logger.exception("Failed to open workbook")
        raise ExcelImportError(f"Could not read the Excel file: {exc}") from exc

    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration as exc:
        raise ExcelImportError("The Excel file is empty.") from exc

    headers = [_cell_to_str(h) for h in header_row]
    field_to_index, missing, unmatched = build_header_map(headers)

    if missing:
        wb.close()
        raise ExcelImportError(
            "Required columns are missing or renamed: " + ", ".join(missing),
            missing=missing,
            unmatched=unmatched,
        )

    records = []
    row_index = 0
    for raw in rows:
        if raw is None or all(c is None or _cell_to_str(c) == "" for c in raw):
            continue
        row_index += 1

        def get(field):
            idx = field_to_index.get(field)
            if idx is None or idx >= len(raw):
                return ""
            return _cell_to_str(raw[idx])

        record = Record(
            row_index=row_index,
            acknowledgement_no=get("acknowledgement_no"),
            bank=get("bank"),
            layer=get("layer"),
            account_no=get("account_no"),
            ifsc=get("ifsc"),
            transaction_date=get("transaction_date"),
            transaction_id=get("transaction_id"),
            transaction_amount=get("transaction_amount"),
            reference_no=get("reference_no"),
            company_email=get("company_email"),
            remarks=get("remarks"),
            action_taken=get("action_taken"),
            date_of_action=get("date_of_action"),
            reference_name="",
            status="missing",
        )
        records.append(record)

    wb.close()

    summary = {
        "total_records": len(records),
        "unmatched_headers": unmatched,
        "mapped_fields": list(field_to_index.keys()),
    }
    logger.info("Parsed %d records from %s", len(records), file_path)
    return records, summary
