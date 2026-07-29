"""Background job runner for the evidence pipeline.

A single shared :class:`concurrent.futures.ThreadPoolExecutor` runs I/O-bound
pipeline work (hashing, metadata, extraction, indexing) off the request
thread so the UI stays instant. CPU-heavy stages may opt into a process pool
in a later stage; for Stage 2 the work is I/O-bound and thread pools are the
lighter, dependency-free choice.

Jobs are fire-and-forget from the request's perspective; progress is written
back to the ``evidence.status`` column and the ``activity`` table, which the
UI polls sparingly (or, later, receives via SSE - no busy polling).
"""
from __future__ import annotations

import atexit
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

_logger = logging.getLogger("iie.jobs")

# Small pool: this is a local, single-operator tool. Keeping the pool modest
# bounds memory and avoids saturating the machine that also runs Ollama.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iie-job")
_CANCELLED_CASES: set[str] = set()
_CANCELLED_EVIDENCE_IDS: set[int] = set()


def cancel_case(case_uid: str, evidence_ids: Iterable[int] | None = None) -> None:
    """Mark an investigation as closed so queued/running evidence jobs exit early."""
    if case_uid:
        _CANCELLED_CASES.add(str(case_uid))
    if evidence_ids:
        for evidence_id in evidence_ids:
            try:
                _CANCELLED_EVIDENCE_IDS.add(int(evidence_id))
            except (TypeError, ValueError):
                continue


def is_case_cancelled(case_uid: str | None) -> bool:
    return bool(case_uid and str(case_uid) in _CANCELLED_CASES)


def is_evidence_cancelled(evidence_id: int | None) -> bool:
    try:
        return int(evidence_id) in _CANCELLED_EVIDENCE_IDS
    except (TypeError, ValueError):
        return False



def submit(fn: Callable[..., None], *args, **kwargs) -> None:
    """Schedule ``fn`` on the shared pool. Exceptions are logged, not raised."""
    def _wrapped() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive; never crash a worker
            _logger.exception("Background job failed: %s", getattr(fn, "__name__", fn))

    _executor.submit(_wrapped)


@atexit.register
def _shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
