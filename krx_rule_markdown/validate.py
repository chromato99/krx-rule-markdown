from __future__ import annotations

from pathlib import Path

from .markdown import load_documents


def validate_data(data_dir: Path) -> list[str]:
    errors: list[str] = []
    for doc in load_documents(data_dir):
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
