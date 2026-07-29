"""Dashboard summary API used by the hero card and engine home view.

Returns lightweight, aggregate counts only (active investigations, evidence
count, pending tasks, recent investigations, storage usage). Kept deliberately
cheap so the hero card feels instant.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from investigation_app.services.case_service import CaseService

dashboard_bp = Blueprint("dashboard_api", __name__)


@dashboard_bp.route("/summary", methods=["GET"])
def summary():
    """Return aggregate stats for the hero card."""
    return jsonify(CaseService().dashboard_summary())
