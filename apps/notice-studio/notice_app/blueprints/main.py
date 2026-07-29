"""Main page routes (server-rendered shell)."""
from flask import Blueprint, current_app, render_template

from notice_app.models import ImportSession

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    session = ImportSession.query.order_by(ImportSession.id.desc()).first()
    return render_template(
        "index.html",
        sender_roles=current_app.config["SENDER_ROLES"],
        unsigned_role=current_app.config["UNSIGNED_ROLE"],
        has_session=session is not None,
        active_session=session,
    )
