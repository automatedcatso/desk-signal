"""Excel upload + parsing endpoint."""
import os

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from notice_app.models import ImportSession, Record, db
from notice_app.services.excel_parser import ExcelImportError, parse_workbook
from notice_app.services.validation import apply_validation

upload_bp = Blueprint("upload", __name__)


def _allowed(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


@upload_bp.route("", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided."}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "No file selected."}), 400
    if not _allowed(file.filename):
        return jsonify({"ok": False, "error": "Only .xlsx files are accepted."}), 400

    filename = secure_filename(file.filename)
    save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
    file.save(save_path)

    try:
        records, summary = parse_workbook(save_path)
    except ExcelImportError as exc:
        return jsonify({
            "ok": False, "error": str(exc),
            "missing": exc.missing, "unmatched": exc.unmatched,
        }), 422

    # Replace any prior session (single active workflow).
    ImportSession.query.delete()
    db.session.commit()

    session = ImportSession(filename=filename, total_records=len(records))
    db.session.add(session)
    db.session.flush()
    for rec in records:
        rec.session_id = session.id
        db.session.add(rec)
    db.session.flush()

    apply_validation(session.records.all())
    db.session.commit()

    return jsonify({"ok": True, "session_id": session.id, "summary": summary})
