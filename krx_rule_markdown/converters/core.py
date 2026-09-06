from __future__ import annotations

from pathlib import Path
import copy

from ..contracts import MAX_SOURCE_BYTES, add_quality_code, canonical_text_hash, converter_version_for_source, parse_quality_codes
from ..models import Attachment, ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, hash_bytes, now_utc
from ..quality import apply_quality, inspect_attachment_quality, mark_quality_failure
from ..repository import atomic_write_text
from .base import ConversionDiagnostic, ConversionError, ConversionOutcome, infer_extension, normalize_converted_text
from .cache import SourceInspectionCache
from .hwp import extract_hwp_with_diagnostics
from .hwpx import extract_hwpx
from .inspection import inspect_converted_source
from .pdf import extract_pdf_details
from ..html import html_to_markdown


MAX_RAW_FILE_BYTES = MAX_SOURCE_BYTES


def convert_attachment(
    raw_path: Path,
    out_path: Path,
    att: Attachment,
    *,
    inspection_cache: SourceInspectionCache | None = None,
) -> Attachment:
    previous = copy.deepcopy(att)
    size = raw_path.stat().st_size
    if size > MAX_RAW_FILE_BYTES:
        raise ConversionError(f"raw file exceeds {MAX_RAW_FILE_BYTES} bytes")
    data = raw_path.read_bytes()
    att.raw_path = str(raw_path)
    att.text_path = str(out_path)
    att.size = len(data)
    att.content_hash = hash_bytes(data)
    att.raw_file_hash = att.content_hash
    att.preservation_status = "preserved"
    inspection_cache = inspection_cache or SourceInspectionCache()
    try:
        outcome = convert_bytes_outcome(
            raw_path,
            data,
            inspection_cache=inspection_cache,
            source_id=att.id,
        )
        text = normalize_converted_text(outcome.text)
        if not text.strip():
            raise ConversionError("conversion produced empty text")
        apply_quality(att, inspect_attachment_quality(text, raw_path, inspection_cache))
        att.status = ATTACHMENT_CONVERTED
        att.converter_version = converter_version_for_source(raw_path)
        att.converted_text_hash = canonical_text_hash(text)
        att.searchable = outcome.searchable and att.quality_status != "fail"
        att.diagnostics = [
            {"code": item.code, "message": item.message, "severity": item.severity}
            for item in outcome.diagnostics
        ]
        for diagnostic in outcome.diagnostics:
            att.quality_codes = add_quality_code(att.quality_codes, diagnostic.code)
        if outcome.diagnostics and att.quality_status == "ok":
            att.quality_status = "warn"
        att.quality_flags = ",".join(parse_quality_codes(att.quality_codes or att.quality_flags))
        att.error = ""
        att.last_refresh_error = ""
        att.last_refresh_failed_at = ""
        # Commit only after conversion, structural inspection, and metadata
        # calculation all succeed. os.replace keeps an existing LKG file intact
        # when writing or fsyncing the temporary file fails.
        atomic_write_text(out_path, text + "\n")
    except Exception as exc:  # noqa: BLE001 - failure reason is part of the manifest.
        if previous.status == ATTACHMENT_CONVERTED and previous.text_path and out_path.exists():
            att.__dict__.update(previous.__dict__)
            att.last_refresh_error = str(exc)
            att.last_refresh_failed_at = now_utc()
            att.quality_codes = add_quality_code(att.quality_codes or att.quality_flags, "stale_due_to_refresh_failure")
            att.quality_flags = ",".join(att.quality_codes)
            if att.quality_status in {"", "ok"}:
                att.quality_status = "warn"
        else:
            att.status = ATTACHMENT_FAILED
            att.error = str(exc)
            att.text_path = ""
            att.converted_text_hash = ""
            att.diagnostics = []
            att.searchable = False
            mark_quality_failure(att, "conversion_failed")
    return att


def convert_bytes(path: Path, data: bytes) -> str:
    return convert_bytes_outcome(path, data).text


def convert_bytes_outcome(
    path: Path,
    data: bytes,
    *,
    inspection_cache: SourceInspectionCache | None = None,
    source_id: str = "",
) -> ConversionOutcome:
    if len(data) > MAX_RAW_FILE_BYTES:
        raise ConversionError(f"raw file exceeds {MAX_RAW_FILE_BYTES} bytes")
    ext = infer_extension(path, data)
    validate_signature(ext, data)
    raw_file_hash = hash_bytes(data)
    if ext in {".md", ".txt"}:
        return ConversionOutcome(data.decode("utf-8", errors="replace"), raw_file_hash=raw_file_hash)
    if ext in {".html", ".htm"}:
        text = html_to_markdown(data.decode("utf-8", errors="replace"))
        diagnostics = []
        if "[이미지:" in text:
            diagnostics.extend(
                [
                    ConversionDiagnostic("inline_image_missing", "inline HTML image is not preserved locally"),
                    ConversionDiagnostic("image_content_unindexed", "inline image content is not text searchable"),
                ]
            )
        return ConversionOutcome(text, raw_file_hash=raw_file_hash, diagnostics=diagnostics, searchable=bool(text.strip()))
    if ext == ".hwpx":
        text = extract_hwpx(data)
        diagnostics, searchable = inspect_converted_source(path, text)
        return ConversionOutcome(text, raw_file_hash=raw_file_hash, diagnostics=diagnostics, searchable=searchable)
    if ext == ".pdf":
        text, page_count = extract_pdf_details(path, comparison_id=source_id)
        diagnostics, searchable = inspect_converted_source(path, text, pdf_pages=page_count)
        return ConversionOutcome(text, raw_file_hash=raw_file_hash, diagnostics=diagnostics, searchable=searchable)
    if ext == ".hwp":
        inspection_cache = inspection_cache or SourceInspectionCache()
        text, fallback_diagnostics = extract_hwp_with_diagnostics(path, inspection_cache)
        diagnostics, searchable = inspect_converted_source(path, text, inspection_cache=inspection_cache)
        return ConversionOutcome(
            text,
            raw_file_hash=raw_file_hash,
            diagnostics=[*fallback_diagnostics, *diagnostics],
            searchable=searchable,
        )
    raise ConversionError(f"unsupported attachment extension {ext!r}")


def validate_signature(ext: str, data: bytes) -> None:
    if ext == ".pdf" and not data.startswith(b"%PDF-"):
        raise ConversionError("PDF signature mismatch")
    if ext == ".hwp" and not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ConversionError("HWP signature mismatch")
    if ext == ".hwpx" and not data.startswith(b"PK\x03\x04"):
        raise ConversionError("HWPX signature mismatch")
