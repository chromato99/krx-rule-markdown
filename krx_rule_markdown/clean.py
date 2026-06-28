from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .attachment_policy import is_excluded_current_rule_attachment, is_professional_attachment
from .markdown import load_documents, write_document
from .models import DOCUMENT_RULE
from .quality import write_manifest


@dataclass
class CleanResult:
    scanned: int
    removed: int


@dataclass
class DropResult:
    documents: int
    removed: int


def clean_unreferenced_attachments(data_dir: Path, *, dry_run: bool = False) -> CleanResult:
    data_dir = Path(data_dir)
    referenced = referenced_attachment_paths(data_dir)
    scanned = 0
    removed = 0
    for base in attachment_roots(data_dir):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*"), reverse=True):
            if path.is_dir():
                if not dry_run and not any(path.iterdir()):
                    path.rmdir()
                continue
            scanned += 1
            try:
                rel = str(path.relative_to(data_dir))
            except ValueError:
                continue
            if rel in referenced:
                continue
            removed += 1
            if not dry_run:
                path.unlink()
    return CleanResult(scanned=scanned, removed=removed)


def drop_professional_attachments(data_dir: Path, *, dry_run: bool = False) -> DropResult:
    data_dir = Path(data_dir)
    docs = load_documents(data_dir)
    removed = 0
    changed = 0
    for doc in docs:
        kept = []
        for att in doc.attachments:
            if is_professional_attachment(att.title, att.file_name, att.server_file, att.id):
                removed += 1
                continue
            kept.append(att)
        if len(kept) != len(doc.attachments):
            changed += 1
            doc.attachments = kept
            if not dry_run:
                write_document(data_dir, doc)
    if changed and not dry_run:
        write_manifest(data_dir, docs)
    return DropResult(documents=changed, removed=removed)


def drop_past_rule_attachments(data_dir: Path, *, dry_run: bool = False) -> DropResult:
    data_dir = Path(data_dir)
    docs = load_documents(data_dir)
    removed = 0
    changed = 0
    for doc in docs:
        if doc.document_type != DOCUMENT_RULE:
            continue
        kept = []
        for att in doc.attachments:
            if is_excluded_current_rule_attachment(att.title, att.file_name, att.server_file, att.id):
                removed += 1
                continue
            kept.append(att)
        if len(kept) != len(doc.attachments):
            changed += 1
            doc.attachments = kept
            if not dry_run:
                write_document(data_dir, doc)
    if changed and not dry_run:
        write_manifest(data_dir, docs)
    return DropResult(documents=changed, removed=removed)


def referenced_attachment_paths(data_dir: Path) -> set[str]:
    paths: set[str] = set()
    for doc in load_documents(data_dir):
        if doc.raw_path:
            paths.add(normalize_relative(doc.raw_path))
        if doc.text_path:
            paths.add(normalize_relative(doc.text_path))
        for att in doc.attachments:
            if att.raw_path:
                paths.add(normalize_relative(att.raw_path))
            if att.text_path:
                paths.add(normalize_relative(att.text_path))
    return paths


def attachment_roots(data_dir: Path) -> list[Path]:
    roots = [data_dir / "attachments", data_dir / "ko" / "attachments", data_dir / "en" / "attachments"]
    for language in ("ko", "en"):
        for folder in ("rules", "notices"):
            base = data_dir / language / folder
            if not base.exists():
                continue
            for bundle in base.iterdir():
                if not bundle.is_dir():
                    continue
                roots.append(bundle / "raw")
                roots.append(bundle / "attachments")
    return roots


def normalize_relative(path: str) -> str:
    return str(Path(path))
