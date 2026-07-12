from __future__ import annotations

from pathlib import Path
import re

from .models import Attachment, Document, slug
from .markdown import document_bundle_dir


MAX_CONVERTED_NAME_BYTES = 160
MAX_RAW_NAME_BYTES = 180


def converted_attachment_path(
    data_dir: Path,
    doc: Document,
    att: Attachment,
    used: set[str] | None = None,
) -> Path:
    stem = converted_attachment_stem(att)
    name = truncate_markdown_name(f"{slug(stem)}.md")
    base = document_bundle_dir(data_dir, doc)
    if used is None:
        return base / "attachments" / name
    return base / "attachments" / unique_name(name, used, max_bytes=MAX_CONVERTED_NAME_BYTES)


def raw_attachment_path(
    data_dir: Path,
    doc: Document,
    att: Attachment,
    used: set[str] | None = None,
) -> Path:
    suffix = attachment_suffix(att)
    stem = converted_attachment_stem(att)
    name = truncate_name(f"{slug(stem)}{suffix}", MAX_RAW_NAME_BYTES)
    base = document_bundle_dir(data_dir, doc)
    if used is None:
        return base / "raw" / name
    return base / "raw" / unique_name(name, used, max_bytes=MAX_RAW_NAME_BYTES)


def converted_attachment_stem(att: Attachment) -> str:
    title = clean_title(att.title)
    file_stem = clean_title(Path(att.file_name).stem)
    if title and not is_generic_server_name(title):
        return title
    if file_stem and not is_generic_server_name(file_stem):
        return file_stem
    return att.id or "attachment"


def clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value.strip(" ._-")


def is_generic_server_name(value: str) -> bool:
    compact = re.sub(r"[^0-9a-zA-Z가-힣]", "", value or "")
    if not compact:
        return True
    if compact.lower() in {"download", "attachment", "file"}:
        return True
    return bool(re.fullmatch(r"\d{8,}[a-zA-Z0-9]*", compact))


def truncate_markdown_name(name: str, max_bytes: int = MAX_CONVERTED_NAME_BYTES) -> str:
    return truncate_name(name, max_bytes)


def truncate_name(name: str, max_bytes: int) -> str:
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    shortened = truncate_stem(stem, budget)
    return shortened + suffix


def truncate_stem(stem: str, max_bytes: int) -> str:
    out: list[str] = []
    used = 0
    for ch in stem:
        ch_len = len(ch.encode("utf-8"))
        if used + ch_len > max_bytes:
            break
        out.append(ch)
        used += ch_len
    shortened = "".join(out).rstrip(" ._-")
    return shortened or truncate_ascii_fallback("attachment", max_bytes)


def truncate_ascii_fallback(value: str, max_bytes: int) -> str:
    return value.encode("ascii")[:max(1, max_bytes)].decode("ascii")


def unique_name(name: str, used: set[str], *, max_bytes: int) -> str:
    candidate = truncate_name(name, max_bytes)
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while candidate in used:
        disambiguator = f"-{index}"
        stem_budget = max_bytes - len(suffix.encode("utf-8")) - len(disambiguator)
        if stem_budget < 1:
            raise ValueError("max_bytes is too small for a unique filename suffix")
        candidate = f"{truncate_stem(stem, stem_budget)}{disambiguator}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def attachment_suffix(att: Attachment) -> str:
    suffix = Path(att.file_name).suffix or Path(att.server_file).suffix
    return suffix or ".bin"
