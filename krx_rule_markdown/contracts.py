from __future__ import annotations

from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
import json
import stat
import unicodedata
from typing import Any, Iterable


CORPUS_SCHEMA_VERSION = 2
CONVERTER_VERSION = "2"
RELEASE_PROFILE_VERSION = 1
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_CONVERTED_TEXT_BYTES = 64 * 1024 * 1024
MAX_METADATA_FILE_BYTES = 64 * 1024 * 1024


def converter_version_for_source(path: str | Path) -> str:
    """Invalidate only PDF conversion caches when the reading-order algorithm changes."""
    if Path(path).suffix.lower() == ".pdf":
        return f"{CONVERTER_VERSION}+pdf-coordinate-order"
    return CONVERTER_VERSION

CONVERSION_STATUSES = frozenset({"pending", "converted", "failed"})
PRESERVATION_STATUSES = frozenset({"preserved", "missing", "failed"})
QUALITY_STATUSES = frozenset({"ok", "warn", "fail"})
REFRESH_OPERATIONAL_FIELDS = frozenset(
    {
        "last_refresh_error",
        "last_refresh_failed_at",
    }
)
RELEASE_OPERATIONAL_FIELDS = frozenset(
    {
        "release_hash",
        "generated_at",
        "last_checked_at",
        "source_response_hash",
    }
) | REFRESH_OPERATIONAL_FIELDS

VALIDATOR_ERROR_CODES = frozenset(
    {
        "body_hash_mismatch",
        "raw_file_hash_mismatch",
        "converted_text_hash_mismatch",
        "manifest_metadata_mismatch",
        "required_source_missing",
        "required_conversion_failed",
        "path_outside_data_root",
        "duplicate_document_id",
        "duplicate_attachment_id",
        "duplicate_asset_id",
        "invalid_status_combination",
        "formula_source_count_mismatch",
    }
)

QUALITY_WARNING_CODES = frozenset(
    {
        "document_empty_body",
        "pdf_text_layer_too_sparse",
        "pdf_comparison_structure_lost",
        "image_content_unindexed",
        "inline_image_missing",
        "hwp_picture_missing",
        "html_text_boundary_collapsed",
        "formula_generated_latex_invalid",
        "source_inspection_failed",
        "stale_due_to_refresh_failure",
        # Findings emitted by the current text and conversion inspections.
        "empty_text",
        "very_short_text",
        "replacement_characters",
        "very_long_lines",
        "raw_table_hints_without_table_text",
        "raw_table_cells_may_be_flattened",
        "raw_formula_hints_without_formula_text",
        "conversion_failed",
        "conversion_pending",
        "missing_text_path",
        "missing_converted_file",
    }
)

QUALITY_CODES = VALIDATOR_ERROR_CODES | QUALITY_WARNING_CODES


def canonical_text(value: str) -> str:
    """Return the producer/consumer canonical text representation.

    The contract is UTF-8, LF line endings, NFC normalization, and trimming at
    the boundary of the complete value. Internal whitespace is not rewritten.
    """

    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", value).strip()


def canonical_text_bytes(value: str) -> bytes:
    return canonical_text(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {path}")
    if max_bytes is not None and info.st_size > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    digest = sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_utf8_file_bounded(path: Path, *, max_bytes: int = MAX_METADATA_FILE_BYTES) -> str:
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {path}")
    if info.st_size > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    return data.decode("utf-8", errors="strict")


def canonical_text_hash(value: str) -> str:
    return sha256_bytes(canonical_text_bytes(value))


def canonical_json_bytes(value: Any) -> bytes:
    normalized = normalize_json_strings(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def canonical_timestamp(value: str) -> str:
    text = canonical_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    parsed = parsed.astimezone(timezone.utc)
    base = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{parsed.microsecond:06d}".rstrip("0")
    return f"{base}.{fraction}Z" if fraction else f"{base}Z"


def normalize_json_strings(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, list):
        return [normalize_json_strings(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_json_strings(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_json_strings(item) for key, item in value.items()}
    return value


def parse_quality_codes(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    out: list[str] = []
    for item in values:
        code = str(item).strip()
        if code and code not in out:
            out.append(code)
    return out


def add_quality_code(codes: Iterable[str] | str, code: str) -> list[str]:
    out = parse_quality_codes(codes)
    code = (code or "").strip()
    if code and code not in out:
        out.append(code)
    return out


def status_combination_errors(
    *,
    conversion_status: str = "",
    preservation_status: str = "",
    searchable: bool | None = None,
    quality_status: str = "",
) -> list[str]:
    errors: list[str] = []
    if conversion_status and conversion_status not in CONVERSION_STATUSES:
        errors.append(f"unknown conversion_status {conversion_status!r}")
    if preservation_status and preservation_status not in PRESERVATION_STATUSES:
        errors.append(f"unknown preservation_status {preservation_status!r}")
    if quality_status and quality_status not in QUALITY_STATUSES:
        errors.append(f"unknown quality_status {quality_status!r}")
    if conversion_status == "failed" and searchable is True:
        errors.append("failed conversion must not be searchable")
    if quality_status == "fail" and searchable is True:
        errors.append("failed quality must not be searchable")
    return errors


def document_index_payload(doc: Any) -> dict[str, Any]:
    attachments = []
    for att in sorted(getattr(doc, "attachments", []), key=lambda item: item.id):
        attachments.append(
            {
                "id": canonical_text(att.id),
                "title": canonical_text(att.title),
                "file_name": canonical_text(att.file_name),
                "conversion_status": canonical_text(getattr(att, "status", "")),
                "searchable": effective_searchable(att),
                "converted_text_hash": canonical_text(getattr(att, "converted_text_hash", "")),
                "quality_status": canonical_text(getattr(att, "quality_status", "")),
                "quality_codes": sorted(parse_quality_codes(getattr(att, "quality_codes", []))),
            }
        )
    return {
        "id": canonical_text(doc.id),
        "document_type": canonical_text(doc.document_type),
        "language": canonical_text(doc.language),
        "title": canonical_text(doc.title),
        "category": canonical_text(doc.category),
        "source_url": canonical_text(doc.source_url),
        "collected_at": canonical_timestamp(doc.collected_at),
        "file_name": canonical_text(doc.file_name),
        "body_hash": getattr(doc, "body_hash", "") or canonical_text_hash(doc.body),
        "source_id": canonical_text(doc.source_id),
        "effective_date": canonical_text(doc.effective_date),
        "published_date": canonical_text(doc.published_date),
        "searchable": effective_searchable(doc),
        "quality_status": canonical_text(getattr(doc, "quality_status", "")),
        "quality_codes": sorted(parse_quality_codes(getattr(doc, "quality_codes", []))),
        "attachments": attachments,
    }


def index_source_hash(documents: Iterable[Any]) -> str:
    return canonical_json_hash(index_source_payload(documents))


def index_source_payload(documents: Iterable[Any]) -> dict[str, Any]:
    payload = [
        document_index_payload(doc)
        for doc in sorted(
            documents,
            key=lambda item: (item.id, item.document_type, item.language),
        )
    ]
    return {"schema_version": CORPUS_SCHEMA_VERSION, "documents": payload}


def release_hash(manifest: dict[str, Any]) -> str:
    return canonical_json_hash(scrub_operational_fields(manifest))


def scrub_operational_fields(
    value: Any,
    *,
    excluded_fields: frozenset[str] = RELEASE_OPERATIONAL_FIELDS,
) -> Any:
    if isinstance(value, dict):
        return {
            key: scrub_operational_fields(item, excluded_fields=excluded_fields)
            for key, item in value.items()
            if key not in excluded_fields
        }
    if isinstance(value, list):
        return [
            scrub_operational_fields(item, excluded_fields=excluded_fields)
            for item in value
        ]
    return value


def contains_refresh_operational_fields(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in REFRESH_OPERATIONAL_FIELDS
            or contains_refresh_operational_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_refresh_operational_fields(item) for item in value)
    return False


def effective_searchable(entity: Any) -> bool:
    status = getattr(entity, "status", getattr(entity, "conversion_status", ""))
    if status == "failed" or getattr(entity, "quality_status", "") == "fail":
        return False
    value = getattr(entity, "searchable", None)
    if value is not None:
        return bool(value)
    if hasattr(entity, "body"):
        return bool(canonical_text(getattr(entity, "body", "")))
    if status:
        return status == "converted" and bool(getattr(entity, "text_path", ""))
    return False
