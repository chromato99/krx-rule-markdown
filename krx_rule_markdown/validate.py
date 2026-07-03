from __future__ import annotations

from pathlib import Path
import json

from .markdown import load_documents


def validate_data(data_dir: Path) -> list[str]:
    errors: list[str] = []
    docs = load_documents(data_dir)
    errors.extend(validate_manifest_document_paths(data_dir, docs))
    seen_ids: set[tuple[str, str, str]] = set()
    for doc in docs:
        key = (doc.language, doc.document_type, doc.id)
        if key in seen_ids:
            errors.append(f"{doc.path}: duplicate document id {doc.id}")
        seen_ids.add(key)
        if not doc.id:
            errors.append(f"{doc.path}: id is required")
        if not doc.title:
            errors.append(f"{doc.path}: title is required")
        if doc.document_type not in {"rule", "notice"}:
            errors.append(f"{doc.path}: document_type must be rule or notice")
        if not doc.source_url:
            errors.append(f"{doc.path}: source_url is required")
        if not doc.collected_at:
            errors.append(f"{doc.path}: collected_at is required")
        if not doc.content_hash:
            errors.append(f"{doc.path}: content_hash is required")
        if doc.language not in {"ko", "en"}:
            errors.append(f"{doc.path}: language must be ko or en")
        if doc.raw_path and not (data_dir / doc.raw_path).exists():
            errors.append(f"{doc.path}: missing raw document file {doc.raw_path}")
        if doc.text_path and not (data_dir / doc.text_path).exists():
            errors.append(f"{doc.path}: missing converted document file {doc.text_path}")
        for att in doc.attachments:
            if att.raw_path and not (data_dir / att.raw_path).exists():
                errors.append(f"{doc.path}: missing raw attachment {att.raw_path}")
            if att.text_path and not (data_dir / att.text_path).exists():
                errors.append(f"{doc.path}: missing converted attachment {att.text_path}")
            if att.status == "converted" and not att.text_path:
                errors.append(f"{doc.path}: converted attachment {att.id} has no text_path")
            if att.status == "failed" and att.text_path:
                errors.append(f"{doc.path}: failed attachment {att.id} must not expose text_path")
    return errors


def validate_manifest_document_paths(data_dir: Path, docs: list) -> list[str]:
    manifest = data_dir / "manifest.json"
    if not manifest.exists():
        return []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{manifest}: invalid manifest: {exc}"]
    disk_paths: set[str] = set()
    for doc in docs:
        if not doc.path:
            continue
        try:
            disk_paths.add(str(Path(doc.path).relative_to(data_dir)))
        except ValueError:
            disk_paths.add(doc.path)
    manifest_paths: set[str] = set()
    for item in payload.get("documents", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if path:
            manifest_paths.add(path)
    errors: list[str] = []
    if len(manifest_paths) != len(disk_paths):
        errors.append(
            f"{manifest}: manifest document count {len(manifest_paths)} does not match disk document count {len(disk_paths)}"
        )
    for path in sorted(disk_paths - manifest_paths):
        errors.append(f"{manifest}: missing document path in manifest: {path}")
    for path in sorted(manifest_paths - disk_paths):
        errors.append(f"{manifest}: manifest references missing document path: {path}")
    return errors
