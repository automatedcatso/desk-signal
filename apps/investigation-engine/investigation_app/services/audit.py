"""Structured audit logging for mutating actions (security requirement)."""
from __future__ import annotations

import logging

_audit = logging.getLogger("iie_audit")


def record(action: str, **fields: object) -> None:
    """Append a single audit line. Never raises."""
    try:
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        _audit.info("%s %s", action, detail)
    except Exception:  # pragma: no cover - logging must never break a request
        pass
