from __future__ import annotations

from pathlib import Path
import contextlib

from ..models import Attachment, ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, hash_bytes
from ..quality import apply_quality, inspect_attachment_quality, mark_quality_failure
from .base import ConversionError, infer_extension
from .hwp import extract_hwp
from .hwpx import extract_hwpx
from .pdf import extract_pdf
from ..html import html_to_markdown


def convert_attachment(raw_path: Path, out_path: Path, att: Attachment) -> Attachment:
    data = raw_path.read_bytes()
    att.raw_path = str(raw_path)
    att.text_path = str(out_path)
    att.size = len(data)
    att.content_hash = hash_bytes(data)
    try:
        text = convert_bytes(raw_path, data)
        if not text.strip():
            raise ConversionError("conversion produced empty text")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.strip() + "\n", encoding="utf-8")
        apply_quality(att, inspect_attachment_quality(text, raw_path))
        att.status = ATTACHMENT_CONVERTED
        att.error = ""
    except Exception as exc:  # noqa: BLE001 - failure reason is part of the manifest.
        att.status = ATTACHMENT_FAILED
        att.error = str(exc)
        att.text_path = ""
        mark_quality_failure(att, "conversion_failed")
        with contextlib.suppress(FileNotFoundError):
            out_path.unlink()
    return att


def convert_bytes(path: Path, data: bytes) -> str:
    ext = infer_extension(path, data)
    if ext in {".md", ".txt"}:
        return data.decode("utf-8", errors="replace")
    if ext in {".html", ".htm"}:
        return html_to_markdown(data.decode("utf-8", errors="replace"))
    if ext == ".hwpx":
        return extract_hwpx(data)
    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".hwp":
        return extract_hwp(path)
    raise ConversionError(f"unsupported attachment extension {ext!r}")
