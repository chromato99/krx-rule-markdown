from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any


DOCUMENT_RULE = "rule"
DOCUMENT_NOTICE = "notice"

LANGUAGE_KO = "ko"
LANGUAGE_EN = "en"

ATTACHMENT_PENDING = "pending"
ATTACHMENT_CONVERTED = "converted"
ATTACHMENT_FAILED = "failed"


@dataclass
class Attachment:
    id: str
    title: str = ""
    file_name: str = ""
    mime_type: str = ""
    source_url: str = ""
    server_file: str = ""
    folder: str = ""
    raw_path: str = ""
    text_path: str = ""
    content_hash: str = ""
    status: str = ATTACHMENT_PENDING
    error: str = ""
    size: int = 0
    quality_status: str = ""
    quality_score: int = 0
    quality_flags: str = ""
    converted_text_chars: int = 0
    converted_non_space_chars: int = 0
    table_row_count: int = 0
    formula_hint_count: int = 0
    replacement_char_count: int = 0

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Attachment":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            file_name=str(data.get("file_name", "")),
            mime_type=str(data.get("mime_type", "")),
            source_url=str(data.get("source_url", "")),
            server_file=str(data.get("server_file", "")),
            folder=str(data.get("folder", "")),
            raw_path=str(data.get("raw_path", "")),
            text_path=str(data.get("text_path", "")),
            content_hash=str(data.get("content_hash", "")),
            status=str(data.get("status", ATTACHMENT_PENDING)),
            error=str(data.get("error", "")),
            size=int(data.get("size") or 0),
            quality_status=str(data.get("quality_status", "")),
            quality_score=int(data.get("quality_score") or 0),
            quality_flags=str(data.get("quality_flags", "")),
            converted_text_chars=int(data.get("converted_text_chars") or 0),
            converted_non_space_chars=int(data.get("converted_non_space_chars") or 0),
            table_row_count=int(data.get("table_row_count") or 0),
            formula_hint_count=int(data.get("formula_hint_count") or 0),
            replacement_char_count=int(data.get("replacement_char_count") or 0),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "file_name": self.file_name,
            "status": self.status,
        }
        optional = {
            "mime_type": self.mime_type,
            "source_url": self.source_url,
            "server_file": self.server_file,
            "folder": self.folder,
            "raw_path": self.raw_path,
            "text_path": self.text_path,
            "content_hash": self.content_hash,
            "error": self.error,
            "size": self.size,
            "quality_status": self.quality_status,
            "quality_score": self.quality_score,
            "quality_flags": self.quality_flags,
            "converted_text_chars": self.converted_text_chars,
            "converted_non_space_chars": self.converted_non_space_chars,
            "table_row_count": self.table_row_count,
            "formula_hint_count": self.formula_hint_count,
            "replacement_char_count": self.replacement_char_count,
        }
        for key, value in optional.items():
            if value not in ("", 0, None):
                out[key] = value
        return out


@dataclass
class Document:
    id: str
    title: str
    source_url: str
    document_type: str
    body: str = ""
    category: str = ""
    effective_date: str = ""
    published_date: str = ""
    collected_at: str = ""
    content_hash: str = ""
    language: str = LANGUAGE_KO
    source_id: str = ""
    file_name: str = ""
    raw_path: str = ""
    text_path: str = ""
    file_content_hash: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    path: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any], body: str = "") -> "Document":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            source_url=str(data.get("source_url", "")),
            document_type=str(data.get("document_type", "")),
            body=body,
            category=str(data.get("category", "")),
            effective_date=str(data.get("effective_date", "")),
            published_date=str(data.get("published_date", "")),
            collected_at=str(data.get("collected_at", "")),
            content_hash=str(data.get("content_hash", "")),
            language=normalize_language(str(data.get("language", LANGUAGE_KO))),
            source_id=str(data.get("source_id", "")),
            file_name=str(data.get("file_name", "")),
            raw_path=str(data.get("raw_path", "")),
            text_path=str(data.get("text_path", "")),
            file_content_hash=str(data.get("file_content_hash", "")),
            attachments=[Attachment.from_mapping(item) for item in data.get("attachments", [])],
            path=str(data.get("path", "")),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "source_url": self.source_url,
            "collected_at": self.collected_at,
            "content_hash": self.content_hash,
            "document_type": self.document_type,
            "language": normalize_language(self.language),
        }
        for key in (
            "category",
            "effective_date",
            "published_date",
            "source_id",
            "file_name",
            "raw_path",
            "text_path",
            "file_content_hash",
        ):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.attachments:
            out["attachments"] = [att.to_mapping() for att in self.attachments]
        return out


@dataclass
class Item:
    id: str
    title: str
    document_type: str
    category: str = ""
    book_id: str = ""
    noformyn: str = "N"
    menu_id: str = ""
    published_date: str = ""
    effective_date: str = ""
    state_history_id: str = ""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hash_text(text: str) -> str:
    return sha256(text.strip().encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def slug(text: str) -> str:
    text = text.strip().lower()
    for old in ("/", "\\", " ", "_", ".", ":"):
        text = text.replace(old, "-")
    text = re.sub(r"[^0-9a-z가-힣-]+", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "untitled"


def safe_file_name(title: str) -> str:
    return f"{slug(title)}.md"


def first_non_empty(*values: str) -> str:
    for value in values:
        value = (value or "").strip()
        if value:
            return value
    return ""


def normalize_language(value: str) -> str:
    value = (value or "").strip().lower().replace("_", "-")
    if value in {"en", "eng", "english", "en-us", "en-gb"}:
        return LANGUAGE_EN
    return LANGUAGE_KO
