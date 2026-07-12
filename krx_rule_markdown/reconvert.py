from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import (
    CONVERTER_VERSION,
    MAX_CONVERTED_TEXT_BYTES,
    MAX_SOURCE_BYTES,
    add_quality_code,
    canonical_text_hash,
    sha256_file,
)
from .assets import preserve_hwp_attachment_assets
from .convert import convert_attachment
from .converters.cache import SourceInspectionCache
from .converters.core import convert_bytes_outcome
from .converters.base import normalize_converted_text
from .markdown import load_documents, write_document
from .models import ATTACHMENT_CONVERTED, LANGUAGE_EN, Attachment, hash_text
from .paths import converted_attachment_path
from .quality import write_manifest
from .repository import mutate_staged_corpus
from .sync import english_rule_title


@dataclass
class ReconvertResult:
    documents: int = 0
    attachments: int = 0
    converted: int = 0
    failed: int = 0
    skipped: int = 0


def reconvert_data(
    data_dir: Path,
    *,
    document_id: str = "",
    dry_run: bool = False,
    force: bool = False,
) -> ReconvertResult:
    if dry_run:
        return _reconvert_data(data_dir, document_id=document_id, dry_run=True, force=force)
    return mutate_staged_corpus(
        data_dir,
        "reconvert",
        lambda staging: _reconvert_data(staging, document_id=document_id, dry_run=False, force=force),
    )


def _reconvert_data(
    data_dir: Path,
    *,
    document_id: str = "",
    dry_run: bool = False,
    force: bool = False,
) -> ReconvertResult:
    docs = load_documents(data_dir)
    result = ReconvertResult()
    touched_docs = []
    for doc in docs:
        if document_id and doc.id != document_id and doc.source_id != document_id:
            continue
        result.documents += 1
        changed = False
        if doc.language == LANGUAGE_EN and doc.file_name:
            normalized_title = english_rule_title(doc.file_name, doc.title)
            if normalized_title != doc.title:
                doc.title = normalized_title
                doc.content_hash = hash_text(doc.title + "\n" + doc.body)
                changed = True
        if doc.source_content_path:
            source_path = data_dir / doc.source_content_path
            if not source_path.exists():
                result.failed += 1
            elif source_path.stat().st_size > MAX_SOURCE_BYTES:
                result.failed += 1
            elif dry_run:
                result.converted += 1
            else:
                try:
                    outcome = convert_bytes_outcome(source_path, source_path.read_bytes())
                    body = normalize_converted_text(outcome.text)
                    if not body:
                        raise ValueError("source HTML conversion produced empty text")
                except Exception:  # noqa: BLE001 - retain the last-known-good index.md.
                    result.failed += 1
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
                result.failed += 1
            elif not force and document_file_is_current(data_dir, doc, raw_path):
                result.skipped += 1
            elif dry_run:
                result.converted += 1
            else:
                pseudo_attachment = convert_document_file(data_dir, doc, raw_path)
                if pseudo_attachment.status == ATTACHMENT_CONVERTED and pseudo_attachment.text_path:
                    result.converted += 1
                    changed = True
                else:
                    result.failed += 1
        used_converted_names = existing_converted_names(doc)
        for att in doc.attachments:
            result.attachments += 1
            if not att.raw_path:
                result.skipped += 1
                continue
            raw_path = data_dir / att.raw_path
            if not raw_path.exists():
                result.failed += 1
                continue
            if dry_run:
                result.converted += 1
                continue
            if not force and attachment_is_current(data_dir, att, raw_path):
                if raw_path.suffix.lower() == ".hwp" and att.asset_inspection_version != "1":
                    cache = SourceInspectionCache()
                    streams, inspection_error = cache.hwp_images(raw_path)
                    if inspection_error:
                        result.failed += 1
                        continue
                    preserve_hwp_attachment_assets(data_dir, doc, att, streams=streams)
                    changed = True
                    result.converted += 1
                else:
                    result.skipped += 1
                continue
            text_path = data_dir / att.text_path if att.text_path else converted_attachment_path(data_dir, doc, att, used_converted_names)
            inspection_cache = SourceInspectionCache()
            att = convert_attachment(raw_path, text_path, att, inspection_cache=inspection_cache)
            att.raw_path = str(raw_path.relative_to(data_dir))
            if att.status == ATTACHMENT_CONVERTED and att.text_path:
                att.text_path = str(text_path.relative_to(data_dir))
                if raw_path.suffix.lower() == ".hwp":
                    streams, inspection_error = inspection_cache.hwp_images(raw_path)
                    if not inspection_error:
                        preserve_hwp_attachment_assets(data_dir, doc, att, streams=streams)
                result.converted += 1
            else:
                result.failed += 1
            changed = True
        if changed:
            refresh_document_body_from_text_path(data_dir, doc)
            write_document(data_dir, doc)
            touched_docs.append(doc)
    if not dry_run and touched_docs:
        write_manifest(data_dir, docs)
    return result


def existing_converted_names(doc) -> set[str]:
    names: set[str] = set()
    for att in doc.attachments:
        if att.text_path:
            names.add(Path(att.text_path).name)
    return names


def attachment_is_current(data_dir: Path, att: Attachment, raw_path: Path) -> bool:
    if att.status != ATTACHMENT_CONVERTED or att.converter_version != CONVERTER_VERSION:
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
    if doc.converter_version != CONVERTER_VERSION:
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
    doc.converter_version = pseudo_attachment.converter_version or CONVERTER_VERSION
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
