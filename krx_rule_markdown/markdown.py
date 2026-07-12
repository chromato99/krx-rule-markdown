from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .models import LANGUAGE_EN, LANGUAGE_KO, ATTACHMENT_CONVERTED, Document, hash_bytes, hash_text, normalize_language, safe_file_name, slug
from .contracts import (
    MAX_CONVERTED_TEXT_BYTES,
    MAX_METADATA_FILE_BYTES,
    MAX_SOURCE_BYTES,
    read_utf8_file_bounded,
    sha256_file,
)
from .repository import atomic_write_text


def parse_markdown(data: str) -> Document:
    if not data.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    end = data.find("\n---", 4)
    if end < 0:
        raise ValueError("missing YAML frontmatter terminator")
    frontmatter = data[4:end]
    body = data[end + len("\n---") :].strip()
    mapping = parse_frontmatter(frontmatter)
    doc = Document.from_mapping(mapping, body)
    if not doc.id:
        raise ValueError("id is required")
    if not doc.title:
        raise ValueError("title is required")
    if not doc.document_type:
        raise ValueError("document_type is required")
    return doc


def parse_frontmatter(text: str) -> dict[str, Any]:
    return parse_frontmatter_legacy(text)


def parse_frontmatter_legacy(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    attachments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_attachments = False
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.strip() == "attachments:":
            in_attachments = True
            out["attachments"] = attachments
            continue
        if in_attachments:
            if raw_line.startswith("  - "):
                current = {}
                attachments.append(current)
                key, value = split_key_value(raw_line[4:])
                if key:
                    current[key] = parse_scalar(value)
                continue
            if raw_line.startswith("    ") and current is not None:
                key, value = split_key_value(raw_line[4:])
                if key:
                    current[key] = parse_scalar(value)
                continue
            in_attachments = False
        key, value = split_key_value(raw_line)
        if key:
            out[key] = parse_scalar(value)
    return out


def split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        return "", ""
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value.startswith(("[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        if value.startswith('"'):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value[1:-1]
        return value[1:-1].replace("''", "'")
    if value.isdigit():
        return int(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none"}:
        return None
    return value


def render_markdown(doc: Document) -> str:
    if not doc.collected_at:
        raise ValueError("collected_at is required")
    lines = ["---"]
    for key, value in doc.to_mapping().items():
        if key == "attachments":
            lines.append("attachments:")
            for att in value:
                first = True
                for att_key, att_value in att.items():
                    prefix = "  - " if first else "    "
                    lines.append(f"{prefix}{att_key}: {format_scalar(att_value)}")
                    first = False
            continue
        lines.append(f"{key}: {format_scalar(value)}")
    lines.extend(["---", "", doc.body.strip(), ""])
    return "\n".join(lines)


def format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return '""'
    text = str(value)
    if text == "":
        return '""'
    return json.dumps(text, ensure_ascii=False)


def write_document(root: Path, doc: Document) -> Path:
    hydrate_file_metadata(Path(root), doc)
    doc.body_hash = hash_text(doc.body)
    doc.content_hash = hash_text(doc.title + "\n" + doc.body)
    folder = "notices" if doc.document_type == "notice" else "rules"
    path = existing_document_path(root, doc) or document_bundle_dir(root, doc) / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path = language_root(root, doc.language) / folder / safe_file_name(doc.title)
    if legacy_path.exists():
        legacy_path.unlink()
    atomic_write_text(path, render_markdown(doc))
    doc.path = str(path)
    return path


def hydrate_file_metadata(root: Path, doc: Document) -> None:
    hydrate_asset_file_metadata(root, doc.assets)
    if doc.raw_path:
        raw_path = root / doc.raw_path
        if raw_path.is_file():
            doc.raw_file_hash = sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES)
            doc.file_content_hash = doc.raw_file_hash
            doc.preservation_status = "preserved"
    for att in doc.attachments:
        hydrate_asset_file_metadata(root, att.assets)
        if att.raw_path:
            raw_path = root / att.raw_path
            if raw_path.is_file():
                att.raw_file_hash = sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES)
                att.content_hash = att.raw_file_hash
                att.size = raw_path.stat().st_size
                att.preservation_status = "preserved"
        if att.status == ATTACHMENT_CONVERTED and att.text_path:
            text_path = root / att.text_path
            if text_path.is_file():
                att.converted_text_hash = hash_text(
                    read_utf8_file_bounded(text_path, max_bytes=MAX_CONVERTED_TEXT_BYTES)
                )
                if att.searchable is None:
                    att.searchable = True


def hydrate_asset_file_metadata(root: Path, assets) -> None:
    from .assets import MAX_ASSET_BYTES, inspect_image

    for asset in assets:
        if asset.preservation_status != "preserved" or not asset.path:
            continue
        path = root / asset.path
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError(f"asset exceeds {MAX_ASSET_BYTES} bytes: {path}")
        data = path.read_bytes()
        image = inspect_image(data)
        asset.raw_file_hash = hash_bytes(data)
        asset.size = len(data)
        asset.mime_type = image.mime_type
        asset.width = image.width
        asset.height = image.height
        asset.searchable = False


def existing_document_path(root: Path, doc: Document) -> Path | None:
    if not doc.path:
        return None
    root = Path(root)
    path = Path(doc.path)
    if not path.is_absolute():
        path = path if path.exists() else root / path
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if path.name != "index.md":
        return None
    return path


def load_documents(root: Path) -> list[Document]:
    docs: list[Document] = []
    seen: set[Path] = set()
    for language, folder in document_roots(root):
        base = folder
        if not base.exists():
            continue
        for path in document_paths(base):
            if path in seen:
                continue
            seen.add(path)
            doc = parse_markdown(read_utf8_file_bounded(path, max_bytes=MAX_METADATA_FILE_BYTES))
            doc.directory_language = language
            if not doc.language:
                doc.language = language
            else:
                doc.language = normalize_language(doc.language)
            doc.path = str(path)
            docs.append(doc)
    return docs


def document_bundle_dir(root: Path, doc: Document) -> Path:
    folder = "notices" if doc.document_type == "notice" else "rules"
    parent = language_root(root, doc.language) / folder
    base = parent / slug(doc.title)
    if bundle_can_hold_document(base, doc):
        return base
    suffixed = parent / f"{slug(doc.title)}-{slug(doc.id) or 'document'}"
    if not bundle_can_hold_document(suffixed, doc):
        raise ValueError(f"document bundle collision for {doc.id}: {suffixed}")
    return suffixed


def bundle_can_hold_document(path: Path, doc: Document) -> bool:
    index = path / "index.md"
    if not index.exists():
        return True
    try:
        existing = parse_markdown(
            read_utf8_file_bounded(index, max_bytes=MAX_METADATA_FILE_BYTES)
        )
    except (OSError, ValueError):
        return False
    return existing.id == doc.id and existing.document_type == doc.document_type


def language_root(root: Path, language: str) -> Path:
    normalized = normalize_language(language)
    if normalized not in {LANGUAGE_KO, LANGUAGE_EN}:
        raise ValueError(f"language must be ko or en, got {language!r}")
    return root / normalized


def document_paths(base: Path) -> list[Path]:
    return sorted(base.glob("*/index.md"))


def document_roots(root: Path) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    for language in (LANGUAGE_KO, LANGUAGE_EN):
        for folder in ("rules", "notices"):
            roots.append((language, root / language / folder))
    return roots
