"""Combined WSGI entry point for local hosting and Vercel."""
from __future__ import annotations

import sys
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware


ROOT = Path(__file__).resolve().parent
for source_dir in (
    ROOT,
    ROOT / "apps" / "notice-studio",
    ROOT / "apps" / "investigation-engine",
):
    value = str(source_dir)
    if value not in sys.path:
        sys.path.insert(0, value)

from investigation_app import create_app as create_investigation_app
from notice_app import create_app as create_notice_app
from signal_portal import create_portal_app


def create_wsgi_app():
    portal = create_portal_app()
    return DispatcherMiddleware(
        portal,
        {
            "/notices": create_notice_app(),
            "/investigations": create_investigation_app(),
        },
    )


app = create_wsgi_app()
