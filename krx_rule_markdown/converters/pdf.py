from __future__ import annotations

from pathlib import Path

from .base import ConversionError


def extract_pdf(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:
        raise ConversionError("pdfminer.six is not installed") from exc
    return extract_text(str(path))
