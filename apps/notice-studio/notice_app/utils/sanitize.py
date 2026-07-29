"""Filename sanitisation helpers for safe, collision-resistant output paths."""
import re

_INVALID = re.compile(r"[^A-Za-z0-9._\- ]+")
_WS = re.compile(r"\s+")


def sanitize_filename(value: str, fallback: str = "unknown") -> str:
    """Return a filesystem-safe filename component."""
    if value is None:
        value = ""
    value = str(value).strip()
    value = _INVALID.sub("_", value)
    value = _WS.sub("_", value)
    value = value.strip("._ ")
    return value or fallback


def sanitize_folder(value: str, fallback: str = "Unknown") -> str:
    """Return a filesystem-safe folder name (spaces preserved, trimmed)."""
    if value is None:
        value = ""
    value = str(value).strip()
    value = _INVALID.sub("_", value)
    value = value.strip("._")
    return value or fallback
