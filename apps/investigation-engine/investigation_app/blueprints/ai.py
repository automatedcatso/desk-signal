"""AI, timeline and report API endpoints."""
from __future__ import annotations

import io

from flask import Blueprint, Response, jsonify, request, send_file

from investigation_app.services import ai_service, report_service, timeline_service

ai_bp = Blueprint("ai", __name__)


@ai_bp.route("/<case_uid>/ask", methods=["POST"])
def ask(case_uid: str):
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    mode = payload.get("mode")
    provider = payload.get("provider")
    return jsonify(ai_service.ask(case_uid, query, mode, provider))


@ai_bp.route("/<case_uid>/history", methods=["GET"])
def history(case_uid: str):
    return jsonify(ai_service.history(case_uid))


@ai_bp.route("/<case_uid>/history", methods=["DELETE"])
def clear_history(case_uid: str):
    result = ai_service.clear_history(case_uid)
    if result.get("error"):
        return jsonify(result), 404
    return jsonify(result)


@ai_bp.route("/status", methods=["GET"])
def status():
    return jsonify(ai_service.provider_status())


@ai_bp.route("/diagnostics", methods=["GET"])
def diagnostics():
    """Self-test each IIE subsystem and report pass/fail.

    Every check is isolated so a single failure never aborts the rest of the
    report. Powers an end-to-end connectivity check from within the engine.
    """
    from investigation_app.extensions import get_connection

    results = {}

    # 1. Backend is up (this endpoint answered).
    results["backend"] = {"ok": True, "detail": "IIE backend is running."}

    # 2. Database read/write.
    try:
        conn = get_connection()
        try:
            n = conn.execute("SELECT COUNT(*) AS c FROM cases").fetchone()["c"]
            results["database"] = {"ok": True, "detail": f"{n} case(s) in store."}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        results["database"] = {"ok": False, "detail": str(exc)}

    # 3. FTS5 search index availability.
    try:
        conn = get_connection()
        try:
            conn.execute(
                "SELECT ref_id FROM search_index "
                "WHERE search_index MATCH ? LIMIT 1",
                ('"selftest"*',),
            ).fetchall()
            results["search_index"] = {"ok": True, "detail": "FTS5 index is queryable."}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        results["search_index"] = {"ok": False, "detail": str(exc)}

    # 4. Background job pool.
    try:
        from investigation_app.services import jobs
        results["jobs"] = {
            "ok": jobs._executor is not None,
            "detail": "Background job pool is initialised.",
        }
    except Exception as exc:  # noqa: BLE001
        results["jobs"] = {"ok": False, "detail": str(exc)}

    # 5. Optional AI providers. STANDARD mode works without either one.
    provider_state = ai_service.provider_status()["providers"]
    results["local_ai"] = {
        "ok": provider_state["local"]["available"],
        "detail": provider_state["local"]["detail"],
    }
    results["gemini"] = {
        "ok": provider_state["gemini"]["available"],
        "detail": provider_state["gemini"]["detail"],
    }

    # AI providers are optional; overall health ignores them.
    overall = all(
        v["ok"] for k, v in results.items() if k not in {"local_ai", "gemini"}
    )
    return jsonify({"ok": overall, "results": results})


@ai_bp.route("/<case_uid>/timeline", methods=["GET"])
def timeline(case_uid: str):
    return jsonify(timeline_service.list_events(case_uid))


@ai_bp.route("/<case_uid>/timeline/rebuild", methods=["POST"])
def timeline_rebuild(case_uid: str):
    return jsonify({"events": timeline_service.rebuild(case_uid)})


@ai_bp.route("/<case_uid>/report", methods=["GET"])
def report(case_uid: str):
    kind = request.args.get("kind", "investigation")
    fmt = (request.args.get("format") or "md").lower()
    download = request.args.get("download") in {"1", "true", "yes"}
    base_name = f"iie_{case_uid}_{kind}"

    try:
        if fmt in {"json", "js"}:
            payload = report_service.build_json(case_uid, kind)
            if payload is None:
                return jsonify({"error": "case not found"}), 404
            headers = {}
            if download:
                headers["Content-Disposition"] = f"attachment; filename={base_name}.json"
            return Response(payload, mimetype="application/json", headers=headers)

        if fmt == "pdf":
            data = report_service.build_pdf_bytes(case_uid, kind)
            if data is None:
                return jsonify({"error": "case not found"}), 404
            return send_file(
                io.BytesIO(data),
                mimetype="application/pdf",
                as_attachment=download,
                download_name=f"{base_name}.pdf",
            )

        if fmt == "docx":
            data = report_service.build_docx_bytes(case_uid, kind)
            if data is None:
                return jsonify({"error": "case not found"}), 404
            return send_file(
                io.BytesIO(data),
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                as_attachment=download,
                download_name=f"{base_name}.docx",
            )

        md = report_service.build_report(case_uid, kind)
        if md is None:
            return jsonify({"error": "case not found"}), 404
        headers = {}
        if download:
            headers["Content-Disposition"] = f"attachment; filename={base_name}.md"
        return Response(md, mimetype="text/markdown", headers=headers)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 501
