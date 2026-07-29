"""Application factory for the Notice Studio."""
import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template

from notice_app.config import Config
from notice_app.models import db


def _configure_logging(app: Flask) -> None:
    log_dir = os.path.join(app.instance_path, "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=2_000_000, backupCount=5
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info("Logging initialised")


def create_app(config_class: type = Config) -> Flask:
    # Vercel's deployed source bundle is read-only. Point Flask's own instance
    # directory at the same writable runtime location as uploads and SQLite.
    instance_path = os.path.abspath(
        getattr(config_class, "INSTANCE_PATH", Config.INSTANCE_PATH)
    )
    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=instance_path,
    )
    app.config.from_object(config_class)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    _configure_logging(app)

    # Register blueprints (modular feature areas).
    from notice_app.blueprints.main import main_bp
    from notice_app.blueprints.upload import upload_bp
    from notice_app.blueprints.records import records_bp
    from notice_app.blueprints.generate import generate_bp
    from notice_app.blueprints.session_mgmt import session_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp, url_prefix="/api/upload")
    app.register_blueprint(records_bp, url_prefix="/api/records")
    app.register_blueprint(generate_bp, url_prefix="/api/generate")
    app.register_blueprint(session_bp, url_prefix="/api/session")

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Unhandled server error: %s", e)
        return render_template("errors/500.html"), 500

    app.logger.info("Notice Studio initialized")
    return app
