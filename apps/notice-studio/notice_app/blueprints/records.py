"""Record listing, filtering, search, stats, save and preview endpoints."""
import json

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import or_

from notice_app.models import ImportSession, Record, db
from notice_app.services.docx_engine import extract_preview_lines
from notice_app.services.validation import apply_validation, compute_status

records_bp = Blueprint("records", __name__)


def _active_session():
    return ImportSession.query.order_by(ImportSession.id.desc()).first()


@records_bp.route("/stats", methods=["GET"])
def stats():
    session = _active_session()
    if not session:
        return jsonify({"ok": False, "error": "No active session."}), 404
    records = session.records.all()
    total = len(records)
    banks = {r.bank for r in records if r.bank}
    layers = {r.layer for r in records if r.layer}

    def _amount(v):
        try:
            return float(str(v).replace(",", "").replace("\u20b9", "").strip() or 0)
        except ValueError:
            return 0.0

    total_amount = sum(_amount(r.transaction_amount) for r in records)
    pending = sum(1 for r in records if r.status == "missing")
    ready = sum(1 for r in records if r.status in ("ready", "generated"))
    completion = round((ready / total) * 100, 1) if total else 0.0
    email_ready = sum(1 for r in records if (r.company_email or "").strip())
    return jsonify({
        "ok": True,
        "total_records": total,
        "total_banks": len(banks),
        "total_layers": len(layers),
        "total_amount": total_amount,
        "pending_names": pending,
        "ready_notices": ready,
        "email_ready": email_ready,
        "completion": completion,
        "sender_name": session.sender_name,
        "sender_role": session.sender_role,
    })


@records_bp.route("/filters", methods=["GET"])
def filters():
    """Return unique layers, and banks for an (optional) selected layer."""
    session = _active_session()
    if not session:
        return jsonify({"ok": False, "error": "No active session."}), 404
    layer = request.args.get("layer", "").strip()
    layer_q = db.session.query(Record.layer).filter(
        Record.session_id == session.id, Record.layer != ""
    ).distinct()
    layers = sorted({row[0] for row in layer_q})

    bank_q = db.session.query(Record.bank).filter(
        Record.session_id == session.id, Record.bank != ""
    )
    if layer:
        bank_q = bank_q.filter(Record.layer == layer)
    banks = sorted({row[0] for row in bank_q.distinct()})
    return jsonify({"ok": True, "layers": layers, "banks": banks})


@records_bp.route("/list", methods=["GET"])
def list_records():
    session = _active_session()
    if not session:
        return jsonify({"ok": False, "error": "No active session."}), 404

    layer = request.args.get("layer", "").strip()
    bank = request.args.get("bank", "").strip()
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip()
    sort = request.args.get("sort", "row_index")
    direction = request.args.get("dir", "asc")
    page = max(1, int(request.args.get("page", 1)))
    per_page = int(request.args.get("per_page", current_app.config["PAGE_SIZE"]))

    query = Record.query.filter(Record.session_id == session.id)
    if layer:
        query = query.filter(Record.layer == layer)
    if bank:
        query = query.filter(Record.bank == bank)
    if status:
        query = query.filter(Record.status == status)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            Record.account_no.ilike(like),
            Record.acknowledgement_no.ilike(like),
            Record.transaction_id.ilike(like),
            Record.reference_name.ilike(like),
            Record.ifsc.ilike(like),
            Record.reference_no.ilike(like),
            Record.company_email.ilike(like),
        ))

    sort_col = getattr(Record, sort, Record.row_index)
    sort_col = sort_col.desc() if direction == "desc" else sort_col.asc()
    query = query.order_by(sort_col)

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        "ok": True,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "records": [r.to_dict() for r in items],
    })


@records_bp.route("/<int:record_id>", methods=["GET"])
def get_record(record_id):
    record = Record.query.get_or_404(record_id)
    return jsonify({"ok": True, "record": record.to_dict()})


@records_bp.route("/<int:record_id>", methods=["PATCH"])
def update_record(record_id):
    record = Record.query.get_or_404(record_id)
    data = request.get_json(silent=True) or {}
    if "reference_name" in data:
        record.reference_name = (data["reference_name"] or "").strip()
    if "sender_name" in data:
        record.sender_name = (data["sender_name"] or "").strip()
    if "sender_role" in data:
        record.sender_role = (data["sender_role"] or "").strip()
    if "company_email" in data:
        record.company_email = (data["company_email"] or "").strip()
    record.status = compute_status(record)
    db.session.commit()
    return jsonify({"ok": True, "record": record.to_dict()})


@records_bp.route("/bulk-name", methods=["POST"])
def bulk_name():
    """Assign the same reference name to multiple selected records."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    name = (data.get("reference_name") or "").strip()
    if not ids or not name:
        return jsonify({"ok": False, "error": "ids and reference_name required."}), 400
    records = Record.query.filter(Record.id.in_(ids)).all()
    for r in records:
        r.reference_name = name
        r.status = compute_status(r)
    db.session.commit()
    return jsonify({"ok": True, "updated": len(records)})


@records_bp.route("/preview/<int:record_id>", methods=["GET"])
def preview(record_id):
    record = Record.query.get_or_404(record_id)
    session = _active_session()
    sender_name = request.args.get("sender_name")
    sender_role = request.args.get("sender_role")
    if sender_name is None:
        sender_name = record.sender_name or (session.sender_name if session else "")
    if sender_role is None:
        sender_role = record.sender_role or (session.sender_role if session else "")

    data = record.to_dict()
    if (data.get("reference_name") or "") == "":
        # Show the live-typed name without persisting via this GET.
        typed = request.args.get("reference_name")
        if typed is not None:
            data["reference_name"] = typed
    typed_email = request.args.get("company_email")
    if typed_email is not None:
        data["company_email"] = typed_email

    from datetime import datetime

    date_value = datetime.now().strftime(
        current_app.config.get("DATE_FORMAT", "%d/%m/%Y")
    )
    subject_value = current_app.config.get("DEFAULT_SUBJECT", "")
    preview_data = extract_preview_lines(
        current_app.config["TEMPLATE_DOCX"], data, sender_name, sender_role,
        current_app.config["UNSIGNED_ROLE"],
        date_value=date_value, subject_value=subject_value,
    )
    return jsonify({"ok": True, "preview": preview_data})


@records_bp.route("/error-report", methods=["GET"])
def error_report():
    session = _active_session()
    if not session:
        return jsonify({"ok": False, "error": "No active session."}), 404
    records = session.records.all()
    report = apply_validation(records)
    db.session.commit()
    return jsonify({"ok": True, "report": report})
