"""Application factory for the Investigation Intelligence Engine (IIE).

This Flask app is a fully isolated portal module. It talks to other modules
through explicit adapters and keeps cloud analysis opt-in.

CORS is restricted to the known local portal origins (ports 5000-5005) so the
engine can be embedded in the dashboard iframe and reach the local AI assistant
backend on :5003. The app binds to 127.0.0.1 and has no telemetry. Outbound
requests occur only when a user explicitly selects the configured Gemini
provider for Smart or Deep analysis.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify

try:  # CORS is only needed for cross-origin /api calls; keep boot resilient.
    from flask_cors import CORS
except ImportError:  # pragma: no cover - defensive: never fail to boot
    CORS = None

from investigation_app.config import load_config
from investigation_app.extensions import init_db, instance_dir

# Local portal origins allowed to talk to this backend. Requests from any
# other origin (or a file:// page, which sends "Origin: null") are rejected.
ALLOWED_ORIGINS = [
    f"http://{host}:{port}"
    for host in ("127.0.0.1", "localhost")
    for port in (5000, 5001, 5002, 5003, 5004, 5005)
]


def _configure_logging(app: Flask) -> None:
    log_dir = os.path.join(instance_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(log_dir, "iie.log"), maxBytes=2_000_000, backupCount=5
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    # Dedicated audit log (security requirement): every mutating action.
    audit = logging.getLogger("iie_audit")
    audit_handler = RotatingFileHandler(
        os.path.join(log_dir, "audit.log"), maxBytes=2_000_000, backupCount=10
    )
    audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    audit.addHandler(audit_handler)
    audit.setLevel(logging.INFO)


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    cfg = load_config()
    app.config["IIE"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = (
        int(cfg.get("limits", {}).get("max_upload_mb", 32)) * 1024 * 1024
    )

    if CORS is not None:
        CORS(
            app,
            resources={
                r"/health": {"origins": ALLOWED_ORIGINS},
                r"/api/*": {"origins": ALLOWED_ORIGINS},
            },
            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type"],
            supports_credentials=False,
        )

    _configure_logging(app)
    if CORS is None:
        app.logger.warning(
            "flask-cors not installed; cross-origin /api calls may be blocked. "
            "Install apps/investigation-engine/requirements.txt."
        )
    init_db()

    from investigation_app.blueprints.ui import ui_bp
    from investigation_app.blueprints.dashboard_api import dashboard_bp
    from investigation_app.blueprints.cases import cases_bp
    from investigation_app.blueprints.evidence import evidence_bp
    from investigation_app.blueprints.ai import ai_bp
    from investigation_app.blueprints.workspace import workspace_bp

    app.register_blueprint(ui_bp)
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    app.register_blueprint(cases_bp, url_prefix="/api/cases")
    app.register_blueprint(evidence_bp, url_prefix="/api/evidence")
    app.register_blueprint(ai_bp, url_prefix="/api/ai")
    app.register_blueprint(workspace_bp, url_prefix="/api/workspace")

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"ok": True, "module": "investigation-intelligence-engine"})

    app.logger.info("Investigation Intelligence Engine initialised")
    return app
