"""Workspace state + settings API (autosave / restore / guided-mode flag)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from investigation_app.services import workspace_service

workspace_bp = Blueprint("workspace", __name__)


@workspace_bp.route("/<case_uid>", methods=["GET"])
def load(case_uid: str):
    return jsonify(workspace_service.load(case_uid))


@workspace_bp.route("/<case_uid>", methods=["PUT"])
def save(case_uid: str):
    state = request.get_json(silent=True) or {}
    ok = workspace_service.save(case_uid, state)
    return jsonify({"ok": ok})


@workspace_bp.route("/settings/<key>", methods=["GET"])
def get_setting(key: str):
    return jsonify({"key": key, "value": workspace_service.get_setting(key)})


@workspace_bp.route("/settings/<key>", methods=["PUT"])
def set_setting(key: str):
    payload = request.get_json(silent=True) or {}
    workspace_service.set_setting(key, str(payload.get("value", "")))
    return jsonify({"ok": True})
