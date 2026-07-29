"""UI routes for the Investigation Intelligence Engine.

Serves the hero/workspace page. The page is designed to be loaded inside the
portal dashboard's existing modal iframe over HTTP (never file://), matching
the convention used by the other modules.
"""
from __future__ import annotations

from flask import Blueprint, render_template

ui_bp = Blueprint("ui", __name__)


@ui_bp.route("/")
def index():
    """Render the engine workspace shell."""
    return render_template("engine.html")
