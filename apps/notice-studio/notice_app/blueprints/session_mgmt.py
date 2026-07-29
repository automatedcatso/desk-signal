"""Session persistence: signatory settings and clear-session."""
from flask import Blueprint, jsonify, request

from notice_app.models import ImportSession, db

session_bp = Blueprint("session_mgmt", __name__)


def _active_session():
    return ImportSession.query.order_by(ImportSession.id.desc()).first()


@session_bp.route("/signatory", methods=["POST"])
def signatory():
    """Save the optional global sender identity for all notices."""
    session = _active_session()
    if not session:
        return jsonify({"ok": False, "error": "No active session."}), 404
    data = request.get_json(silent=True) or {}
    session.sender_role = (data.get("sender_role") or "").strip()
    # The renderer suppresses the name when the user chooses No signature.
    session.sender_name = (data.get("sender_name") or "").strip()
    db.session.commit()
    return jsonify({"ok": True, "sender_name": session.sender_name,
                    "sender_role": session.sender_role})


@session_bp.route("/clear", methods=["POST"])
def clear():
    """Explicit Clear Session action: wipe imported data."""
    ImportSession.query.delete()
    db.session.commit()
    return jsonify({"ok": True})
