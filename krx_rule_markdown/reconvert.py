from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .convert import convert_attachment
from .markdown import load_documents, write_document
from .models import ATTACHMENT_CONVERTED
from .paths import converted_attachment_path
from .quality import write_manifest


@dataclass
class ReconvertResult:
    documents: int = 0
    attachments: int = 0
    converted: int = 0
    failed: int = 0
    skipped: int = 0


def reconvert_data(data_dir: Path, *, document_id: str = "", dry_run: bool = False) -> ReconvertResult:
    docs = load_documents(data_dir)
    result = ReconvertResult()
    touched_docs = []
    for doc in docs:
        if document_id and doc.id != document_id and doc.source_id != document_id:
            continue
        result.documents += 1
        changed = False
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
            text_path = data_dir / att.text_path if att.text_path else converted_attachment_path(data_dir, doc, att, used_converted_names)
            att = convert_attachment(raw_path, text_path, att)
            att.raw_path = str(raw_path.relative_to(data_dir))
            if att.status == ATTACHMENT_CONVERTED and att.text_path:
                att.text_path = str(text_path.relative_to(data_dir))
                result.converted += 1
            else:
                result.failed += 1
            changed = True
        if changed:
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
