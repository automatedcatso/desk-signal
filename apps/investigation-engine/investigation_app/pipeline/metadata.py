"""Evidence metadata + type detection, with real offline extraction.

Detects a coarse evidence type from extension/MIME and extracts plain text
for the entity engine and FTS5 index. Text-like files are read directly;
PDF, Office documents, images (OCR) and email are handled by the optional,
lazily-loaded extractors in :mod:`investigation_app.pipeline.extractors`. Anything that
cannot be parsed (missing optional library, unsupported/binary format)
degrades gracefully to empty text so the pipeline never fails.
"""
from __future__ import annotations

import mimetypes
import os
from typing import Callable, Dict, Optional, Tuple

from investigation_app.pipeline import extractors

# Treat scripts/source/config/log exports as *evidence text*, never as executable code.
# This makes ingestion robust for Python, BAT, PowerShell, shell, JS, Markdown,
# JSON/YAML/TOML/INI, logs, CSV/TSV, HTML/XML and many tool-output formats.
_TEXT_EXTS = {
    ".txt", ".text", ".csv", ".tsv", ".log", ".logs", ".json", ".jsonl",
    ".ndjson", ".md", ".markdown", ".rst", ".html", ".htm", ".xhtml",
    ".xml", ".svg", ".eml", ".msg", ".vcf", ".ics", ".ini", ".cfg",
    ".conf", ".config", ".cnf", ".properties", ".env", ".yaml", ".yml",
    ".toml", ".lock", ".sql", ".sqlite.sql",
    # scripts / source code
    ".py", ".pyw", ".bat", ".cmd", ".ps1", ".psm1", ".sh", ".bash",
    ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".java", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs",
    ".go", ".rs", ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".m",
    ".swift", ".dart", ".scala", ".groovy", ".vb", ".vbs", ".ahk",
    # app/devops outputs
    ".dockerfile", ".gitignore", ".gitattributes", ".npmrc", ".yarnrc",
    ".service", ".timer", ".reg", ".desktop", ".manifest", ".plist",
}
_ARCHIVE_EXTS = {".zip", ".tar", ".tgz", ".tar.gz"}
_MAX_TEXT_BYTES = 5 * 1024 * 1024  # cap text read to bound memory.
_SNIFF_BYTES = 8192


def _looks_like_text(path: str) -> bool:
    """Return True for unknown extensions that are still safe text evidence.

    This is intentionally conservative: binary files remain metadata/hash-only,
    while extensionless scripts, tool dumps and logs still get indexed.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(_SNIFF_BYTES)
    except OSError:
        return False
    if not data:
        return True
    if b"\x00" in data:
        return False
    printable = sum(1 for b in data if b in b"\t\n\r\f\b" or 32 <= b <= 126 or b >= 128)
    return (printable / max(len(data), 1)) >= 0.85


def _hint_ext(path: str, filename_hint: Optional[str] = None) -> str:
    """Return the real/original extension for stored evidence.

    Evidence originals are stored on disk as their SHA-256 digest, so the
    stored path usually has no extension.  Type detection must therefore use
    the original uploaded filename when available; otherwise XLSX/DOCX/PPTX,
    scripts, EML and archives are misclassified as binary and never parsed.
    """
    return (os.path.splitext(filename_hint or "")[1] or os.path.splitext(path)[1]).lower()


def detect_type(path: str, mime: Optional[str], filename_hint: Optional[str] = None) -> str:
    """Return a coarse evidence category.

    ``filename_hint`` is the original uploaded filename. It is critical because
    stored evidence paths are content hashes with no extension.
    """
    ext = _hint_ext(path, filename_hint)
    m = (mime or mimetypes.guess_type(filename_hint or path)[0] or "").lower()
    if ext in _TEXT_EXTS or m.startswith("text/") or m in {"application/json", "application/xml"}:
        return "text"
    if m.startswith("image/"):
        return "image"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    if ext in {".pdf"} or m == "application/pdf":
        return "pdf"
    if ext in _ARCHIVE_EXTS or ext in {".7z", ".rar", ".gz"}:
        return "archive"
    if ext in {".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx"}:
        return "office"
    if m in {"application/javascript", "application/x-javascript", "application/sql", "application/x-sh", "application/x-bat"}:
        return "text"
    if _looks_like_text(path):
        return "text"
    return "binary"


def extract_text(path: str, kind: str, filename_hint: Optional[str] = None, progress_callback: Optional[Callable[[float, str, int, int], None]] = None) -> str:
    """Return best-effort plain text for any supported evidence kind, else ''.

    Delegates to the real extractors (PDF/Office/image-OCR/email/HTML). Falls
    back to a bounded raw read for text-like files if the extractor yields
    nothing, preserving the previous behaviour for plain text.
    """
    text = extractors.extract_text(path, kind, filename_hint=filename_hint, progress_callback=progress_callback)
    if text:
        return text
    if kind != "text":
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(min(size, _MAX_TEXT_BYTES))
    except OSError:
        return ""


def collect(path: str, mime: Optional[str], filename_hint: Optional[str] = None) -> Tuple[str, Dict[str, object]]:
    """Return (kind, metadata dict) for the file."""
    kind = detect_type(path, mime, filename_hint=filename_hint)
    ext = _hint_ext(path, filename_hint)
    meta: Dict[str, object] = {
        "kind": kind,
        "ext": ext,
        "original_ext": ext,
        "mime": (mime or mimetypes.guess_type(filename_hint or path)[0] or "application/octet-stream"),
    }
    try:
        meta["size"] = os.path.getsize(path)
    except OSError:
        meta["size"] = 0
    # Fold in rich, format-specific metadata (EXIF/GPS, PDF/Office props).
    extracted = extractors.extract_metadata(path, kind, filename_hint=filename_hint)
    if extracted:
        meta["extracted"] = extracted
    return kind, meta
