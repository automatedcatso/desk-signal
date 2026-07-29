"""Configuration for the local-first Notice Studio."""
import os
import tempfile

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def _runtime_dir() -> str:
    configured = (os.environ.get("SIGNAL_DESK_DATA_DIR") or "").strip()
    if configured:
        return os.path.join(os.path.abspath(configured), "notice-studio")
    if os.environ.get("VERCEL"):
        return os.path.join(tempfile.gettempdir(), "signal-desk", "notice-studio")
    return os.path.join(BASE_DIR, "instance")


INSTANCE_DIR = _runtime_dir()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")

    # SQLite keeps imported work across refresh and scales past 5,000 records.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(INSTANCE_DIR, 'notice_studio.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER = os.path.join(INSTANCE_DIR, "uploads")
    OUTPUT_FOLDER = os.path.join(INSTANCE_DIR, "output")

    # Master DOCX template lives at the project root next to app.py.
    TEMPLATE_DOCX = os.path.join(BASE_DIR, "notice_template.docx")

    MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MB upload ceiling.
    ALLOWED_EXTENSIONS = {"xlsx"}

    # Neutral sender roles shared across single + bulk modes.
    SENDER_ROLES = [
        "No signature",
        "Authorized Sender",
        "Operations Team",
        "Compliance Team",
        "Legal Team",
    ]
    UNSIGNED_ROLE = "No signature"

    # Default subject line used for the {{subject}} token when the template
    # does not otherwise provide one. Date format used for the {{date}} token.
    DEFAULT_SUBJECT = "Information request for referenced transaction"
    DATE_FORMAT = "%d/%m/%Y"

    PAGE_SIZE = 50  # Server-side pagination for the record grid.
