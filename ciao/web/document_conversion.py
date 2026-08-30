"""Optional local conversion of supported documents to Markdown."""
from __future__ import annotations
from pathlib import Path

ANYDOC_MAX_BYTES = 50 * 1024 * 1024

ANYDOC_EXTENSIONS = frozenset({
    ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv", ".pdf",
})

def is_anydoc_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in ANYDOC_EXTENSIONS

def convert_document(path: Path) -> str:
    if path.stat().st_size > ANYDOC_MAX_BYTES:
        raise ValueError("file too large")
    try:
        import anydoc
    except ImportError as exc:
        raise RuntimeError("Document conversion is unavailable; install firecrawl-anydoc.") from exc
    try:
        if path.suffix.lower() == ".csv":
            result = anydoc.to_markdown_bytes(path.read_bytes(), "csv")
        else:
            result = anydoc.to_markdown(str(path))
    except Exception as exc:
        raise RuntimeError(f"document conversion failed: {exc}") from exc
    return result.decode("utf-8") if isinstance(result, bytes) else str(result)
