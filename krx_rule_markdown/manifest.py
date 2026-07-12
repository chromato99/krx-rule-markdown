from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
import json

from .contracts import (
    CORPUS_SCHEMA_VERSION,
    MAX_CONVERTED_TEXT_BYTES,
    MAX_SOURCE_BYTES,
    canonical_text_hash,
    index_source_hash,
    release_hash,
    sha256_file,
)
from .models import Document, now_utc
from .repository import atomic_write_json


def build_manifest(
    data_dir: Path,
    docs: Iterable[Document],
    *,
    source: str = "",
    version: str = "0.1.0",
    release_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(docs, key=lambda doc: (doc.language, doc.document_type, doc.title, doc.id))
    hydrate_contract_hashes(Path(data_dir), ordered)
    payload: dict[str, Any] = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "version": version,
        "generated_at": now_utc(),
        "source": source,
        "documents": [doc.to_mapping() | {"path": relative_doc_path(data_dir, doc)} for doc in ordered],
        "attachment_log": [att.to_mapping() for doc in ordered for att in doc.attachments],
        "index_source_hash": index_source_hash(ordered),
    }
    payload["release_profile"] = release_profile or {
        "version": 1,
        "default": "strict",
        "allowed_failure_ids": [],
    }
    payload["release_hash"] = release_hash(payload)
    return payload


def hydrate_contract_hashes(data_dir: Path, docs: Iterable[Document]) -> None:
    for doc in docs:
        doc.body_hash = canonical_text_hash(doc.body)
        if doc.raw_path:
            raw_path = data_dir / doc.raw_path
            if raw_path.exists():
                doc.raw_file_hash = sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES)
                if not doc.file_content_hash:
                    doc.file_content_hash = doc.raw_file_hash
        for att in doc.attachments:
            if att.raw_path:
                raw_path = data_dir / att.raw_path
                if raw_path.exists():
                    att.raw_file_hash = sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES)
                    if not att.content_hash:
                        att.content_hash = att.raw_file_hash
            if att.text_path:
                text_path = data_dir / att.text_path
                if text_path.exists():
                    if text_path.stat().st_size > MAX_CONVERTED_TEXT_BYTES:
                        raise ValueError(f"converted text exceeds {MAX_CONVERTED_TEXT_BYTES} bytes: {text_path}")
                    att.converted_text_hash = canonical_text_hash(
                        text_path.read_text(encoding="utf-8", errors="strict")
                    )


def write_manifest_atomic(
    data_dir: Path,
    docs: Iterable[Document],
    *,
    source: str = "",
    version: str = "0.1.0",
    release_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_manifest(
        data_dir,
        docs,
        source=source,
        version=version,
        release_profile=release_profile,
    )
    path = Path(data_dir) / "manifest.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if (
        existing.get("release_hash") == payload.get("release_hash")
        and existing.get("release_hash") == release_hash(existing)
    ):
        return existing
    atomic_write_json(path, payload)
    return payload


def relative_doc_path(data_dir: Path, doc: Document) -> str:
    if not doc.path:
        return ""
    path = Path(doc.path)
    try:
        return path.relative_to(data_dir).as_posix()
    except ValueError:
        return str(path)


def manifest_allowed_failure_ids(data_dir: Path) -> set[str] | None:
    path = Path(data_dir) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    profile = payload.get("release_profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("allowed_failure_ids"), list):
        return None
    values = profile["allowed_failure_ids"]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return set(values)
