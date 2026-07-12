from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import re
import statistics
import zipfile

from .contracts import (
    CORPUS_SCHEMA_VERSION,
    MAX_CONVERTED_TEXT_BYTES,
    MAX_SOURCE_BYTES,
    add_quality_code,
    parse_quality_codes,
)
from .converters.cache import SourceInspectionCache
from .converters.inspection import inspect_converted_source
from .markdown import load_documents, write_document
from .manifest import manifest_allowed_failure_ids, write_manifest_atomic
from .models import (
    ATTACHMENT_CONVERTED,
    ATTACHMENT_FAILED,
    ATTACHMENT_PENDING,
    Attachment,
    Document,
    now_utc,
)
from .repository import CorpusMutationError, atomic_write_json, mutate_staged_corpus


REPORT_VERSION = "0.1.0"
FORMULA_RE = re.compile(
    r"(≤|≥|≠|±|×|÷|∑|∫|∞|∂|→|←|"
    r"√(?:\s*[\(\{]|[0-9A-Za-z가-힣])|"
    r"\b[A-Za-z]\s*=\s*[-+0-9A-Za-z(]|[0-9]\s*[+*]\s*[0-9]|"
    r"\b(?:hat|sum|Isum|LEFT|RIGHT|over|sqrt|root|matrix|dmatrix)\b|"
    r"수식\s+\d+:|```(?:hwp-equation|math))"
)
FORMULA_BLOCK_RE = re.compile(r"^```(?:hwp-equation|math)\s*$", re.MULTILINE)
SOURCE_FORMULA_BLOCK_RE = re.compile(r"^```hwp-equation\s*$", re.MULTILINE)


@dataclass
class AttachmentQuality:
    status: str
    score: int
    flags: list[str]
    text_chars: int
    non_space_chars: int
    line_count: int
    table_row_count: int
    table_block_count: int
    table_cell_count: int
    formula_block_count: int
    generated_math_block_count: int
    formula_hint_count: int
    replacement_char_count: int
    raw_table_hint_count: int = 0
    raw_table_cell_hint_count: int = 0
    raw_formula_hint_count: int = 0
    source_inspection_error: str = ""


def inspect_attachment_quality(
    text: str,
    raw_path: Path | None = None,
    inspection_cache: SourceInspectionCache | None = None,
) -> AttachmentQuality:
    text_chars = len(text)
    non_space_chars = sum(1 for ch in text if not ch.isspace())
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    table_row_count = sum(1 for line in lines if is_table_like_line(line))
    table_block_count, table_cell_count = markdown_table_structure_counts(text)
    formula_block_count = preserved_formula_count(text)
    generated_math_block_count = len(re.findall(r"^```math\s*$", text, re.MULTILINE))
    formula_hint_count = len(FORMULA_RE.findall(text))
    replacement_char_count = text.count("\ufffd")
    raw_table_hints, raw_table_cell_hints, raw_formula_hints, inspection_error = raw_structure_hints(
        raw_path,
        inspection_cache,
    )

    flags: list[str] = []
    if text_chars == 0:
        flags.append("empty_text")
    if 0 < non_space_chars < 40:
        flags.append("very_short_text")
    if replacement_char_count:
        flags.append("replacement_characters")
    if max((len(line) for line in lines), default=0) > 1200:
        flags.append("very_long_lines")
    if raw_table_hints > 0 and table_block_count == 0:
        flags.append("raw_table_hints_without_table_text")
    if raw_table_cell_hints >= 8 and table_cell_count < raw_table_cell_hints * 0.5:
        flags.append("raw_table_cells_may_be_flattened")
    if raw_formula_hints > 0 and formula_hint_count == 0:
        flags.append("raw_formula_hints_without_formula_text")
    if raw_formula_hints != formula_block_count and raw_path is not None and raw_path.suffix.lower() == ".hwp":
        flags.append("formula_source_count_mismatch")
    elif formula_block_count > generated_math_block_count:
        flags.append("formula_generated_latex_invalid")
    if inspection_error:
        flags.append("source_inspection_failed")

    score = 100
    penalties = {
        "empty_text": 100,
        "very_short_text": 45,
        "replacement_characters": min(30, replacement_char_count * 3),
        "very_long_lines": 15,
        "raw_table_hints_without_table_text": 25,
        "raw_table_cells_may_be_flattened": 20,
        "raw_formula_hints_without_formula_text": 25,
        "source_inspection_failed": 20,
        "formula_source_count_mismatch": 100,
        "formula_generated_latex_invalid": 10,
    }
    for flag in flags:
        score -= penalties.get(flag, 10)
    score = max(0, min(100, score))
    status = "fail" if {"empty_text", "formula_source_count_mismatch"} & set(flags) else "warn" if flags else "ok"
    return AttachmentQuality(
        status=status,
        score=score,
        flags=flags,
        text_chars=text_chars,
        non_space_chars=non_space_chars,
        line_count=len(lines),
        table_row_count=table_row_count,
        table_block_count=table_block_count,
        table_cell_count=table_cell_count,
        formula_block_count=formula_block_count,
        generated_math_block_count=generated_math_block_count,
        formula_hint_count=formula_hint_count,
        replacement_char_count=replacement_char_count,
        raw_table_hint_count=raw_table_hints,
        raw_table_cell_hint_count=raw_table_cell_hints,
        raw_formula_hint_count=raw_formula_hints,
        source_inspection_error=inspection_error,
    )


def is_table_like_line(line: str) -> bool:
    stripped = line.strip()
    if re.search(r"<tr\b", stripped, re.I):
        return True
    if stripped.count("|") >= 2:
        cells = [cell.strip() for cell in stripped.strip("|").split("|") if cell.strip()]
        return len(cells) >= 2
    if "\t" in stripped:
        cells = [cell.strip() for cell in stripped.split("\t") if cell.strip()]
        return len(cells) >= 2
    cells = [cell for cell in re.split(r"\s{2,}", stripped) if cell.strip()]
    if len(cells) < 3:
        return False
    meaningful = sum(1 for cell in cells if re.search(r"[0-9A-Za-z가-힣]", cell))
    return meaningful >= 2


def markdown_table_structure_counts(text: str) -> tuple[int, int]:
    lines = text.splitlines()
    block_count = 0
    cell_count = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"<table\b", line, re.I):
            block_count += 1
            while i < len(lines):
                cell_count += sum(html_cell_span_width(match) for match in re.finditer(r"<t[dh]\b[^>]*>", lines[i], re.I))
                if re.search(r"</table\s*>", lines[i], re.I):
                    i += 1
                    break
                i += 1
            continue
        if line.strip().startswith("|") and line.count("|") >= 2:
            rows = 0
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].count("|") >= 2:
                parts = [cell.strip() for cell in lines[i].strip().strip("|").split("|")]
                is_separator = all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in parts)
                if not is_separator:
                    rows += 1
                    cell_count += len(parts)
                i += 1
            if rows:
                block_count += 1
            continue
        i += 1
    return block_count, cell_count


def html_cell_span_width(match: re.Match[str]) -> int:
    attrs = match.group(0)
    colspan = re.search(r"\bcolspan\s*=\s*['\"]?(\d+)", attrs, re.I)
    return max(1, int(colspan.group(1))) if colspan else 1


def preserved_formula_count(text: str) -> int:
    source_blocks = len(SOURCE_FORMULA_BLOCK_RE.findall(text))
    if source_blocks:
        return source_blocks
    return len(FORMULA_BLOCK_RE.findall(text))


def raw_structure_hints(
    raw_path: Path | None,
    inspection_cache: SourceInspectionCache | None = None,
) -> tuple[int, int, int, str]:
    if raw_path is None or not raw_path.exists():
        return 0, 0, 0, ""
    if raw_path.stat().st_size > MAX_SOURCE_BYTES:
        return 0, 0, 0, f"raw source exceeds {MAX_SOURCE_BYTES} bytes"
    if raw_path.suffix.lower() == ".hwp":
        return hwp_structure_counts(raw_path, inspection_cache)
    if raw_path.suffix.lower() != ".hwpx":
        return 0, 0, 0, ""
    table_hints = 0
    table_cell_hints = 0
    formula_hints = 0
    try:
        with zipfile.ZipFile(raw_path) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if not lower.endswith(".xml"):
                    continue
                xml = zf.read(name).decode("utf-8", errors="replace")
                table_hints += len(re.findall(r"<[^>]*:?tbl\b", xml, re.I))
                table_cell_hints += len(re.findall(r"<[^>]*:?tc\b", xml, re.I))
                formula_hints += len(re.findall(r"(equation|formula|수식|<[^>]*:?eq\b)", xml, re.I))
    except (OSError, zipfile.BadZipFile):
        return 0, 0, 0, "HWPX structure inspection failed"
    return table_hints, table_cell_hints, formula_hints, ""


def hwp_structure_counts(
    raw_path: Path,
    inspection_cache: SourceInspectionCache | None = None,
) -> tuple[int, int, int, str]:
    if raw_path.stat().st_size > MAX_SOURCE_BYTES:
        return 0, 0, 0, f"HWP exceeds {MAX_SOURCE_BYTES} bytes"
    try:
        from hwp5.binmodel import EqEdit
        from hwp5.proc.find import hwp5file_models
    except ImportError:
        return 0, 0, 0, "pyhwp is unavailable for HWP structure inspection"

    table_count = 0
    cell_count = 0
    equation_count = 0
    try:
        if inspection_cache is None:
            models = hwp5file_models(str(raw_path))
        else:
            models, model_error = inspection_cache.hwp_models(raw_path)
            if model_error:
                return 0, 0, 0, model_error
        for model in models:
            if model.get("type") is EqEdit:
                equation_count += 1
            if model.get("tagname") != "HWPTAG_TABLE":
                continue
            table_count += 1
            content = model.get("content", {})
            cell_count += sum(content.get("rowcols", []) or []) or int(content.get("rows", 0) or 0) * int(content.get("cols", 0) or 0)
    except Exception:
        return 0, 0, 0, "HWP structure inspection failed"
    return table_count, cell_count, equation_count, ""


def hwp_eqedit_count(raw_path: Path) -> int:
    return hwp_structure_counts(raw_path)[2]


def hwp_table_counts(raw_path: Path) -> tuple[int, int]:
    table_count, cell_count, _, _ = hwp_structure_counts(raw_path)
    return table_count, cell_count


def apply_quality(att: Attachment, quality: AttachmentQuality) -> Attachment:
    att.quality_status = quality.status
    att.quality_score = quality.score
    att.quality_flags = ",".join(quality.flags)
    att.quality_codes = parse_quality_codes(quality.flags)
    att.converted_text_chars = quality.text_chars
    att.converted_non_space_chars = quality.non_space_chars
    att.table_row_count = quality.table_row_count
    att.formula_block_count = quality.formula_block_count
    att.formula_hint_count = quality.formula_hint_count
    att.replacement_char_count = quality.replacement_char_count
    if quality.status == "fail":
        att.searchable = False
    return att


def mark_quality_failure(att: Attachment, flag: str) -> Attachment:
    att.quality_status = "fail"
    att.quality_score = 0
    att.quality_flags = flag
    att.quality_codes = [flag]
    att.searchable = False
    att.converted_text_chars = 0
    att.converted_non_space_chars = 0
    att.table_row_count = 0
    att.formula_block_count = 0
    att.formula_hint_count = 0
    att.replacement_char_count = 0
    return att


def audit_data_quality(
    data_dir: Path,
    *,
    update_metadata: bool = False,
    allowed_failure_ids: set[str] | None = None,
    release_gate: bool = False,
    fail_on: str = "none",
) -> dict:
    profile_allowed = manifest_allowed_failure_ids(data_dir)
    if profile_allowed is not None and (update_metadata or release_gate):
        if allowed_failure_ids is None:
            allowed_failure_ids = profile_allowed
        elif set(allowed_failure_ids) != profile_allowed:
            raise CorpusMutationError(
                "CLI/API allowed_failure_ids must exactly match release_profile.allowed_failure_ids"
            )
    if not update_metadata:
        return _audit_data_quality(
            data_dir,
            update_metadata=False,
            allowed_failure_ids=allowed_failure_ids,
            release_gate=release_gate,
        )
    def update_staging(staging: Path) -> dict:
        report = _audit_data_quality(
            staging,
            update_metadata=True,
            allowed_failure_ids=allowed_failure_ids,
            release_gate=release_gate,
        )
        failures = report_failures(report, fail_on)
        if failures:
            raise CorpusMutationError(
                f"quality gate {fail_on!r} rejected staged corpus with {len(failures)} issue(s):\n"
                + "\n".join(failures[:20])
            )
        return report

    return mutate_staged_corpus(data_dir, "quality-update", update_staging)


def _audit_data_quality(
    data_dir: Path,
    *,
    update_metadata: bool = False,
    allowed_failure_ids: set[str] | None = None,
    release_gate: bool = False,
) -> dict:
    data_dir = Path(data_dir)
    docs = load_documents(data_dir)
    original_metadata = (
        {doc.id: document_metadata_fingerprint(doc) for doc in docs}
        if update_metadata
        else {}
    )
    attachment_count = 0
    asset_count = 0
    status_counts: Counter[str] = Counter()
    extension_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    text_lengths: list[int] = []
    formula_source_blocks = 0
    issues: list[dict[str, object]] = []

    for doc in docs:
        if update_metadata:
            doc.schema_version = CORPUS_SCHEMA_VERSION
        attachment_fallback_available = any(
            att.status == ATTACHMENT_CONVERTED
            and bool(att.text_path)
            and att.searchable is not False
            for att in doc.attachments
        )
        if not doc.body.strip():
            doc.quality_codes = add_quality_code(doc.quality_codes, "document_empty_body")
            doc.quality_status = "warn"
            doc.searchable = False
            severity = "error" if release_gate and not attachment_fallback_available else "warn"
            issues.append(document_issue(severity, doc, "document_empty_body", "document body is empty"))
        if doc.conversion_status == "failed":
            issues.append(
                document_issue(
                    "error" if release_gate and not attachment_fallback_available else "warn",
                    doc,
                    "required_conversion_failed",
                    "document body conversion failed",
                )
            )
        if "[이미지:" in doc.body:
            doc.quality_codes = add_quality_code(doc.quality_codes, "inline_image_missing")
            doc.quality_codes = add_quality_code(doc.quality_codes, "image_content_unindexed")
            if doc.quality_status in {"", "ok"}:
                doc.quality_status = "warn"
            issues.append(
                document_issue("warn", doc, "inline_image_missing", "document contains an unresolved inline image")
            )
        audit_document_source(data_dir, doc, issues)
        for asset in doc.assets:
            asset_count += 1
            audit_asset(doc, asset, issues)

    for doc in docs:
        for att in doc.attachments:
            attachment_count += 1
            status_counts[att.status or ATTACHMENT_PENDING] += 1
            extension_counts[Path(att.file_name or att.raw_path).suffix.lower() or "(none)"] += 1
            if att.status == ATTACHMENT_CONVERTED:
                formula_source_blocks += issue_quality_for_converted(
                    data_dir,
                    doc,
                    att,
                    issues,
                    quality_counts,
                    text_lengths,
                    update_metadata,
                )
            elif att.status == ATTACHMENT_FAILED:
                quality_counts["fail"] += 1
                if update_metadata:
                    mark_quality_failure(att, "conversion_failed")
                allowed_degraded = is_allowed_degraded_attachment(data_dir, att, allowed_failure_ids or set())
                required_failure = release_gate and not allowed_degraded
                issues.append(
                    issue(
                        "error" if required_failure else "warn",
                        doc,
                        att,
                        "required_conversion_failed" if required_failure else "conversion_failed",
                        att.error or "attachment conversion failed",
                    )
                )
            else:
                quality_counts["pending"] += 1
                if update_metadata:
                    mark_quality_failure(att, "conversion_pending")
                required_failure = release_gate
                issues.append(
                    issue(
                        "error" if required_failure else "warn",
                        doc,
                        att,
                        "required_conversion_failed" if required_failure else "conversion_pending",
                        "attachment has not been converted",
                    )
                )
            for asset in att.assets:
                asset_count += 1
                audit_asset(doc, asset, issues, att=att)

    report = {
        "version": REPORT_VERSION,
        "generated_at": now_utc(),
        "summary": {
            "documents": len(docs),
            "attachments": attachment_count,
            "assets": asset_count,
            "attachment_status": dict(sorted(status_counts.items())),
            "attachment_extensions": dict(sorted(extension_counts.items())),
            "quality_status": dict(sorted(quality_counts.items())),
            "converted_text_chars": length_summary(text_lengths),
            "hwp_equation_source_blocks": formula_source_blocks,
        },
        "issues": issues,
    }
    if update_metadata:
        changed_docs = [
            doc
            for doc in docs
            if document_metadata_fingerprint(doc) != original_metadata.get(doc.id)
        ]
        for doc in changed_docs:
            write_document(data_dir, doc)
        if changed_docs:
            write_manifest(data_dir, docs)
    return report


def is_allowed_degraded_attachment(data_dir: Path, att: Attachment, allowed_ids: set[str]) -> bool:
    if att.id not in allowed_ids or att.status != ATTACHMENT_FAILED:
        return False
    if att.preservation_status != "preserved" or att.searchable is not False or not att.raw_path:
        return False
    return (Path(data_dir) / att.raw_path).is_file()


def report_failures(report: dict, fail_on: str) -> list[str]:
    if fail_on == "none":
        return []
    severities = {"error"} if fail_on == "error" else {"error", "warn"}
    return [
        f"{item.get('severity')}: {item.get('code')} {item.get('document_id')}/{item.get('attachment_id', '')}"
        for item in report.get("issues", [])
        if item.get("severity") in severities
    ]


def document_metadata_fingerprint(doc: Document) -> str:
    return json.dumps(doc.to_mapping(), ensure_ascii=False, sort_keys=True)


def issue_quality_for_converted(
    data_dir: Path,
    doc: Document,
    att: Attachment,
    issues: list[dict[str, object]],
    quality_counts: Counter[str],
    text_lengths: list[int],
    update_metadata: bool,
) -> int:
    if not att.text_path:
        quality_counts["fail"] += 1
        if update_metadata:
            mark_quality_failure(att, "missing_text_path")
        issues.append(issue("error", doc, att, "missing_text_path", "converted attachment has no text_path"))
        return 0
    text_path = data_dir / att.text_path
    if not text_path.exists():
        quality_counts["fail"] += 1
        if update_metadata:
            mark_quality_failure(att, "missing_converted_file")
        issues.append(issue("error", doc, att, "missing_converted_file", f"missing converted text file {att.text_path}"))
        return 0
    raw_path = data_dir / att.raw_path if att.raw_path else None
    if text_path.stat().st_size > MAX_CONVERTED_TEXT_BYTES:
        quality_counts["fail"] += 1
        issues.append(issue("error", doc, att, "source_inspection_failed", "converted text exceeds byte limit"))
        return 0
    if raw_path is not None and raw_path.exists() and raw_path.stat().st_size > MAX_SOURCE_BYTES:
        quality_counts["fail"] += 1
        issues.append(issue("error", doc, att, "source_inspection_failed", "raw source exceeds byte limit"))
        return 0
    text = text_path.read_text(encoding="utf-8", errors="replace")
    inspection_cache = SourceInspectionCache()
    quality = inspect_attachment_quality(text, raw_path, inspection_cache)
    text_lengths.append(quality.text_chars)
    quality_counts[quality.status] += 1
    if update_metadata:
        retained_codes = parse_quality_codes(
            [
                *att.quality_codes,
                *(item.get("code", "") for item in att.diagnostics),
            ]
        )
        apply_quality(att, quality)
        att.quality_codes = parse_quality_codes([*att.quality_codes, *retained_codes])
        att.quality_flags = ",".join(att.quality_codes)
    severity = "error" if quality.status == "fail" else "warn"
    for flag in quality.flags:
        issues.append(issue(severity, doc, att, flag, quality_message(flag, quality)))
    if raw_path is not None and raw_path.exists():
        diagnostics, searchable = inspect_converted_source(
            raw_path,
            text,
            inspection_cache=inspection_cache,
            assets=att.assets,
        )
        for diagnostic in diagnostics:
            issues.append(issue("warn", doc, att, diagnostic.code, diagnostic.message))
        if update_metadata and diagnostics:
            for diagnostic in diagnostics:
                att.quality_codes = add_quality_code(att.quality_codes, diagnostic.code)
            att.quality_flags = ",".join(att.quality_codes)
            att.searchable = searchable
            if att.quality_status in {"", "ok"}:
                att.quality_status = "warn"
    existing_issue_keys = {
        (str(item.get("code") or ""), str(item.get("message") or ""))
        for item in issues
        if item.get("document_id") == doc.id and item.get("attachment_id") == att.id
    }
    for diagnostic in att.diagnostics:
        code = str(diagnostic.get("code") or "")
        message = str(diagnostic.get("message") or code)
        if code and (code, message) not in existing_issue_keys:
            issues.append(issue(str(diagnostic.get("severity") or "warn"), doc, att, code, message))
    return quality.formula_block_count


def quality_message(flag: str, quality: AttachmentQuality) -> str:
    messages = {
        "empty_text": "converted text is empty",
        "very_short_text": f"converted text is very short ({quality.non_space_chars} non-space chars)",
        "replacement_characters": f"converted text contains {quality.replacement_char_count} replacement character(s)",
        "very_long_lines": "converted text contains very long lines; table or paragraph boundaries may be lost",
        "raw_table_hints_without_table_text": "raw attachment has table tags but converted text has no table-like rows",
        "raw_table_cells_may_be_flattened": (
            "raw attachment has many table cells but converted Markdown has far fewer table cells "
            f"({quality.table_cell_count}/{quality.raw_table_cell_hint_count}); nested or layout tables may be flattened"
        ),
        "raw_formula_hints_without_formula_text": "raw attachment has formula hints but converted text has no formula-like text",
        "source_inspection_failed": quality.source_inspection_error or "raw source structure inspection failed",
        "formula_source_count_mismatch": (
            "raw HWP EqEdit count does not match preserved hwp-equation source blocks "
            f"({quality.raw_formula_hint_count}/{quality.formula_block_count})"
        ),
        "formula_generated_latex_invalid": (
            "one or more preserved HWP equation source blocks have no generated math block"
        ),
    }
    return messages.get(flag, flag)


def issue(severity: str, doc: Document, att: Attachment, code: str, message: str) -> dict[str, object]:
    return {
        "severity": severity,
        "code": code,
        "document_id": doc.id,
        "document_title": doc.title,
        "attachment_id": att.id,
        "attachment_title": att.title,
        "file_name": att.file_name,
        "message": message,
    }


def document_issue(severity: str, doc: Document, code: str, message: str) -> dict[str, object]:
    return {
        "severity": severity,
        "code": code,
        "document_id": doc.id,
        "document_title": doc.title,
        "message": message,
    }


def audit_asset(
    doc: Document,
    asset,
    issues: list[dict[str, object]],
    *,
    att: Attachment | None = None,
) -> None:
    base = {
        "severity": "warn",
        "document_id": doc.id,
        "document_title": doc.title,
        "asset_id": asset.id,
        "source_anchor": asset.source_anchor,
        **({"attachment_id": att.id, "attachment_title": att.title} if att is not None else {}),
    }
    if asset.preservation_status != "preserved":
        code = "inline_image_missing" if asset.source_kind == "html_inline" else "hwp_picture_missing"
        issues.append(base | {"code": code, "message": asset.error or "binary asset was not preserved"})
        return
    issues.append(
        base
        | {
            "code": "image_content_unindexed",
            "message": f"preserved image asset is not text searchable ({asset.width}x{asset.height})",
        }
    )


def audit_document_source(data_dir: Path, doc: Document, issues: list[dict[str, object]]) -> None:
    if not doc.raw_path or not doc.text_path:
        return
    raw_path = data_dir / doc.raw_path
    text_path = data_dir / doc.text_path
    if not raw_path.exists() or not text_path.exists():
        return
    if raw_path.stat().st_size > MAX_SOURCE_BYTES or text_path.stat().st_size > MAX_CONVERTED_TEXT_BYTES:
        issues.append(document_issue("error", doc, "source_inspection_failed", "document source exceeds byte limit"))
        return
    text = text_path.read_text(encoding="utf-8", errors="replace")
    inspection_cache = SourceInspectionCache()
    quality = inspect_attachment_quality(text, raw_path, inspection_cache)
    for flag in quality.flags:
        issues.append(document_issue("warn", doc, flag, quality_message(flag, quality)))
    if quality.flags:
        doc.quality_codes = parse_quality_codes([*doc.quality_codes, *quality.flags])
        if doc.quality_status in {"", "ok"}:
            doc.quality_status = "warn"
    diagnostics, searchable = inspect_converted_source(
        raw_path,
        text,
        inspection_cache=inspection_cache,
        assets=doc.assets,
    )
    for diagnostic in diagnostics:
        issues.append(document_issue("warn", doc, diagnostic.code, diagnostic.message))
        doc.quality_codes = add_quality_code(doc.quality_codes, diagnostic.code)
    if diagnostics:
        doc.searchable = searchable
        if doc.quality_status in {"", "ok"}:
            doc.quality_status = "warn"


def length_summary(values: list[int]) -> dict[str, int]:
    if not values:
        return {"count": 0, "min": 0, "median": 0, "max": 0}
    return {
        "count": len(values),
        "min": min(values),
        "median": int(statistics.median(values)),
        "max": max(values),
    }


def write_quality_report(path: Path, report: dict) -> None:
    try:
        existing = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if existing is not None and report_without_generated_at(existing) == report_without_generated_at(report):
        return
    atomic_write_json(path, report)


def report_without_generated_at(report: dict) -> dict:
    return {key: value for key, value in report.items() if key != "generated_at"}


def write_manifest(data_dir: Path, docs: list[Document]) -> None:
    old: dict[str, object] = {}
    path = data_dir / "manifest.json"
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            old = json.load(fh)
    write_manifest_atomic(
        data_dir,
        docs,
        source=str(old.get("source", "")),
        version=str(old.get("version", "0.1.0")),
        release_profile=old.get("release_profile") if isinstance(old.get("release_profile"), dict) else None,
    )


def relative_doc_path(data_dir: Path, doc: Document) -> str:
    if not doc.path:
        return ""
    path = Path(doc.path)
    try:
        return str(path.relative_to(data_dir))
    except ValueError:
        return str(path)
