"""HTTP adapter to the existing AI Investigation Assistant (port 5003).

The assistant module is the ONLY component that talks to local Ollama
(Qwen3 8B text model). To avoid duplicating that client - and to keep this module fully
isolated - the engine reaches the model exclusively through the assistant's
local HTTP API. Everything stays on 127.0.0.1; no cloud, no second model
process.

The adapter is defensive: if the assistant is down or slow it reports
unavailable so callers can fall back to STANDARD (deterministic) mode. It uses
only the Python standard library (urllib) - no new dependencies.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

_logger = logging.getLogger("iie.ai_adapter")


class AIAssistantAdapter:
    """Thin client for the local AI assistant backend."""

    def __init__(self, base_url: str, timeout: int = 180) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def is_available(self) -> Tuple[bool, str]:
        """Return (ok, detail) by probing the assistant's /health endpoint."""
        try:
            req = urllib.request.Request(f"{self._base}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok")), str(data.get("detail", ""))
        except (urllib.error.URLError, ValueError, OSError) as exc:
            return False, f"assistant unreachable: {exc}"

    def generate(
        self,
        prompt: str,
        context: Optional[List[str]] = None,
        case: str = "",
    ) -> Optional[str]:
        """Ask the assistant to answer ``prompt`` grounded in ``context``.

        Calls the assistant's real endpoint ``POST /api/ai/message`` which
        expects ``{"message": str, "context": {dict}}`` and returns
        ``{"ok": true, "reply": str}`` (or HTTP 503/400 with
        ``{"ok": false, "error": ...}``).

        The assistant's ``context`` is a small metadata dict (case/reviewer),
        not a list of chunks, so the retrieved RAG chunks are folded into the
        message as a compact grounding block. Only that compact context is
        sent - never raw evidence files. Returns ``None`` on any failure so
        the caller can fall back to deterministic mode.
        """
        message = prompt
        if context:
            grounding = "\n\n".join(f"- {c}" for c in context if c)
            if grounding:
                message = (
                    "Use the following retrieved investigation context to "
                    "answer. If it is insufficient, say so.\n\n"
                    f"CONTEXT:\n{grounding}\n\nQUESTION:\n{prompt}"
                )
        payload = json.dumps({
            "message": message,
            "context": {"case": case, "module": "Investigation Intelligence Engine"},
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{self._base}/api/ai/message",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                _logger.info("AI assistant returned not-ok: %s", data.get("error"))
                return None
            reply = data.get("reply")
            return reply if reply else None
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError) as exc:
            _logger.info("AI assistant /api/ai/message failed: %s", exc)
            return None
