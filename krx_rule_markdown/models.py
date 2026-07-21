from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any

from .contracts import (
    CORPUS_SCHEMA_VERSION,
    canonical_text_hash,
    effective_searchable,
    parse_quality_codes,
    sha256_bytes,
)


DOCUMENT_RULE = "rule"
DOCUMENT_NOTICE = "notice"

LANGUAGE_KO = "ko"
LANGUAGE_EN = "en"

ATTACHMENT_PENDING = "pending"
ATTACHMENT_CONVERTED = "converted"
ATTACHMENT_FAILED = "failed"


@dataclass
class Asset:
    id: str
    source_kind: str = ""
    source_anchor: str = ""
    source_url: str = ""
    path: str = ""
    mime_type: str = ""
    raw_file_hash: str = ""
    size: int = 0
    width: int = 0
    height: int = 0
    preservation_status: str = ""
    searchable: bool | None = False
    quality_codes: list[str] = field(default_factory=list)
    error: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Asset":
        return cls(
            id=str(data.get("id", "")),
            source_kind=str(data.get("source_kind", "")),
            source_anchor=str(data.get("source_anchor", "")),
            source_url=str(data.get("source_url", "")),
            path=str(data.get("path", "")),
            mime_type=str(data.get("mime_type", "")),
            raw_file_hash=str(data.get("raw_file_hash", "")),
            size=int(data.get("size") or 0),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            preservation_status=str(data.get("preservation_status", "")),
            searchable=parse_optional_bool(data.get("searchable")),
            quality_codes=parse_quality_codes(data.get("quality_codes")),
            error=str(data.get("error", "")),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "source_kind": self.source_kind,
            "source_anchor": self.source_anchor,
            "path": self.path,
            "mime_type": self.mime_type,
            "raw_file_hash": self.raw_file_hash,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "preservation_status": self.preservation_status,
            "searchable": False,
        }
        for key in ("source_url", "error"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.quality_codes:
            out["quality_codes"] = parse_quality_codes(self.quality_codes)
        return out


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
    raw_file_hash: str = ""
    converted_text_hash: str = ""
    converter_version: str = ""
    asset_inspection_version: str = ""
    status: str = ATTACHMENT_PENDING
    preservation_status: str = ""
    searchable: bool | None = None
    error: str = ""
    last_refresh_error: str = field(default="", repr=False)
    last_refresh_failed_at: str = field(default="", repr=False)
    size: int = 0
    quality_status: str = ""
    quality_score: int = 0
    quality_flags: str = ""
    quality_codes: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    converted_text_chars: int = 0
    converted_non_space_chars: int = 0
    table_row_count: int = 0
    formula_block_count: int = 0
    formula_hint_count: int = 0
    replacement_char_count: int = 0

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Attachment":
        content_hash = str(data.get("content_hash") or data.get("raw_file_hash") or "")
        quality_codes = parse_quality_codes(data.get("quality_codes") or data.get("quality_flags"))
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
            content_hash=content_hash,
            raw_file_hash=str(data.get("raw_file_hash") or content_hash),
            converted_text_hash=str(data.get("converted_text_hash", "")),
            converter_version=str(data.get("converter_version", "")),
            asset_inspection_version=str(data.get("asset_inspection_version", "")),
            status=str(data.get("conversion_status") or data.get("status") or ATTACHMENT_PENDING),
            preservation_status=str(data.get("preservation_status", "")),
            searchable=parse_optional_bool(data.get("searchable")),
            error=str(data.get("error", "")),
            last_refresh_error=str(data.get("last_refresh_error", "")),
            last_refresh_failed_at=str(data.get("last_refresh_failed_at", "")),
            size=int(data.get("size") or 0),
            quality_status=str(data.get("quality_status", "")),
            quality_score=int(data.get("quality_score") or 0),
            quality_flags=str(data.get("quality_flags") or ",".join(quality_codes)),
            quality_codes=quality_codes,
            diagnostics=[
                {str(key): str(value) for key, value in item.items()}
                for item in data.get("diagnostics", [])
                if isinstance(item, dict)
            ],
            assets=[Asset.from_mapping(item) for item in data.get("assets", []) if isinstance(item, dict)],
            converted_text_chars=int(data.get("converted_text_chars") or 0),
            converted_non_space_chars=int(data.get("converted_non_space_chars") or 0),
            table_row_count=int(data.get("table_row_count") or 0),
            formula_block_count=int(data.get("formula_block_count") or 0),
            formula_hint_count=int(data.get("formula_hint_count") or 0),
            replacement_char_count=int(data.get("replacement_char_count") or 0),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "file_name": self.file_name,
            "status": self.status,
            "conversion_status": self.status,
        }
        raw_file_hash = self.raw_file_hash or self.content_hash
        quality_codes = parse_quality_codes(self.quality_codes or self.quality_flags)
        optional = {
            "mime_type": self.mime_type,
            "source_url": self.source_url,
            "server_file": self.server_file,
            "folder": self.folder,
            "raw_path": self.raw_path,
            "text_path": self.text_path,
            "content_hash": self.content_hash,
            "raw_file_hash": raw_file_hash,
            "converted_text_hash": self.converted_text_hash,
            "converter_version": self.converter_version,
            "asset_inspection_version": self.asset_inspection_version,
            "preservation_status": self.preservation_status,
            "searchable": self.searchable,
            "error": self.error,
            "size": self.size,
            "quality_status": self.quality_status,
            "quality_score": self.quality_score,
            "quality_flags": self.quality_flags,
            "quality_codes": quality_codes,
            "diagnostics": self.diagnostics,
            "assets": [asset.to_mapping() for asset in self.assets],
            "converted_text_chars": self.converted_text_chars,
            "converted_non_space_chars": self.converted_non_space_chars,
            "table_row_count": self.table_row_count,
            "formula_block_count": self.formula_block_count,
            "formula_hint_count": self.formula_hint_count,
            "replacement_char_count": self.replacement_char_count,
        }
        for key, value in optional.items():
            if key == "searchable":
                if value is not None:
                    out[key] = bool(value)
                continue
            if value not in ("", 0, None, []):
                out[key] = value
        return out

    @property
    def conversion_status(self) -> str:
        return self.status

    @conversion_status.setter
    def conversion_status(self, value: str) -> None:
        self.status = value


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
    body_hash: str = ""
    source_response_hash: str = ""
    source_content_hash: str = ""
    source_content_path: str = ""
    source_request_path: str = ""
    converter_version: str = ""
    asset_inspection_version: str = ""
    conversion_status: str = "converted"
    preservation_status: str = ""
    searchable: bool | None = None
    quality_status: str = ""
    quality_codes: list[str] = field(default_factory=list)
    schema_version: int = CORPUS_SCHEMA_VERSION
    language: str = LANGUAGE_KO
    source_id: str = ""
    file_name: str = ""
    raw_path: str = ""
    text_path: str = ""
    file_content_hash: str = ""
    raw_file_hash: str = ""
    assets: list[Asset] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    path: str = ""
    source_content_html: str = field(default="", repr=False)
    source_request: dict[str, Any] = field(default_factory=dict, repr=False)
    declared_language: bool = field(default=True, repr=False)
    directory_language: str = field(default="", repr=False)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], body: str = "") -> "Document":
        raw_language = str(data.get("language") or "")
        quality_codes = parse_quality_codes(data.get("quality_codes"))
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
            body_hash=str(data.get("body_hash", "")),
            source_response_hash=str(data.get("source_response_hash", "")),
            source_content_hash=str(data.get("source_content_hash", "")),
            source_content_path=str(data.get("source_content_path", "")),
            source_request_path=str(data.get("source_request_path", "")),
            converter_version=str(data.get("converter_version", "")),
            asset_inspection_version=str(data.get("asset_inspection_version", "")),
            conversion_status=str(data.get("conversion_status") or "converted"),
            preservation_status=str(data.get("preservation_status", "")),
            searchable=parse_optional_bool(data.get("searchable")),
            quality_status=str(data.get("quality_status", "")),
            quality_codes=quality_codes,
            schema_version=int(data.get("schema_version") or 1),
            language=normalize_language(raw_language),
            declared_language="language" in data and bool(raw_language.strip()),
            source_id=str(data.get("source_id", "")),
            file_name=str(data.get("file_name", "")),
            raw_path=str(data.get("raw_path", "")),
            text_path=str(data.get("text_path", "")),
            file_content_hash=str(data.get("file_content_hash", "")),
            raw_file_hash=str(data.get("raw_file_hash") or data.get("file_content_hash") or ""),
            assets=[Asset.from_mapping(item) for item in data.get("assets", []) if isinstance(item, dict)],
            attachments=[Attachment.from_mapping(item) for item in data.get("attachments", [])],
            path=str(data.get("path", "")),
        )

    def to_mapping(self) -> dict[str, Any]:
        body_hash = self.body_hash or canonical_text_hash(self.body)
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "title": self.title,
            "source_url": self.source_url,
            "collected_at": self.collected_at,
            "content_hash": self.content_hash,
            "body_hash": body_hash,
            "document_type": self.document_type,
            "language": self.language,
            "conversion_status": self.conversion_status,
            "searchable": effective_searchable(self),
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
            "raw_file_hash",
            "source_content_hash",
            "source_content_path",
            "source_request_path",
            "converter_version",
            "asset_inspection_version",
            "preservation_status",
            "quality_status",
        ):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.quality_codes:
            out["quality_codes"] = parse_quality_codes(self.quality_codes)
        if self.assets:
            out["assets"] = [asset.to_mapping() for asset in self.assets]
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
    return canonical_text_hash(text)


def hash_bytes(data: bytes) -> str:
    return sha256_bytes(data)


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
    if value in {"ko", "kor", "korean", "ko-kr"}:
        return LANGUAGE_KO
    return value


def parse_optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None
