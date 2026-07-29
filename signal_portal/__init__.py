"""Signal Desk launcher application."""
from __future__ import annotations

from flask import Flask, render_template


def create_portal_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "product": "signal-desk",
            "tools": ["notice-studio", "investigation-engine"],
        }

    return app
