"""Bulk DOCX and ready-to-send EML generation with progress tracking."""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
import uuid
import zipfile

from datetime import datetime
from email.message import EmailMessage

from flask import current_app

from notice_app.services.docx_engine import render_document
from notice_app.utils.sanitize import sanitize_filename, sanitize_folder

logger = logging.getLogger(__name__)

# In-memory job registry for progress polling (single-user local app).
_JOBS = {}
_LOCK = threading.Lock()


def new_job() -> str:
    job_id = uuid.uuid4().hex
    with _LOCK:
        _JOBS[job_id] = {
            "state": "preparing", "total": 0, "done": 0,
            "generated": 0, "email_drafts": 0, "missing_emails": 0,
            "skipped": 0, "errored": 0,
            "log": [], "zip_path": None, "error": None,
        }
    return job_id


def get_job(job_id: str):
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _update(job_id, **kwargs):
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(kwargs)


def _append_log(job_id, entry):
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id]["log"].append(entry)


def _build_filename(record) -> str:
    """ReferenceName_Bank_AccountNo.docx (sanitised).

    The reference name leads so notices are named after the person they are
    addressed to. Bank + account keep filenames unique when an reference has
    more than one account.
    """
    parts = [
        sanitize_filename(record.reference_name, "Reference"),
        sanitize_filename(record.bank, "Bank"),
        sanitize_filename(record.account_no, "Account"),
    ]
    return "_".join(parts) + ".docx"


def _build_email(record, subject: str, document_bytes: bytes,
                 document_name: str, sender_name: str, sender_role: str) -> bytes:
    """Create a local RFC 5322 draft with the generated notice attached."""
    message = EmailMessage()
    message["X-Unsent"] = "1"
    recipient = (record.company_email or "").strip()
    if recipient:
        message["To"] = recipient
    message["Subject"] = f"{subject} - {record.bank or 'Company'}"
    sender_lines = [value for value in (sender_name, sender_role) if value]
    closing = "\n".join(sender_lines) if sender_lines else "Authorized sender"
    body = (
        f"Hello {record.bank or 'team'},\n\n"
        "Please review the attached notice for the referenced transaction. "
        "The document contains the available account, routing and transaction "
        "details for your review.\n\n"
        f"Reference name: {record.reference_name or 'Not provided'}\n"
        f"Reference number: {record.reference_no or record.acknowledgement_no or 'Not provided'}\n"
        f"Transaction ID: {record.transaction_id or 'Not provided'}\n\n"
        "Please reply to the sender with the requested information or the "
        "appropriate next contact. This is an administrative information "
        "request and not a legal order.\n\n"
        f"Regards,\n{closing}\n"
    )
    message.set_content(body)
    message.add_attachment(
        document_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=document_name,
    )
    return message.as_bytes()


def generate_zip(job_id, record_ids, template_path, output_folder,
                 sender_name, sender_role, unsigned_role,
                 zip_basename):
    """Generate one DOCX per record, organised by Layer/Bank, then ZIP.

    Runs in a background thread; progress is polled via get_job().
    Records are re-loaded inside the active app context (set by the caller)
    so the ORM instances are bound to this thread's session and status
    writes commit correctly.
    """
    from notice_app.models import Record

    try:
        records = (
            Record.query.filter(Record.id.in_(record_ids))
            .order_by(Record.row_index)
            .all()
        )
        date_value = datetime.now().strftime(
            current_app.config.get("DATE_FORMAT", "%d/%m/%Y")
        )
        subject_value = current_app.config.get("DEFAULT_SUBJECT", "")
        total = len(records)
        logger.info("Generating %d notice and email draft pairs", total)
        _update(job_id, state="generating", total=total)
        buffer = io.BytesIO()
        manifest_rows = []

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for record in records:
                try:
                    eff_name = record.sender_name or sender_name
                    eff_role = record.sender_role or sender_role
                    document = render_document(
                        template_path, record.to_dict(), eff_name, eff_role,
                        unsigned_role,
                        date_value=date_value, subject_value=subject_value,
                    )
                    layer_folder = "Layer_" + sanitize_folder(record.layer, "Unknown")
                    bank_folder = sanitize_folder(record.bank, "Unknown_Bank")
                    filename = _build_filename(record)
                    arcname = f"Generated_Notices/{layer_folder}/{bank_folder}/{filename}"

                    doc_buf = io.BytesIO()
                    document.save(doc_buf)
                    document_bytes = doc_buf.getvalue()
                    zf.writestr(arcname, document_bytes)

                    email_name = os.path.splitext(filename)[0] + ".eml"
                    email_arcname = (
                        f"Email_Drafts/{layer_folder}/{bank_folder}/{email_name}"
                    )
                    zf.writestr(
                        email_arcname,
                        _build_email(
                            record, subject_value, document_bytes, filename,
                            "" if eff_role == unsigned_role else eff_name,
                            "" if eff_role == unsigned_role else eff_role,
                        ),
                    )
                    manifest_rows.append({
                        "row": record.row_index,
                        "reference_name": record.reference_name,
                        "company": record.bank,
                        "company_email": record.company_email,
                        "document": arcname,
                        "email_draft": email_arcname,
                    })

                    record.status = "generated"
                    _append_log(job_id, {
                        "row": record.row_index, "file": arcname,
                        "email": email_arcname, "status": "ok",
                    })
                    with _LOCK:
                        _JOBS[job_id]["generated"] += 1
                        _JOBS[job_id]["email_drafts"] += 1
                        if not (record.company_email or "").strip():
                            _JOBS[job_id]["missing_emails"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to generate notice for row %s",
                                     record.row_index)
                    _append_log(job_id, {"row": record.row_index,
                                          "status": "error", "message": str(exc)})
                    with _LOCK:
                        _JOBS[job_id]["errored"] += 1
                finally:
                    with _LOCK:
                        _JOBS[job_id]["done"] += 1

            manifest = io.StringIO()
            writer = csv.DictWriter(
                manifest,
                fieldnames=[
                    "row", "reference_name", "company", "company_email",
                    "document", "email_draft",
                ],
            )
            writer.writeheader()
            writer.writerows(manifest_rows)
            zf.writestr("delivery_manifest.csv", manifest.getvalue().encode("utf-8"))

        _update(job_id, state="packaging")
        os.makedirs(output_folder, exist_ok=True)
        safe_base = sanitize_filename(zip_basename, "Bulk_Notices")
        zip_path = os.path.join(output_folder, f"{safe_base}.zip")
        with open(zip_path, "wb") as fh:
            fh.write(buffer.getvalue())

        _update(job_id, state="ready", zip_path=zip_path)
        logger.info("ZIP ready at %s", zip_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ZIP generation failed")
        _update(job_id, state="failed", error=str(exc))


def start_generation(app, **kwargs):
    """Spawn the background generation thread with app context."""
    job_id = kwargs.pop("job_id")

    def _runner():
        with app.app_context():
            generate_zip(job_id=job_id, **kwargs)
            from notice_app.models import db
            db.session.commit()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return job_id
