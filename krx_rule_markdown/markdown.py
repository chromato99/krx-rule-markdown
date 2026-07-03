from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .models import LANGUAGE_EN, LANGUAGE_KO, Document, normalize_language, safe_file_name, slug


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
    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}
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
    if isinstance(value, int):
        return str(value)
    if value is None:
        return '""'
    text = str(value)
    if text == "":
        return '""'
    return json.dumps(text, ensure_ascii=False)


def write_document(root: Path, doc: Document) -> Path:
    folder = "notices" if doc.document_type == "notice" else "rules"
    path = existing_document_path(root, doc) or document_bundle_dir(root, doc) / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path = language_root(root, doc.language) / folder / safe_file_name(doc.title)
    if legacy_path.exists():
        legacy_path.unlink()
    path.write_text(render_markdown(doc), encoding="utf-8")
    return path


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
            doc = parse_markdown(path.read_text(encoding="utf-8"))
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
        existing = parse_markdown(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return existing.id == doc.id and existing.document_type == doc.document_type


def language_root(root: Path, language: str) -> Path:
    return root / normalize_language(language)


def document_paths(base: Path) -> list[Path]:
    return sorted(base.glob("*/index.md"))


def document_roots(root: Path) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    for language in (LANGUAGE_KO, LANGUAGE_EN):
        for folder in ("rules", "notices"):
            roots.append((language, root / language / folder))
    return roots
