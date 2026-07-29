"""Case (investigation) CRUD API.

All persistence goes through :class:`CaseService`; blueprints stay thin.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from investigation_app.services.case_service import CaseService

cases_bp = Blueprint("cases", __name__)


@cases_bp.route("", methods=["GET"])
def list_cases():
    return jsonify(CaseService().list_cases())


@cases_bp.route("", methods=["POST"])
def create_case():
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    case = CaseService().create_case(
        title=title,
        reference_no=(payload.get("reference_no") or "").strip() or None,
        ai_mode=(payload.get("ai_mode") or "standard").strip(),
    )
    return jsonify(case), 201


@cases_bp.route("/<uid>", methods=["GET"])
def get_case(uid: str):
    case = CaseService().get_case(uid)
    if case is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(case)


@cases_bp.route("/<uid>", methods=["PUT"])
def update_case(uid: str):
    payload = request.get_json(silent=True) or {}
    case = CaseService().update_case(uid, payload)
    if case is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(case)


@cases_bp.route("/<uid>/close", methods=["DELETE"])
def close_case(uid: str):
    """Permanently close an investigation and clear all associated local data."""
    result = CaseService().close_investigation(uid)
    status = result.pop("status", 200)
    return jsonify(result), status


@cases_bp.route("/<uid>", methods=["DELETE"])
def delete_case(uid: str):
    """Alias for close_case so clients can use normal DELETE semantics."""
    result = CaseService().close_investigation(uid)
    status = result.pop("status", 200)
    return jsonify(result), status
