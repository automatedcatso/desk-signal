"""Shared offline OCR helpers for local analysis tools.

This intentionally mirrors the AI Assistant OCR stack: RapidOCR + ONNX Runtime.
It does not depend on either Flask app's ``app`` package, so the analysis
Engine can reuse the same OCR approach without import-name collisions.

All functions are local/offline and fail with clear diagnostics instead of
raising into the evidence pipeline.
"""
from __future__ import annotations

import hashlib
import io
import logging
from collections import OrderedDict
from typing import Any

_log = logging.getLogger("signal_desk.offline_ocr")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")

_ENGINE: Any | None = None
_ENGINE_ERROR = ""
_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CACHE_LIMIT = 64


def _cache_get(key: str) -> str | None:
    val = _CACHE.get(key)
    if val is not None:
        _CACHE.move_to_end(key)
    return val


def _cache_set(key: str, val: str) -> None:
    if not val:
        return
    _CACHE[key] = val
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)


def _get_engine():
    """Lazily build one RapidOCR engine. Model load is expensive."""
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None or _ENGINE_ERROR:
        return _ENGINE
    rapid_cls = None
    try:
        from rapidocr import RapidOCR as _RapidOCR  # type: ignore
        rapid_cls = _RapidOCR
    except Exception:
        try:
            from rapidocr_onnxruntime import RapidOCR as _RapidOCR  # type: ignore
            rapid_cls = _RapidOCR
        except Exception:
            _ENGINE_ERROR = "RapidOCR/ONNX Runtime is not installed"
            return None
    try:
        _ENGINE = rapid_cls()
    except Exception as exc:  # noqa: BLE001
        _ENGINE_ERROR = f"RapidOCR engine failed to initialise: {exc}"
        _log.exception("RapidOCR initialisation failed")
        return None
    return _ENGINE


def rapidocr_available() -> bool:
    return _get_engine() is not None


def rapidocr_status() -> str:
    return "ok" if rapidocr_available() else _ENGINE_ERROR or "RapidOCR unavailable"


def _normalise_output(raw):
    """Return a list of (box, text) pairs across RapidOCR 1.x/2.x APIs."""
    if isinstance(raw, tuple):
        raw = raw[0] if raw else None
    if raw is None:
        return []

    boxes = getattr(raw, "boxes", None)
    txts = getattr(raw, "txts", None)
    if boxes is not None and txts is not None:
        return list(zip(boxes, txts))

    pairs = []
    try:
        for item in raw:
            try:
                pairs.append((item[0], item[1]))
            except Exception:
                continue
    except Exception:
        return []
    return pairs


def _lines_from_result(raw) -> str:
    pairs = _normalise_output(raw)
    if not pairs:
        return ""
    rows: list[tuple[int, float, str]] = []
    for box, text in pairs:
        if not text or not str(text).strip():
            continue
        try:
            top_y = min(pt[1] for pt in box)
            left_x = min(pt[0] for pt in box)
        except Exception:
            top_y, left_x = 0, 0
        rows.append((round(float(top_y) / 12), float(left_x), str(text).strip()))
    rows.sort(key=lambda r: (r[0], r[1]))

    lines: list[str] = []
    seen: set[str] = set()
    for _, __, text in rows:
        key = text.lower()
        if key not in seen:
            seen.add(key)
            lines.append(text)
    return "\n".join(lines)


def rapidocr_image_bytes(data: bytes) -> tuple[str, str]:
    """OCR image bytes with RapidOCR. Returns ``(text, diagnostic)``."""
    if not data:
        return "", "empty image data"
    key = "img:" + hashlib.sha256(data).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached, "ok (cached)"

    engine = _get_engine()
    if engine is None:
        return "", _ENGINE_ERROR or "RapidOCR unavailable"
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore

        img = Image.open(io.BytesIO(data)).convert("RGB")
        arr = np.array(img)
    except Exception as exc:  # noqa: BLE001
        return "", f"image decode/prep failed: {exc}"
    try:
        raw = engine(arr)
        text = _lines_from_result(raw)
        if text.strip():
            _cache_set(key, text)
            return text, "ok"
        return "", "RapidOCR returned no readable text"
    except Exception as exc:  # noqa: BLE001
        _log.exception("RapidOCR failed on image")
        return "", f"RapidOCR failed: {exc}"


def rapidocr_pil_image(img) -> tuple[str, str]:
    """OCR a PIL image without tying callers to numpy/RapidOCR details."""
    try:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return rapidocr_image_bytes(buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        return "", f"image conversion failed: {exc}"
