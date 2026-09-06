from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys

from .contracts import (
    CONVERTER_VERSION,
    MAX_CONVERTED_TEXT_BYTES,
    MAX_SOURCE_BYTES,
    add_quality_code,
    canonical_text_hash,
    converter_version_for_source,
    sha256_file,
)
from .assets import preserve_hwp_attachment_assets
from .convert import convert_attachment
from .converters.cache import SourceInspectionCache
from .converters.core import convert_bytes_outcome
from .converters.base import normalize_converted_text
from .manifest import manifest_allowed_failure_ids
from .markdown import load_documents, write_document
from .models import ATTACHMENT_CONVERTED, LANGUAGE_EN, Attachment, hash_text, now_utc
from .paths import converted_attachment_path
from .quality import audit_data_quality, is_allowed_degraded_attachment, report_failures, write_manifest
from .repository import CorpusMutationError, mutate_staged_corpus, write_run_report
from .sync import english_rule_title


@dataclass
class ReconvertResult:
    documents: int = 0
    attachments: int = 0
    converted: int = 0
    # Backward-compatible total of blocking failures; allowlisted failures are
    # tracked separately and are not included.
    failed: int = 0
    skipped: int = 0
    metadata_updates: int = 0
    stale_retained: int = 0
    allowed_failed: int = 0
    required_failed: int = 0
    inspection_failed: int = 0
    quality_failed: int = 0
    failure_events: list[dict[str, str]] = field(default_factory=list, repr=False)

    @property
    def has_blocking_failures(self) -> bool:
        return bool(
            self.required_failed
            or self.stale_retained
            or self.inspection_failed
            or self.quality_failed
        )


def reconvert_data(
    data_dir: Path,
    *,
    document_id: str = "",
    dry_run: bool = False,
    force: bool = False,
) -> ReconvertResult:
    if dry_run:
        return _reconvert_data(
            data_dir,
            document_id=document_id,
            dry_run=True,
            force=force,
            allowed_failure_ids=manifest_allowed_failure_ids(data_dir) or set(),
        )

    def operation(staging: Path) -> ReconvertResult:
        allowed_failure_ids = manifest_allowed_failure_ids(staging) or set()
        result = _reconvert_data(
            staging,
            document_id=document_id,
            dry_run=False,
            force=force,
            allowed_failure_ids=allowed_failure_ids,
        )
        if result.has_blocking_failures:
            raise result_error(
                result,
                f"reconvert aborted after {result.failed} blocking failure(s)",
            )
        quality_report = audit_data_quality(
            staging,
            release_gate=True,
            allowed_failure_ids=allowed_failure_ids,
        )
        quality_errors = report_failures(quality_report, "error")
        if quality_errors:
            result.quality_failed += len(quality_errors)
            result.failed += len(quality_errors)
            for issue in quality_report.get("issues", []):
                if issue.get("severity") != "error":
                    continue
                result.failure_events.append(
                    {
                        "document_id": str(issue.get("document_id") or ""),
                        "attachment_id": str(issue.get("attachment_id") or ""),
                        "outcome": "quality_failed",
                        "failed_at": now_utc(),
                        "error": f"{issue.get('code')}: {issue.get('message')}",
                    }
                )
            raise result_error(
                result,
                f"reconvert release quality gate rejected staging with {len(quality_errors)} error(s):\n"
                + "\n".join(quality_errors[:20]),
            )
        return result

    result: ReconvertResult | None = None
    run_error = ""
    try:
        result = mutate_staged_corpus(
            data_dir,
            "reconvert",
            operation,
        )
        return result
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        failed_result = getattr(exc, "reconvert_result", None)
        if isinstance(failed_result, ReconvertResult):
            result = failed_result
        raise
    finally:
        try:
            write_run_report(
                data_dir,
                "reconvert",
                result.failure_events if result is not None else [],
                "failed" if run_error else "ok",
                error=run_error,
            )
        except OSError as exc:
            print(f"warning: could not write reconvert run report: {exc}", file=sys.stderr)


def _reconvert_data(
    data_dir: Path,
    *,
    document_id: str = "",
    dry_run: bool = False,
    force: bool = False,
    allowed_failure_ids: set[str] | None = None,
) -> ReconvertResult:
    docs = load_documents(data_dir)
    result = ReconvertResult()
    allowed_failure_ids = set(allowed_failure_ids or set())
    touched_docs = []
    for doc in docs:
        if document_id and doc.id != document_id and doc.source_id != document_id:
            continue
        result.documents += 1
        changed = False
        if doc.language == LANGUAGE_EN and doc.file_name:
            normalized_title = english_rule_title(doc.file_name, doc.title)
            if normalized_title != doc.title:
                result.metadata_updates += 1
                if not dry_run:
                    doc.title = normalized_title
                    doc.content_hash = hash_text(doc.title + "\n" + doc.body)
                    changed = True
        if doc.source_content_path:
            source_path = data_dir / doc.source_content_path
            if not source_path.exists():
                record_failure(
                    result,
                    doc.id,
                    "",
                    "required",
                    f"missing document source {doc.source_content_path}",
                    dry_run=dry_run,
                )
            elif source_path.stat().st_size > MAX_SOURCE_BYTES:
                record_failure(
                    result,
                    doc.id,
                    "",
                    "required",
                    f"document source exceeds {MAX_SOURCE_BYTES} bytes",
                    dry_run=dry_run,
                )
            elif dry_run:
                result.converted += 1
            else:
                try:
                    outcome = convert_bytes_outcome(source_path, source_path.read_bytes())
                    body = normalize_converted_text(outcome.text)
                    if not body:
                        raise ValueError("source HTML conversion produced empty text")
                except Exception as exc:  # noqa: BLE001 - retain the last-known-good index.md.
                    record_failure(result, doc.id, "", "stale", str(exc), dry_run=False)
                else:
                    result.converted += 1
                    if body != doc.body or doc.converter_version != CONVERTER_VERSION:
                        doc.body = body
                        doc.content_hash = hash_text(doc.title + "\n" + doc.body)
                        doc.body_hash = canonical_text_hash(doc.body)
                        doc.converter_version = CONVERTER_VERSION
                        doc.conversion_status = "converted"
                        doc.searchable = outcome.searchable
                        for diagnostic in outcome.diagnostics:
                            doc.quality_codes = add_quality_code(doc.quality_codes, diagnostic.code)
                        changed = True
        if doc.raw_path:
            result.attachments += 1
            raw_path = data_dir / doc.raw_path
            if not raw_path.exists() or not doc.text_path:
                record_failure(
                    result,
                    doc.id,
                    "",
                    "required",
                    "document raw source or converted text path is missing",
                    dry_run=dry_run,
                )
            else:
                current_check_failed = False
                try:
                    current = not force and document_file_is_current(data_dir, doc, raw_path)
                except Exception as exc:  # noqa: BLE001 - bounded source validation failure.
                    record_failure(result, doc.id, "", "required", str(exc), dry_run=dry_run)
                    current = False
                    current_check_failed = True
                if current_check_failed:
                    pass
                elif current:
                    result.skipped += 1
                elif dry_run:
                    result.converted += 1
                else:
                    had_lkg = document_has_lkg(data_dir, doc)
                    try:
                        pseudo_attachment = convert_document_file(data_dir, doc, raw_path)
                    except Exception as exc:  # noqa: BLE001 - staging must retain the active LKG.
                        record_failure(
                            result,
                            doc.id,
                            "",
                            "stale" if had_lkg else "required",
                            str(exc),
                            dry_run=False,
                        )
                    else:
                        if pseudo_attachment.status == ATTACHMENT_CONVERTED and pseudo_attachment.text_path:
                            inspection_errors = hwp_inspection_errors(pseudo_attachment, raw_path)
                            if inspection_errors:
                                record_failure(
                                    result,
                                    doc.id,
                                    "",
                                    "inspection",
                                    "; ".join(inspection_errors),
                                    dry_run=False,
                                )
                            else:
                                result.converted += 1
                            changed = True
                        else:
                            record_failure(
                                result,
                                doc.id,
                                "",
                                "stale" if had_lkg else "required",
                                pseudo_attachment.error or "document conversion failed",
                                dry_run=False,
                            )
        used_converted_names = existing_converted_names(doc)
        for attachment_index, att in enumerate(doc.attachments):
            result.attachments += 1
            if (
                not force
                and is_allowed_degraded_attachment(data_dir, att, allowed_failure_ids)
            ):
                result.allowed_failed += 1
                continue
            if not att.raw_path:
                if att.status == ATTACHMENT_CONVERTED:
                    result.skipped += 1
                else:
                    record_failure(
                        result,
                        doc.id,
                        att.id,
                        "required",
                        "attachment has no raw source to reconvert",
                        dry_run=dry_run,
                    )
                continue
            raw_path = data_dir / att.raw_path
            if not raw_path.exists():
                record_failure(
                    result,
                    doc.id,
                    att.id,
                    "required",
                    f"missing attachment raw source {att.raw_path}",
                    dry_run=dry_run,
                )
                continue
            try:
                current = not force and attachment_is_current(data_dir, att, raw_path)
            except Exception as exc:  # noqa: BLE001 - bounded source validation failure.
                record_failure(result, doc.id, att.id, "required", str(exc), dry_run=dry_run)
                continue
            if current:
                if raw_path.suffix.lower() == ".hwp" and att.asset_inspection_version != "1":
                    if dry_run:
                        result.converted += 1
                        continue
                    cache = SourceInspectionCache()
                    streams, inspection_error = cache.hwp_images(raw_path)
                    if inspection_error:
                        record_failure(
                            result,
                            doc.id,
                            att.id,
                            "inspection",
                            inspection_error,
                            dry_run=False,
                        )
                        continue
                    try:
                        preserve_hwp_attachment_assets(data_dir, doc, att, streams=streams)
                    except Exception as exc:  # noqa: BLE001 - partial asset output is staging-only.
                        record_failure(
                            result,
                            doc.id,
                            att.id,
                            "inspection",
                            str(exc),
                            dry_run=False,
                        )
                        continue
                    changed = True
                    result.converted += 1
                else:
                    result.skipped += 1
                continue
            if dry_run:
                result.converted += 1
                continue
            text_path = data_dir / att.text_path if att.text_path else converted_attachment_path(data_dir, doc, att, used_converted_names)
            inspection_cache = SourceInspectionCache()
            had_lkg = attachment_has_lkg(data_dir, att)
            try:
                att = convert_attachment(raw_path, text_path, att, inspection_cache=inspection_cache)
            except Exception as exc:  # noqa: BLE001 - normalize converter failures into result provenance.
                record_failure(
                    result,
                    doc.id,
                    att.id,
                    "stale" if had_lkg else "required",
                    str(exc),
                    dry_run=False,
                )
                continue
            doc.attachments[attachment_index] = att
            att.raw_path = str(raw_path.relative_to(data_dir))
            if att.last_refresh_error:
                record_failure(
                    result,
                    doc.id,
                    att.id,
                    "stale",
                    att.last_refresh_error,
                    dry_run=False,
                )
            elif att.status == ATTACHMENT_CONVERTED and att.text_path:
                att.text_path = str(text_path.relative_to(data_dir))
                inspection_errors = hwp_inspection_errors(att, raw_path)
                if raw_path.suffix.lower() == ".hwp":
                    streams, inspection_error = inspection_cache.hwp_images(raw_path)
                    if inspection_error:
                        inspection_errors.append(inspection_error)
                    else:
                        try:
                            preserve_hwp_attachment_assets(data_dir, doc, att, streams=streams)
                        except Exception as exc:  # noqa: BLE001 - partial asset output is staging-only.
                            inspection_errors.append(str(exc))
                if inspection_errors:
                    record_failure(
                        result,
                        doc.id,
                        att.id,
                        "inspection",
                        "; ".join(inspection_errors),
                        dry_run=False,
                    )
                else:
                    result.converted += 1
            else:
                record_failure(
                    result,
                    doc.id,
                    att.id,
                    "stale" if had_lkg else "required",
                    att.error or "attachment conversion failed",
                    dry_run=False,
                )
            changed = True
        if changed:
            refresh_document_body_from_text_path(data_dir, doc)
            write_document(data_dir, doc)
            touched_docs.append(doc)
    if not dry_run and touched_docs:
        write_manifest(data_dir, docs)
    return result


def record_failure(
    result: ReconvertResult,
    document_id: str,
    attachment_id: str,
    category: str,
    error: str,
    *,
    dry_run: bool,
) -> None:
    result.failed += 1
    outcome = "failed"
    if category == "stale":
        result.stale_retained += 1
        outcome = "stale"
    elif category == "inspection":
        result.inspection_failed += 1
        outcome = "inspection_failed"
    else:
        result.required_failed += 1
    if not dry_run:
        result.failure_events.append(
            {
                "document_id": document_id,
                "attachment_id": attachment_id,
                "outcome": outcome,
                "failed_at": now_utc(),
                "error": error,
            }
        )


def result_error(result: ReconvertResult, message: str) -> CorpusMutationError:
    error = CorpusMutationError(message)
    error.reconvert_result = result
    return error


def attachment_has_lkg(data_dir: Path, att: Attachment) -> bool:
    return bool(
        att.status == ATTACHMENT_CONVERTED
        and att.text_path
        and (Path(data_dir) / att.text_path).is_file()
    )


def document_has_lkg(data_dir: Path, doc) -> bool:
    return bool(
        doc.conversion_status == "converted"
        and doc.text_path
        and (Path(data_dir) / doc.text_path).is_file()
    )


def hwp_inspection_errors(att: Attachment, raw_path: Path) -> list[str]:
    if raw_path.suffix.lower() != ".hwp":
        return []
    return [
        str(item.get("message") or "HWP source inspection failed")
        for item in att.diagnostics
        if item.get("code") == "source_inspection_failed"
    ]


def existing_converted_names(doc) -> set[str]:
    names: set[str] = set()
    for att in doc.attachments:
        if att.text_path:
            names.add(Path(att.text_path).name)
    return names


def attachment_is_current(data_dir: Path, att: Attachment, raw_path: Path) -> bool:
    if att.status != ATTACHMENT_CONVERTED or att.converter_version != converter_version_for_source(raw_path):
        return False
    if (att.raw_file_hash or att.content_hash) != sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES):
        return False
    if not att.text_path:
        return False
    text_path = data_dir / att.text_path
    if not text_path.is_file():
        return False
    if text_path.stat().st_size > MAX_CONVERTED_TEXT_BYTES:
        return False
    return att.converted_text_hash == canonical_text_hash(
        text_path.read_text(encoding="utf-8", errors="strict")
    )


def document_file_is_current(data_dir: Path, doc, raw_path: Path) -> bool:
    if doc.converter_version != converter_version_for_source(raw_path):
        return False
    if (doc.raw_file_hash or doc.file_content_hash) != sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES):
        return False
    text_path = data_dir / doc.text_path
    if not text_path.is_file():
        return False
    if text_path.stat().st_size > MAX_CONVERTED_TEXT_BYTES:
        return False
    return canonical_text_hash(doc.body) == canonical_text_hash(
        text_path.read_text(encoding="utf-8", errors="strict")
    )


def convert_document_file(data_dir: Path, doc, raw_path: Path):
    text_path = data_dir / doc.text_path
    pseudo_attachment = Attachment(
        id=doc.id,
        title=doc.title,
        file_name=doc.file_name or raw_path.name,
        raw_path=doc.raw_path,
        text_path=doc.text_path,
    )
    pseudo_attachment = convert_attachment(raw_path, text_path, pseudo_attachment)
    if pseudo_attachment.status != ATTACHMENT_CONVERTED or not pseudo_attachment.text_path:
        return pseudo_attachment
    doc.raw_path = str(raw_path.relative_to(data_dir))
    doc.text_path = str(text_path.relative_to(data_dir))
    doc.file_content_hash = pseudo_attachment.content_hash
    doc.raw_file_hash = pseudo_attachment.raw_file_hash or pseudo_attachment.content_hash
    doc.converter_version = pseudo_attachment.converter_version or converter_version_for_source(raw_path)
    refresh_document_body_from_text_path(data_dir, doc)
    return pseudo_attachment


def refresh_document_body_from_text_path(data_dir: Path, doc) -> None:
    if not doc.text_path:
        return
    text_path = data_dir / doc.text_path
    if not text_path.exists():
        return
    doc.body = text_path.read_text(encoding="utf-8").strip()
    doc.content_hash = hash_text(doc.title + "\n" + doc.body)
    doc.body_hash = canonical_text_hash(doc.body)
