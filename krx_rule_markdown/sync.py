from __future__ import annotations

from pathlib import Path
import json
import sys

from .collector import Client, guess_mime_type
from .convert import convert_attachment
from .markdown import write_document
from .models import ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, LANGUAGE_EN, LANGUAGE_KO, Document, hash_text, now_utc
from .models import DOCUMENT_RULE, Item
from .paths import converted_attachment_path, raw_attachment_path
from .quality import mark_quality_failure


LANGUAGE_ALL = "all"
SYNC_LANGUAGE_CHOICES = (LANGUAGE_ALL, LANGUAGE_KO, LANGUAGE_EN)


def sync_rules(
    *,
    data_dir: Path,
    base_url: str,
    limit: int,
    recent_only: bool,
    rule_id: str,
    download_attachments: bool,
    language: str,
) -> int:
    language = normalize_sync_language(language)
    client = Client(base_url)
    client.bootstrap()
    if rule_id:
        items = [
            Item(
                id=rule_id,
                book_id=rule_id,
                title=rule_id,
                document_type=DOCUMENT_RULE,
                noformyn="N",
            )
        ]
    else:
        items = collect_items(client, limit, recent_only, language)
    if limit and len(items) > limit:
        items = items[:limit]
    manifest_docs: list[Document] = []
    attachment_log = []
    for idx, item in enumerate(dedupe_items(items), start=1):
        print(f"fetching {idx}/{len(items)} {item.document_type} {item.id} {item.title}", file=sys.stderr)
        try:
            doc = client.fetch_document(item)
        except Exception as exc:  # noqa: BLE001 - keep long syncs moving.
            print(f"warning: document fetch failed for {item.id}: {exc}", file=sys.stderr)
            continue
        doc.language = LANGUAGE_KO
        if includes_korean(language) and download_attachments:
            converted = []
            used_converted_names: set[str] = set()
            used_raw_names: set[str] = set()
            for att in doc.attachments:
                if not att.server_file:
                    converted.append(att)
                    continue
                try:
                    att, data = client.download_attachment(att)
                    raw_path = raw_attachment_path(data_dir, doc, att, used_raw_names)
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_bytes(data)
                    if not att.mime_type:
                        att.mime_type = guess_mime_type(raw_path)
                    text_path = converted_attachment_path(data_dir, doc, att, used_converted_names)
                    att = convert_attachment(raw_path, text_path, att)
                    att.raw_path = str(raw_path.relative_to(data_dir))
                    if att.text_path:
                        att.text_path = str(text_path.relative_to(data_dir))
                except Exception as exc:  # noqa: BLE001 - failure belongs in metadata.
                    att.status = ATTACHMENT_FAILED
                    att.error = str(exc)
                    att.text_path = ""
                    mark_quality_failure(att, "conversion_failed")
                converted.append(att)
            doc.attachments = converted
        if includes_korean(language):
            path = write_document(data_dir, doc)
            doc.path = str(path)
            manifest_docs.append(doc)
            attachment_log.extend(doc.attachments)
        if includes_english(language) and doc.document_type == DOCUMENT_RULE:
            english_doc, english_log = fetch_english_rule_document(data_dir, client, item, doc)
            if english_log is not None:
                attachment_log.append(english_log)
            if english_doc is not None:
                path = write_document(data_dir, english_doc)
                english_doc.path = str(path)
                manifest_docs.append(english_doc)
    write_manifest(data_dir, manifest_docs, attachment_log, base_url)
    return 0


def collect_items(client: Client, limit: int, recent_only: bool, language: str) -> list:
    if recent_only:
        items = client.recent_items()
    else:
        items = client.current_rule_items(limit)
        if includes_korean(language):
            items.extend(item for item in client.recent_items() if item.document_type == "notice")
    if language == LANGUAGE_EN:
        items = [item for item in items if item.document_type == DOCUMENT_RULE]
    return items


def dedupe_items(items: list) -> list:
    seen: set[str] = set()
    out = []
    for item in items:
        key = f"{item.document_type}:{item.id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def normalize_sync_language(language: str) -> str:
    value = (language or LANGUAGE_ALL).strip().lower()
    if value not in SYNC_LANGUAGE_CHOICES:
        raise ValueError(f"language must be one of {', '.join(SYNC_LANGUAGE_CHOICES)}")
    return value


def includes_korean(language: str) -> bool:
    language = normalize_sync_language(language)
    return language in {LANGUAGE_ALL, LANGUAGE_KO}


def includes_english(language: str) -> bool:
    language = normalize_sync_language(language)
    return language in {LANGUAGE_ALL, LANGUAGE_EN}


def write_manifest(data_dir: Path, docs: list[Document], attachment_log: list, source: str) -> None:
    payload = {
        "version": "0.1.0",
        "generated_at": now_utc(),
        "source": source,
        "documents": [doc.to_mapping() | {"path": str(Path(doc.path).relative_to(data_dir)) if doc.path else ""} for doc in docs],
        "attachment_log": [att.to_mapping() for att in attachment_log],
    }
    path = data_dir / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_english_rule_document(
    data_dir: Path,
    client: Client,
    item: Item,
    korean_doc: Document,
) -> tuple[Document | None, object | None]:
    try:
        att, data = client.download_rule_file(item, "ENG", "English full text")
    except FileNotFoundError:
        return None, None
    except Exception as exc:  # noqa: BLE001 - keep syncs moving.
        print(f"warning: English rule fetch failed for {korean_doc.id}: {exc}", file=sys.stderr)
        return None, None

    english_doc = Document(
        id=f"{korean_doc.id}-en",
        title=english_rule_title(att.file_name, korean_doc.title),
        category=korean_doc.category,
        source_url=korean_doc.source_url,
        effective_date=korean_doc.effective_date,
        published_date=korean_doc.published_date,
        collected_at=now_utc(),
        document_type=DOCUMENT_RULE,
        language=LANGUAGE_EN,
        source_id=korean_doc.id,
        file_name=att.file_name,
    )
    used_raw_names: set[str] = set()
    used_converted_names: set[str] = set()
    try:
        raw_path = raw_attachment_path(data_dir, english_doc, att, used_raw_names)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(data)
        if not att.mime_type:
            att.mime_type = guess_mime_type(raw_path)
        text_path = converted_attachment_path(data_dir, english_doc, att, used_converted_names)
        att = convert_attachment(raw_path, text_path, att)
        att.raw_path = str(raw_path.relative_to(data_dir))
        if att.text_path:
            att.text_path = str(text_path.relative_to(data_dir))
    except Exception as exc:  # noqa: BLE001 - failure belongs in metadata.
        att.status = ATTACHMENT_FAILED
        att.error = str(exc)
        att.text_path = ""
        mark_quality_failure(att, "conversion_failed")

    if att.status != ATTACHMENT_CONVERTED or not att.text_path:
        return None, att

    body = (data_dir / att.text_path).read_text(encoding="utf-8").strip()
    english_doc.body = body
    english_doc.raw_path = att.raw_path
    english_doc.text_path = att.text_path
    english_doc.file_content_hash = att.content_hash
    english_doc.content_hash = hash_text(english_doc.title + "\n" + english_doc.body)
    return english_doc, att


def english_rule_title(file_name: str, fallback_title: str) -> str:
    stem = Path(file_name).stem.strip()
    if stem:
        return stem
    return f"{fallback_title} (English)"
