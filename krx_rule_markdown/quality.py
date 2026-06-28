from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import re
import statistics
import zipfile

from .markdown import load_documents, write_document
from .models import (
    ATTACHMENT_CONVERTED,
    ATTACHMENT_FAILED,
    ATTACHMENT_PENDING,
    Attachment,
    Document,
    now_utc,
)


REPORT_VERSION = "0.1.0"
FORMULA_RE = re.compile(
    r"(≤|≥|≠|±|×|÷|√|∑|∫|∞|∂|→|←|"
    r"\b[A-Za-z]\s*[=<>]\s*[-+0-9A-Za-z(]|[0-9]\s*[+\-*/]\s*[0-9]|"
    r"\b(?:hat|sum|Isum|LEFT|RIGHT|over|sqrt|root|matrix|dmatrix)\b|"
    r"수식\s+\d+:|```hwp-equation)"
)


@dataclass
class AttachmentQuality:
    status: str
    score: int
    flags: list[str]
    text_chars: int
    non_space_chars: int
    line_count: int
    table_row_count: int
    formula_hint_count: int
    replacement_char_count: int
    raw_table_hint_count: int = 0
    raw_formula_hint_count: int = 0


def inspect_attachment_quality(text: str, raw_path: Path | None = None) -> AttachmentQuality:
    text_chars = len(text)
    non_space_chars = sum(1 for ch in text if not ch.isspace())
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    table_row_count = sum(1 for line in lines if is_table_like_line(line))
    formula_hint_count = len(FORMULA_RE.findall(text))
    replacement_char_count = text.count("\ufffd")
    raw_table_hints, raw_formula_hints = raw_structure_hints(raw_path)

    flags: list[str] = []
    if text_chars == 0:
        flags.append("empty_text")
    if 0 < non_space_chars < 40:
        flags.append("very_short_text")
    if replacement_char_count:
        flags.append("replacement_characters")
    if max((len(line) for line in lines), default=0) > 1200:
        flags.append("very_long_lines")
    if raw_table_hints > 0 and table_row_count == 0:
        flags.append("raw_table_hints_without_table_text")
    if raw_formula_hints > 0 and formula_hint_count == 0:
        flags.append("raw_formula_hints_without_formula_text")

    score = 100
    penalties = {
        "empty_text": 100,
        "very_short_text": 45,
        "replacement_characters": min(30, replacement_char_count * 3),
        "very_long_lines": 15,
        "raw_table_hints_without_table_text": 25,
        "raw_formula_hints_without_formula_text": 25,
    }
    for flag in flags:
        score -= penalties.get(flag, 10)
    score = max(0, min(100, score))
    status = "fail" if "empty_text" in flags else "warn" if flags else "ok"
    return AttachmentQuality(
        status=status,
        score=score,
        flags=flags,
        text_chars=text_chars,
        non_space_chars=non_space_chars,
        line_count=len(lines),
        table_row_count=table_row_count,
        formula_hint_count=formula_hint_count,
        replacement_char_count=replacement_char_count,
        raw_table_hint_count=raw_table_hints,
        raw_formula_hint_count=raw_formula_hints,
    )


def is_table_like_line(line: str) -> bool:
    stripped = line.strip()
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


def raw_structure_hints(raw_path: Path | None) -> tuple[int, int]:
    if raw_path is None or not raw_path.exists():
        return 0, 0
    if raw_path.suffix.lower() == ".hwp":
        return 0, hwp_eqedit_count(raw_path)
    if raw_path.suffix.lower() != ".hwpx":
        return 0, 0
    table_hints = 0
    formula_hints = 0
    try:
        with zipfile.ZipFile(raw_path) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if not lower.endswith(".xml"):
                    continue
                xml = zf.read(name).decode("utf-8", errors="replace")
                table_hints += len(re.findall(r"(<[^>]*:?tbl\b|<[^>]*:?tr\b|<[^>]*:?tc\b)", xml, re.I))
                formula_hints += len(re.findall(r"(equation|formula|수식|<[^>]*:?eq\b)", xml, re.I))
    except (OSError, zipfile.BadZipFile):
        return 0, 0
    return table_hints, formula_hints


def hwp_eqedit_count(raw_path: Path) -> int:
    try:
        from hwp5.binmodel import EqEdit
        from hwp5.proc.find import hwp5file_models
    except ImportError:
        return 0

    count = 0
    try:
        for model in hwp5file_models(str(raw_path)):
            if model.get("type") is EqEdit:
                count += 1
    except Exception:
        return 0
    return count


def apply_quality(att: Attachment, quality: AttachmentQuality) -> Attachment:
    att.quality_status = quality.status
    att.quality_score = quality.score
    att.quality_flags = ",".join(quality.flags)
    att.converted_text_chars = quality.text_chars
    att.converted_non_space_chars = quality.non_space_chars
    att.table_row_count = quality.table_row_count
    att.formula_hint_count = quality.formula_hint_count
    att.replacement_char_count = quality.replacement_char_count
    return att


def mark_quality_failure(att: Attachment, flag: str) -> Attachment:
    att.quality_status = "fail"
    att.quality_score = 0
    att.quality_flags = flag
    att.converted_text_chars = 0
    att.converted_non_space_chars = 0
    att.table_row_count = 0
    att.formula_hint_count = 0
    att.replacement_char_count = 0
    return att


def audit_data_quality(data_dir: Path, *, update_metadata: bool = False) -> dict:
    data_dir = Path(data_dir)
    docs = load_documents(data_dir)
    attachment_count = 0
    status_counts: Counter[str] = Counter()
    extension_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    text_lengths: list[int] = []
    issues: list[dict[str, object]] = []

    for doc in docs:
        for att in doc.attachments:
            attachment_count += 1
            status_counts[att.status or ATTACHMENT_PENDING] += 1
            extension_counts[Path(att.file_name or att.raw_path).suffix.lower() or "(none)"] += 1
            if att.status == ATTACHMENT_CONVERTED:
                issue_quality_for_converted(data_dir, doc, att, issues, quality_counts, text_lengths, update_metadata)
            elif att.status == ATTACHMENT_FAILED:
                quality_counts["fail"] += 1
                if update_metadata:
                    mark_quality_failure(att, "conversion_failed")
                issues.append(issue("warn", doc, att, "conversion_failed", att.error or "attachment conversion failed"))
            else:
                quality_counts["pending"] += 1
                if update_metadata:
                    mark_quality_failure(att, "conversion_pending")
                issues.append(issue("warn", doc, att, "conversion_pending", "attachment has not been converted"))

    report = {
        "version": REPORT_VERSION,
        "generated_at": now_utc(),
        "summary": {
            "documents": len(docs),
            "attachments": attachment_count,
            "attachment_status": dict(sorted(status_counts.items())),
            "attachment_extensions": dict(sorted(extension_counts.items())),
            "quality_status": dict(sorted(quality_counts.items())),
            "converted_text_chars": length_summary(text_lengths),
        },
        "issues": issues,
    }
    if update_metadata:
        for doc in docs:
            write_document(data_dir, doc)
        write_manifest(data_dir, docs)
    return report


def issue_quality_for_converted(
    data_dir: Path,
    doc: Document,
    att: Attachment,
    issues: list[dict[str, object]],
    quality_counts: Counter[str],
    text_lengths: list[int],
    update_metadata: bool,
) -> None:
    if not att.text_path:
        quality_counts["fail"] += 1
        if update_metadata:
            mark_quality_failure(att, "missing_text_path")
        issues.append(issue("error", doc, att, "missing_text_path", "converted attachment has no text_path"))
        return
    text_path = data_dir / att.text_path
    if not text_path.exists():
        quality_counts["fail"] += 1
        if update_metadata:
            mark_quality_failure(att, "missing_converted_file")
        issues.append(issue("error", doc, att, "missing_converted_file", f"missing converted text file {att.text_path}"))
        return
    raw_path = data_dir / att.raw_path if att.raw_path else None
    text = text_path.read_text(encoding="utf-8", errors="replace")
    quality = inspect_attachment_quality(text, raw_path)
    text_lengths.append(quality.text_chars)
    quality_counts[quality.status] += 1
    if update_metadata:
        apply_quality(att, quality)
    severity = "error" if quality.status == "fail" else "warn"
    for flag in quality.flags:
        issues.append(issue(severity, doc, att, flag, quality_message(flag, quality)))


def quality_message(flag: str, quality: AttachmentQuality) -> str:
    messages = {
        "empty_text": "converted text is empty",
        "very_short_text": f"converted text is very short ({quality.non_space_chars} non-space chars)",
        "replacement_characters": f"converted text contains {quality.replacement_char_count} replacement character(s)",
        "very_long_lines": "converted text contains very long lines; table or paragraph boundaries may be lost",
        "raw_table_hints_without_table_text": "raw HWPX has table tags but converted text has no table-like rows",
        "raw_formula_hints_without_formula_text": "raw attachment has formula hints but converted text has no formula-like text",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_manifest(data_dir: Path, docs: list[Document]) -> None:
    old: dict[str, object] = {}
    path = data_dir / "manifest.json"
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            old = json.load(fh)
    attachment_log = [att for doc in docs for att in doc.attachments]
    payload = {
        "version": old.get("version", "0.1.0"),
        "generated_at": now_utc(),
        "source": old.get("source", ""),
        "documents": [
            doc.to_mapping() | {"path": relative_doc_path(data_dir, doc)}
            for doc in docs
        ],
        "attachment_log": [att.to_mapping() for att in attachment_log],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relative_doc_path(data_dir: Path, doc: Document) -> str:
    if not doc.path:
        return ""
    path = Path(doc.path)
    try:
        return str(path.relative_to(data_dir))
    except ValueError:
        return str(path)
