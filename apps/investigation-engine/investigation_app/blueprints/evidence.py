"""Evidence intake / listing / entities / search API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from investigation_app.services.evidence_service import EvidenceService
from investigation_app.services import search_service

evidence_bp = Blueprint("evidence", __name__)


@evidence_bp.route("/<case_uid>/upload", methods=["POST"])
def upload(case_uid: str):
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400
    result = EvidenceService().add_upload(case_uid, request.files["file"])
    status = result.pop("status", 200)
    return jsonify(result), status


@evidence_bp.route("/<case_uid>", methods=["GET"])
def list_evidence(case_uid: str):
    return jsonify(EvidenceService().list_evidence(case_uid))


@evidence_bp.route("/<case_uid>/<int:evidence_id>/reprocess", methods=["POST"])
def reprocess(case_uid: str, evidence_id: int):
    """Queue one existing evidence item for reprocessing."""
    result = EvidenceService().reprocess_evidence(case_uid, evidence_id)
    status = result.pop("status", 200)
    return jsonify(result), status


@evidence_bp.route("/<case_uid>/reprocess-all", methods=["POST"])
def reprocess_all(case_uid: str):
    """Queue all existing evidence items in a case for reprocessing."""
    result = EvidenceService().reprocess_all(case_uid)
    status = result.pop("status", 200)
    return jsonify(result), status



@evidence_bp.route("/<case_uid>/<int:evidence_id>", methods=["DELETE"])
def delete_evidence(case_uid: str, evidence_id: int):
    """Remove one mistakenly uploaded evidence item from this case."""
    result = EvidenceService().delete_evidence(case_uid, evidence_id)
    status = result.pop("status", 200)
    return jsonify(result), status


@evidence_bp.route("/<case_uid>/entities", methods=["GET"])
def entities(case_uid: str):
    return jsonify(EvidenceService().get_entities(case_uid))


@evidence_bp.route("/<case_uid>/search", methods=["GET"])
def search(case_uid: str):
    query = request.args.get("q", "")
    return jsonify(search_service.search(case_uid, query))


@evidence_bp.route("/<case_uid>/graph", methods=["GET"])
def graph(case_uid: str):
    """Return the knowledge graph (entity nodes + co-occurrence edges)."""
    return jsonify(EvidenceService().get_graph(case_uid))


@evidence_bp.route("/<case_uid>/duplicates", methods=["GET"])
def duplicates(case_uid: str):
    """Return duplicate / similar / referenced evidence edges."""
    return jsonify(EvidenceService().get_duplicates(case_uid))


@evidence_bp.route("/<case_uid>/transactions", methods=["GET"])
def transactions(case_uid: str):
    """Return structured financial transaction rows for this case."""
    return jsonify(EvidenceService().get_transactions(case_uid))


@evidence_bp.route("/<case_uid>/messages", methods=["GET"])
def messages(case_uid: str):
    """Return structured communication/message records for this case."""
    return jsonify(EvidenceService().get_messages(case_uid))


@evidence_bp.route("/<case_uid>/social-profiles", methods=["GET"])
def social_profiles(case_uid: str):
    """Return extracted social profile/handle records for this case."""
    return jsonify(EvidenceService().get_social_profiles(case_uid))


@evidence_bp.route("/<case_uid>/technical-indicators", methods=["GET"])
def technical_indicators(case_uid: str):
    """Return technical/forensic indicators for this case."""
    return jsonify(EvidenceService().get_technical_indicators(case_uid))


@evidence_bp.route("/<case_uid>/<int:evidence_id>/intel", methods=["GET"])
def intel(case_uid: str, evidence_id: int):
    """Return stored structured intelligence for one evidence item."""
    result = EvidenceService().get_intel(case_uid, evidence_id)
    if result.get("error"):
        return jsonify(result), 404
    return jsonify(result)


@evidence_bp.route("/<case_uid>/<int:evidence_id>/stages", methods=["GET"])
def stages(case_uid: str, evidence_id: int):
    """Return per-stage processing status for one evidence item."""
    return jsonify(EvidenceService().get_stages(evidence_id))
