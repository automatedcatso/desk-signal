"""Central configuration loader for the Investigation Intelligence Engine.

Loads ``config.json`` from the module root, then applies environment-only
provider settings. Secrets are never read from or written to ``config.json``.
For convenient local use, an ignored project-root ``.env`` or ``.env.local``
file can supply the same variables used by Vercel.
"""
from __future__ import annotations

import json
import os
import tempfile
from functools import lru_cache
from typing import Any, Dict

_MODULE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_PATH = os.path.join(_MODULE_ROOT, "config.json")

_DEFAULTS: Dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 5005},
    "ai": {"default_provider": "local"},
    "ai_assistant": {
        "base_url": "http://127.0.0.1:5003",
        "request_timeout_seconds": 300,
    },
    "gemini": {
        "model": "gemini-3.1-flash-lite",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "request_timeout_seconds": 180,
        "max_output_tokens": 8192,
    },
    "limits": {"max_upload_mb": 32},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_local_env() -> None:
    """Load simple KEY=VALUE pairs without adding a dotenv dependency.

    Existing process environment variables always win. The parser intentionally
    does not expand variables or execute any content.
    """
    project_root = os.path.abspath(os.path.join(_MODULE_ROOT, "..", ".."))
    for filename in (".env", ".env.local"):
        path = os.path.join(project_root, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
                        value = value[1:-1]
                    if key and key.replace("_", "").isalnum():
                        os.environ.setdefault(key, value)
        except OSError:
            pass


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    """Return merged public configuration plus environment-only credentials."""
    _load_local_env()
    cfg = _deep_merge({}, _DEFAULTS)
    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as handle:
                cfg = _deep_merge(cfg, json.load(handle))
        except (ValueError, OSError):
            # Fall back to defaults if the file is missing/corrupt.
            pass

    provider = (os.environ.get("SIGNAL_DESK_AI_PROVIDER") or "").strip().lower()
    if provider in {"local", "gemini"}:
        cfg["ai"]["default_provider"] = provider

    cfg["gemini"]["api_key"] = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    cfg["gemini"]["model"] = (
        os.environ.get("GEMINI_MODEL")
        or cfg["gemini"]["model"]
    ).strip()
    cfg["gemini"]["base_url"] = (
        os.environ.get("GEMINI_API_BASE")
        or cfg["gemini"]["base_url"]
    ).strip().rstrip("/")
    cfg["gemini"]["request_timeout_seconds"] = _env_int(
        "GEMINI_TIMEOUT_SECONDS",
        int(cfg["gemini"]["request_timeout_seconds"]),
        10,
        600,
    )
    cfg["gemini"]["max_output_tokens"] = _env_int(
        "GEMINI_MAX_OUTPUT_TOKENS",
        int(cfg["gemini"]["max_output_tokens"]),
        256,
        65536,
    )
    return cfg


def module_root() -> str:
    """Absolute path to the module root (apps/investigation-engine)."""
    return _MODULE_ROOT


def runtime_root() -> str:
    """Writable persistent directory locally, or temporary storage on Vercel."""
    configured = (os.environ.get("SIGNAL_DESK_DATA_DIR") or "").strip()
    if configured:
        return os.path.join(os.path.abspath(configured), "investigation-engine")
    if os.environ.get("VERCEL"):
        return os.path.join(tempfile.gettempdir(), "signal-desk", "investigation-engine")
    return os.path.join(_MODULE_ROOT, "instance")
