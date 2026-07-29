"""Bulk generation + ZIP download endpoints."""
import os

from flask import (Blueprint, current_app, jsonify, request, send_file)

from notice_app.models import ImportSession, Record
from notice_app.services import zip_builder

generate_bp = Blueprint("generate", __name__)


def _active_session():
    return ImportSession.query.order_by(ImportSession.id.desc()).first()


@generate_bp.route("/start", methods=["POST"])
def start():
    session = _active_session()
    if not session:
        return jsonify({"ok": False, "error": "No active session."}), 404
    data = request.get_json(silent=True) or {}
    selected_ids = data.get("ids")  # None => generate all.
    zip_basename = (data.get("zip_name") or "Notice_Pack").strip()

    # Accept the live global signatory typed top-left so the user does not need
    # to click "Save Signatory" first. Persist it onto the session.
    if "sender_name" in data or "sender_role" in data:
        if "sender_role" in data:
            session.sender_role = (data.get("sender_role") or "").strip()
        if "sender_name" in data:
            session.sender_name = (data.get("sender_name") or "").strip()
        from notice_app.models import db
        db.session.commit()

    unsigned = current_app.config["UNSIGNED_ROLE"]
    require_name = session.sender_role != unsigned

    query = Record.query.filter(Record.session_id == session.id)
    if selected_ids:
        query = query.filter(Record.id.in_(selected_ids))
    records = query.order_by(Record.row_index).all()

    if not records:
        return jsonify({"ok": False, "error": "No records to generate."}), 400

    # Validation gate: every reference name filled, no blocking errors.
    incomplete = [r.row_index for r in records if not (r.reference_name or "").strip()]
    if incomplete:
        return jsonify({
            "ok": False,
            "error": "All reference names must be filled before generation.",
            "incomplete_rows": incomplete,
        }), 422

    errored = [r.row_index for r in records if r.status == "error"]
    if errored:
        return jsonify({
            "ok": False,
            "error": "Resolve validation errors before generation.",
            "errored_rows": errored,
        }), 422

    # A signature can be global, set per record, or intentionally omitted.
    if require_name and not (session.sender_name or "").strip():
        missing_sender = [
            r.row_index for r in records if not (r.sender_name or "").strip()
        ]
        if missing_sender:
            return jsonify({
                "ok": False,
                "error": "Enter a sender name for the selected role, set it "
                         "per notice, or choose No signature.",
            }), 422

    record_ids = [r.id for r in records]

    job_id = zip_builder.new_job()
    zip_builder.start_generation(
        current_app._get_current_object(),
        job_id=job_id,
        record_ids=record_ids,
        template_path=current_app.config["TEMPLATE_DOCX"],
        output_folder=current_app.config["OUTPUT_FOLDER"],
        sender_name=session.sender_name,
        sender_role=session.sender_role,
        unsigned_role=unsigned,
        zip_basename=zip_basename,
    )
    return jsonify({"ok": True, "job_id": job_id})


@generate_bp.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    job = zip_builder.get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Unknown job."}), 404
    payload = {k: v for k, v in job.items() if k != "zip_path"}
    payload["ok"] = True
    payload["has_zip"] = bool(job.get("zip_path"))
    return jsonify(payload)


@generate_bp.route("/download/<job_id>", methods=["GET"])
def download(job_id):
    job = zip_builder.get_job(job_id)
    if not job or not job.get("zip_path") or not os.path.exists(job["zip_path"]):
        return jsonify({"ok": False, "error": "ZIP not ready."}), 404
    return send_file(job["zip_path"], as_attachment=True,
                     download_name=os.path.basename(job["zip_path"]))
