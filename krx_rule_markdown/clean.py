from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil

from .attachment_policy import is_excluded_current_rule_attachment, is_professional_attachment
from .markdown import load_documents, write_document
from .models import DOCUMENT_RULE
from .quality import write_manifest
from .repository import mutate_staged_corpus


@dataclass
class CleanResult:
    scanned: int
    removed: int


@dataclass
class DropResult:
    documents: int
    removed: int


def clean_unreferenced_attachments(data_dir: Path, *, dry_run: bool = False) -> CleanResult:
    if dry_run:
        return _clean_unreferenced_attachments(data_dir, dry_run=True)
    return mutate_staged_corpus(
        data_dir,
        "clean-attachments",
        lambda staging: _clean_unreferenced_attachments(staging, dry_run=False),
    )


def _clean_unreferenced_attachments(data_dir: Path, *, dry_run: bool = False) -> CleanResult:
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


def clean_unreferenced_documents(data_dir: Path, *, dry_run: bool = False) -> CleanResult:
    if dry_run:
        return _clean_unreferenced_documents(data_dir, dry_run=True)
    return mutate_staged_corpus(
        data_dir,
        "clean-documents",
        lambda staging: _clean_unreferenced_documents(staging, dry_run=False),
    )


def _clean_unreferenced_documents(data_dir: Path, *, dry_run: bool = False) -> CleanResult:
    data_dir = Path(data_dir)
    referenced = manifest_document_paths(data_dir)
    duplicate_paths = duplicate_document_paths(data_dir)
    paths = document_index_paths(data_dir)
    scanned = len(paths)
    ensure_manifest_not_truncated(data_dir, scanned, referenced)
    removed = 0
    for path in paths:
        rel = normalize_relative(str(path.relative_to(data_dir)))
        should_remove = rel in duplicate_paths
        if referenced:
            should_remove = should_remove or rel not in referenced
        if not should_remove:
            continue
        removed += 1
        if not dry_run:
            remove_document_bundle(path, data_dir)
    return CleanResult(scanned=scanned, removed=removed)


def ensure_manifest_not_truncated(data_dir: Path, scanned: int, referenced: set[str]) -> None:
    if scanned == 0 or not referenced:
        return
    if len(referenced) >= scanned:
        return
    if len(referenced) < max(1, scanned // 2):
        raise ValueError(
            "manifest references far fewer documents than exist on disk "
            f"({len(referenced)} manifest path(s), {scanned} document(s)); "
            "refusing to prune unreferenced documents"
        )


def drop_professional_attachments(data_dir: Path, *, dry_run: bool = False) -> DropResult:
    if dry_run:
        return _drop_professional_attachments(data_dir, dry_run=True)
    return mutate_staged_corpus(
        data_dir,
        "clean-professional",
        lambda staging: _drop_professional_attachments(staging, dry_run=False),
    )


def _drop_professional_attachments(data_dir: Path, *, dry_run: bool = False) -> DropResult:
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
    if dry_run:
        return _drop_past_rule_attachments(data_dir, dry_run=True)
    return mutate_staged_corpus(
        data_dir,
        "clean-past-rule",
        lambda staging: _drop_past_rule_attachments(staging, dry_run=False),
    )


def _drop_past_rule_attachments(data_dir: Path, *, dry_run: bool = False) -> DropResult:
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
        if doc.source_content_path:
            paths.add(normalize_relative(doc.source_content_path))
        if doc.source_request_path:
            paths.add(normalize_relative(doc.source_request_path))
        if doc.raw_path:
            paths.add(normalize_relative(doc.raw_path))
        if doc.text_path:
            paths.add(normalize_relative(doc.text_path))
        for att in doc.attachments:
            if att.raw_path:
                paths.add(normalize_relative(att.raw_path))
            if att.text_path:
                paths.add(normalize_relative(att.text_path))
            for asset in att.assets:
                if asset.path:
                    paths.add(normalize_relative(asset.path))
        for asset in doc.assets:
            if asset.path:
                paths.add(normalize_relative(asset.path))
    return paths


def attachment_roots(data_dir: Path) -> list[Path]:
    roots: list[Path] = []
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
                roots.append(bundle / "assets")
    return roots


def document_index_paths(data_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for language in ("ko", "en"):
        for folder in ("rules", "notices"):
            base = data_dir / language / folder
            if not base.exists():
                continue
            paths.extend(sorted(base.glob("*/index.md")))
    return paths


def manifest_document_paths(data_dir: Path) -> set[str]:
    manifest = data_dir / "manifest.json"
    if not manifest.exists():
        return set()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    paths: set[str] = set()
    for item in payload.get("documents", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if path:
            paths.add(normalize_relative(path))
    return paths


def duplicate_document_paths(data_dir: Path) -> set[str]:
    groups: dict[tuple[str, str, str], list] = {}
    for doc in load_documents(data_dir):
        if not doc.path:
            continue
        key = (doc.language, doc.document_type, doc.id)
        groups.setdefault(key, []).append(doc)
    duplicates: set[str] = set()
    for docs in groups.values():
        if len(docs) <= 1:
            continue
        kept = max(docs, key=lambda doc: (doc.collected_at, doc.path))
        for doc in docs:
            if doc.path == kept.path:
                continue
            try:
                duplicates.add(normalize_relative(str(Path(doc.path).relative_to(data_dir))))
            except ValueError:
                duplicates.add(normalize_relative(doc.path))
    return duplicates


def remove_document_bundle(index_path: Path, data_dir: Path) -> None:
    if index_path.name == "index.md" and index_path.parent.parent in document_container_roots(data_dir):
        shutil.rmtree(index_path.parent)
        return
    index_path.unlink()


def document_container_roots(data_dir: Path) -> set[Path]:
    roots: set[Path] = set()
    for language in ("ko", "en"):
        for folder in ("rules", "notices"):
            roots.add(data_dir / language / folder)
    return roots


def normalize_relative(path: str) -> str:
    return str(Path(path))
