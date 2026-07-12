from __future__ import annotations

from pathlib import Path
import copy
import json
import re
import shutil
import sys
import tempfile

from .collector import Client, guess_mime_type
from .convert import convert_attachment
from .assets import preserve_hwp_attachment_assets, preserve_inline_document_assets
from .contracts import CONVERTER_VERSION, RELEASE_PROFILE_VERSION, add_quality_code, canonical_json_hash, canonical_text_hash
from .manifest import write_manifest_atomic
from .markdown import document_bundle_dir, load_documents, write_document
from .models import ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, LANGUAGE_EN, LANGUAGE_KO, Document, hash_bytes, hash_text, now_utc
from .models import DOCUMENT_RULE, Item
from .paths import converted_attachment_path, raw_attachment_path
from .quality import _audit_data_quality, mark_quality_failure, report_failures, write_quality_report
from .converters.cache import SourceInspectionCache
from .repository import (
    WriterLock,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    create_staged_corpus as create_staging_corpus,
    publish_staged_corpus,
)
from .validate import validate_data


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
    allowed_failure_ids: set[str] | None = None,
) -> int:
    with WriterLock(data_dir, "sync"):
        staging: Path | None = create_staging_corpus(data_dir)
        runner: SyncRunner | None = None
        final_result = 1
        run_error = ""
        try:
            runner = SyncRunner(
                data_dir=staging,
                base_url=base_url,
                limit=limit,
                recent_only=recent_only,
                rule_id=rule_id,
                download_attachments=download_attachments,
                language=language,
                allowed_failure_ids=allowed_failure_ids,
            )
            final_result = runner.run()
            if final_result:
                return final_result
            if not runner.is_partial_sync():
                prune_staging_documents(staging)
            quality_report = _audit_data_quality(
                staging,
                update_metadata=True,
                allowed_failure_ids=runner.allowed_failure_ids,
                release_gate=True,
            )
            write_quality_report(staging / "reports" / "data-quality.json", quality_report)
            quality_errors = report_failures(quality_report, "error")
            if quality_errors:
                for quality_error in quality_errors:
                    print(quality_error, file=sys.stderr)
                print(
                    f"error: staged corpus failed release quality gate with {len(quality_errors)} error(s)",
                    file=sys.stderr,
                )
                final_result = 1
                return 1
            errors = validate_data(staging, release_mode=True)
            if errors:
                for validation_error in errors:
                    print(validation_error, file=sys.stderr)
                print(f"error: staged corpus failed validation with {len(errors)} error(s)", file=sys.stderr)
                final_result = 1
                return 1
            final_result = 1
            publish_staged_corpus(data_dir, staging)
            staging = None
            final_result = 0
            return 0
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                write_sync_run_report(
                    data_dir,
                    runner.run_provenance if runner is not None else [],
                    final_result,
                    error=run_error,
                )
            except OSError as exc:
                print(f"warning: could not write sync run report: {exc}", file=sys.stderr)
            if staging is not None and staging.exists():
                shutil.rmtree(staging)


class SyncRunner:
    def __init__(
        self,
        *,
        data_dir: Path,
        base_url: str,
        limit: int,
        recent_only: bool,
        rule_id: str,
        download_attachments: bool,
        language: str,
        allowed_failure_ids: set[str] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.base_url = base_url
        self.limit = limit
        self.recent_only = recent_only
        self.rule_id = rule_id
        self.download_attachments = download_attachments
        self.language = normalize_sync_language(language)
        self.allowed_failure_ids = set(allowed_failure_ids or set())
        self.client = Client(base_url)
        self.manifest_docs: list[Document] = []
        self.attachment_log = []
        self.existing_docs: dict[tuple[str, str, str], Document] = {}
        self.required_failures: list[str] = []
        self.run_provenance: list[dict[str, str]] = []

    def run(self) -> int:
        try:
            self.existing_docs = {document_key(doc): doc for doc in load_documents(self.data_dir)}
        except (OSError, ValueError):
            self.existing_docs = {}
        self.client.bootstrap()
        items = dedupe_items(self.items_to_collect())
        guard_error = collection_guard_error(self.data_dir, items, self.language, self.is_partial_sync())
        if guard_error:
            print(f"error: {guard_error}", file=sys.stderr)
            return 1
        for idx, item in enumerate(items, start=1):
            doc = self.fetch_document(item, idx, len(items))
            if doc is None:
                if not self.preserve_failed_document(item):
                    self.required_failures.append(f"required document fetch failed: {first_document_id(item)}")
                continue
            self.write_korean_document(doc)
            self.write_english_document(item, doc)
        if self.required_failures:
            for failure in self.required_failures:
                print(f"error: {failure}", file=sys.stderr)
            return 1
        if not self.manifest_docs and not self.is_partial_sync():
            print("error: sync fetched 0 documents; refusing to rewrite manifest", file=sys.stderr)
            return 1
        write_manifest(
            self.data_dir,
            self.manifest_docs,
            self.attachment_log,
            self.base_url,
            preserve_existing=self.is_partial_sync(),
            allowed_failure_ids=self.allowed_failure_ids,
        )
        return 0

    def is_partial_sync(self) -> bool:
        return bool(self.rule_id or self.recent_only or self.limit or self.language != LANGUAGE_ALL)

    def items_to_collect(self) -> list[Item]:
        if self.rule_id:
            items = [
                Item(
                    id=self.rule_id,
                    book_id=self.rule_id,
                    title=self.rule_id,
                    document_type=DOCUMENT_RULE,
                    noformyn="N",
                )
            ]
        else:
            items = collect_items(self.client, self.limit, self.recent_only, self.language)
        if self.limit and len(items) > self.limit:
            return items[: self.limit]
        return items

    def fetch_document(self, item: Item, index: int, total: int) -> Document | None:
        print(f"fetching {index}/{total} {item.document_type} {item.id} {item.title}", file=sys.stderr)
        try:
            doc = self.client.fetch_document(item)
        except Exception as exc:  # noqa: BLE001 - keep long syncs moving.
            print(f"warning: document fetch failed for {item.id}: {exc}", file=sys.stderr)
            self.run_provenance.append(
                {
                    "document_id": first_document_id(item),
                    "outcome": "failed",
                    "failed_at": now_utc(),
                    "error": str(exc),
                }
            )
            return None
        doc.language = LANGUAGE_KO
        self.run_provenance.append(
            {
                "document_id": doc.id,
                "fetched_at": doc.collected_at,
                "source_response_hash": doc.source_response_hash,
                "source_content_hash": doc.source_content_hash,
                "outcome": "fetched",
            }
        )
        if includes_korean(self.language):
            previous = self.existing_docs.get((LANGUAGE_KO, doc.document_type, doc.id))
            if self.download_attachments:
                if previous is not None:
                    doc.path = previous.path
                doc.attachments = self.download_and_convert_attachments(doc, previous)
            elif previous is not None:
                previous_by_id = {att.id: att for att in previous.attachments}
                doc.attachments = [
                    copy.deepcopy(previous_by_id.get(att.id, att))
                    for att in doc.attachments
                ]
        return doc

    def download_and_convert_attachments(self, doc: Document, previous_doc: Document | None = None) -> list:
        converted = []
        previous_by_id = {att.id: att for att in previous_doc.attachments} if previous_doc else {}
        used_converted_names = {Path(att.text_path).name for att in previous_by_id.values() if att.text_path}
        used_raw_names = {Path(att.raw_path).name for att in previous_by_id.values() if att.raw_path}
        for att in doc.attachments:
            previous = previous_by_id.get(att.id)
            if not att.server_file:
                converted.append(copy.deepcopy(previous) if previous else att)
                continue
            try:
                att, data = self.client.download_attachment(att)
                downloaded_hash = hash_bytes(data)
                if reusable_attachment(self.data_dir, previous, downloaded_hash):
                    reused = merge_attachment_source_metadata(previous, att)
                    if Path(reused.raw_path).suffix.lower() == ".hwp" and reused.asset_inspection_version != "1":
                        cache = SourceInspectionCache()
                        streams, inspection_error = cache.hwp_images(self.data_dir / reused.raw_path)
                        if not inspection_error:
                            preserve_hwp_attachment_assets(self.data_dir, doc, reused, streams=streams)
                    converted.append(reused)
                    continue
                raw_path = (
                    self.data_dir / previous.raw_path
                    if previous and previous.raw_path
                    else raw_attachment_path(self.data_dir, doc, att, used_raw_names)
                )
                if not att.mime_type:
                    att.mime_type = guess_mime_type(raw_path)
                text_path = (
                    self.data_dir / previous.text_path
                    if previous and previous.text_path
                    else converted_attachment_path(self.data_dir, doc, att, used_converted_names)
                )
                inspection_cache = SourceInspectionCache()
                hwp_streams = None
                hwp_stream_error = ""
                with tempfile.TemporaryDirectory(prefix="krx-rule-convert-") as tmp:
                    staged_raw = Path(tmp) / raw_path.name
                    staged_text = Path(tmp) / text_path.name
                    atomic_write_bytes(staged_raw, data)
                    att = convert_attachment(
                        staged_raw,
                        staged_text,
                        att,
                        inspection_cache=inspection_cache,
                    )
                    if staged_raw.suffix.lower() == ".hwp":
                        hwp_streams, hwp_stream_error = inspection_cache.hwp_images(staged_raw)
                    converted_bytes = staged_text.read_bytes() if att.status == ATTACHMENT_CONVERTED else b""
                if att.status == ATTACHMENT_CONVERTED:
                    atomic_write_bytes(raw_path, data)
                    atomic_write_bytes(text_path, converted_bytes)
                    att.text_path = str(text_path)
                elif previous is not None:
                    att = stale_attachment(previous, att.error or "attachment conversion failed")
                    self.record_attachment_failure(doc.id, att.id, att.last_refresh_error, "stale")
                else:
                    atomic_write_bytes(raw_path, data)
                    if att.id not in self.allowed_failure_ids:
                        self.required_failures.append(f"required attachment conversion failed: {doc.id}/{att.id}")
                        outcome = "failed"
                    else:
                        outcome = "degraded"
                    self.record_attachment_failure(doc.id, att.id, att.error or "attachment conversion failed", outcome)
                att.raw_path = str(raw_path.relative_to(self.data_dir))
                if att.text_path:
                    att.text_path = str(text_path.relative_to(self.data_dir))
                if hwp_streams is not None and not hwp_stream_error:
                    preserve_hwp_attachment_assets(self.data_dir, doc, att, streams=hwp_streams)
            except Exception as exc:  # noqa: BLE001 - failure belongs in metadata.
                if previous is not None:
                    att = stale_attachment(previous, str(exc))
                    self.record_attachment_failure(doc.id, att.id, str(exc), "stale")
                else:
                    att.status = ATTACHMENT_FAILED
                    att.error = str(exc)
                    att.text_path = ""
                    att.searchable = False
                    mark_quality_failure(att, "conversion_failed")
                    self.required_failures.append(f"required attachment refresh failed: {doc.id}/{att.id}: {exc}")
                    self.record_attachment_failure(doc.id, att.id, str(exc), "failed")
            converted.append(att)
        return converted

    def write_korean_document(self, doc: Document) -> None:
        if not includes_korean(self.language):
            return
        previous = self.existing_docs.get((LANGUAGE_KO, doc.document_type, doc.id))
        if previous is not None:
            doc.path = previous.path
            doc.assets = copy.deepcopy(previous.assets)
        preserve_inline_document_assets(
            self.data_dir,
            doc,
            getattr(self.client, "download_inline_asset", None),
        )
        if reusable_document(previous, doc):
            doc = copy.deepcopy(previous)
        else:
            write_source_provenance(self.data_dir, doc)
            path = write_document(self.data_dir, doc)
            doc.path = str(path)
        self.manifest_docs.append(doc)
        self.attachment_log.extend(doc.attachments)

    def record_attachment_failure(self, document_id: str, attachment_id: str, message: str, outcome: str) -> None:
        self.run_provenance.append(
            {
                "document_id": document_id,
                "attachment_id": attachment_id,
                "outcome": outcome,
                "failed_at": now_utc(),
                "error": message,
            }
        )

    def write_english_document(self, item: Item, doc: Document) -> None:
        if not includes_english(self.language) or doc.document_type != DOCUMENT_RULE:
            return
        previous = self.existing_docs.get((LANGUAGE_EN, DOCUMENT_RULE, f"{doc.id}-en"))
        english_doc, english_log, refresh_error = fetch_english_rule_document(
            self.data_dir,
            self.client,
            item,
            doc,
            previous,
        )
        if refresh_error:
            self.run_provenance.append(
                {
                    "document_id": f"{doc.id}-en",
                    "outcome": "stale" if previous is not None else "unavailable",
                    "failed_at": now_utc(),
                    "error": refresh_error,
                }
            )
        if english_log is not None:
            self.attachment_log.append(english_log)
        if english_doc is not None:
            path = write_document(self.data_dir, english_doc)
            english_doc.path = str(path)
            self.manifest_docs.append(english_doc)

    def preserve_failed_document(self, item: Item) -> bool:
        preserved = False
        if includes_korean(self.language):
            previous = self.existing_docs.get((LANGUAGE_KO, item.document_type, first_document_id(item)))
            if previous is not None:
                self.manifest_docs.append(stale_document(previous, "document fetch failed"))
                mark_run_document_stale(self.run_provenance, first_document_id(item))
                preserved = True
        if includes_english(self.language) and item.document_type == DOCUMENT_RULE:
            previous = self.existing_docs.get((LANGUAGE_EN, DOCUMENT_RULE, f"{first_document_id(item)}-en"))
            if previous is not None:
                self.manifest_docs.append(stale_document(previous, "source document fetch failed"))
                mark_run_document_stale(self.run_provenance, f"{first_document_id(item)}-en")
                preserved = True
        return preserved


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


def collection_guard_error(data_dir: Path, items: list[Item], language: str, partial: bool) -> str:
    if not items:
        return "sync collected 0 items; refusing to rewrite manifest"
    if partial:
        return ""
    try:
        existing_docs = load_documents(data_dir)
    except (OSError, ValueError):
        return ""
    if includes_korean(language):
        existing_primary = sum(1 for doc in existing_docs if doc.language == LANGUAGE_KO)
        if existing_primary >= 10 and len(items) * 2 < existing_primary:
            return (
                f"sync collected only {len(items)} items, below half of existing "
                f"Korean corpus count {existing_primary}; refusing to rewrite manifest"
            )
    return ""


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


def write_manifest(
    data_dir: Path,
    docs: list[Document],
    attachment_log: list,
    source: str,
    *,
    preserve_existing: bool = False,
    allowed_failure_ids: set[str] | None = None,
) -> None:
    if preserve_existing:
        docs = merge_documents(load_documents(data_dir), docs)
        attachment_log = [att for doc in docs for att in doc.attachments]
    write_manifest_atomic(
        data_dir,
        docs,
        source=source,
        release_profile={
            "version": RELEASE_PROFILE_VERSION,
            "default": "strict",
            "allowed_failure_ids": sorted(allowed_failure_ids or set()),
        },
    )


def merge_documents(existing_docs: list[Document], updated_docs: list[Document]) -> list[Document]:
    merged: dict[tuple[str, str, str], Document] = {}
    for doc in existing_docs:
        merged[document_key(doc)] = doc
    for doc in updated_docs:
        merged[document_key(doc)] = doc
    return sorted(
        merged.values(),
        key=lambda doc: (doc.language, doc.document_type, doc.title, doc.id),
    )


def document_key(doc: Document) -> tuple[str, str, str]:
    return (doc.language, doc.document_type, doc.id)


def reusable_attachment(data_dir: Path, previous, downloaded_hash: str) -> bool:
    if previous is None or previous.status != ATTACHMENT_CONVERTED:
        return False
    if previous.converter_version != CONVERTER_VERSION:
        return False
    if (previous.raw_file_hash or previous.content_hash) != downloaded_hash:
        return False
    if not previous.raw_path or not previous.text_path:
        return False
    return (data_dir / previous.raw_path).is_file() and (data_dir / previous.text_path).is_file()


def merge_attachment_source_metadata(previous, current):
    reused = copy.deepcopy(previous)
    for field_name in (
        "title",
        "file_name",
        "mime_type",
        "source_url",
        "server_file",
        "folder",
    ):
        value = getattr(current, field_name, "")
        if value:
            setattr(reused, field_name, value)
    return reused


def reusable_document(previous: Document | None, current: Document) -> bool:
    if previous is None or previous.converter_version != CONVERTER_VERSION:
        return False
    if not previous.source_content_hash or previous.source_content_hash != current.source_content_hash:
        return False
    scalar_fields = (
        "title",
        "source_url",
        "document_type",
        "category",
        "effective_date",
        "published_date",
        "language",
        "source_id",
        "file_name",
        "asset_inspection_version",
        "quality_status",
    )
    if any(getattr(previous, name) != getattr(current, name) for name in scalar_fields):
        return False
    if canonical_text_hash(previous.body) != canonical_text_hash(current.body):
        return False
    if sorted(previous.quality_codes) != sorted(current.quality_codes):
        return False
    return canonical_json_hash(
        {
            "assets": [asset.to_mapping() for asset in previous.assets],
            "attachments": [att.to_mapping() for att in previous.attachments],
        }
    ) == canonical_json_hash(
        {
            "assets": [asset.to_mapping() for asset in current.assets],
            "attachments": [att.to_mapping() for att in current.attachments],
        }
    )


def mark_run_document_stale(records: list[dict[str, str]], document_id: str) -> None:
    for record in reversed(records):
        if record.get("document_id") == document_id and record.get("outcome") == "failed":
            record["outcome"] = "stale"
            return
    records.append(
        {
            "document_id": document_id,
            "outcome": "stale",
            "failed_at": now_utc(),
            "error": "source refresh failed; last-known-good document retained",
        }
    )


def fetch_english_rule_document(
    data_dir: Path,
    client: Client,
    item: Item,
    korean_doc: Document,
    previous_doc: Document | None = None,
) -> tuple[Document | None, object | None, str]:
    try:
        att, data = client.download_rule_file(item, "ENG", "English full text")
    except FileNotFoundError as exc:
        message = str(exc) or "English source file is not available"
        return (stale_document(previous_doc, message), None, message)
    except Exception as exc:  # noqa: BLE001 - keep syncs moving.
        print(f"warning: English rule fetch failed for {korean_doc.id}: {exc}", file=sys.stderr)
        return (stale_document(previous_doc, str(exc)), None, str(exc))

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
        converter_version=CONVERTER_VERSION,
    )
    used_raw_names: set[str] = set()
    used_converted_names: set[str] = set()
    try:
        downloaded_hash = hash_bytes(data)
        expected_title = english_rule_title(att.file_name, korean_doc.title)
        if (
            previous_doc is not None
            and previous_doc.converter_version == CONVERTER_VERSION
            and (previous_doc.raw_file_hash or previous_doc.file_content_hash) == downloaded_hash
            and previous_doc.file_name == att.file_name
            and previous_doc.title == expected_title
            and previous_doc.category == korean_doc.category
            and previous_doc.effective_date == korean_doc.effective_date
            and previous_doc.published_date == korean_doc.published_date
            and previous_doc.raw_path
            and previous_doc.text_path
            and (data_dir / previous_doc.raw_path).is_file()
            and (data_dir / previous_doc.text_path).is_file()
        ):
            return copy.deepcopy(previous_doc), None, ""
        raw_path = raw_attachment_path(data_dir, english_doc, att, used_raw_names)
        if not att.mime_type:
            att.mime_type = guess_mime_type(raw_path)
        text_path = converted_attachment_path(data_dir, english_doc, att, used_converted_names)
        with tempfile.TemporaryDirectory(prefix="krx-rule-convert-") as tmp:
            staged_raw = Path(tmp) / raw_path.name
            staged_text = Path(tmp) / text_path.name
            atomic_write_bytes(staged_raw, data)
            att = convert_attachment(staged_raw, staged_text, att)
            converted_bytes = staged_text.read_bytes() if att.status == ATTACHMENT_CONVERTED else b""
        if att.status == ATTACHMENT_CONVERTED:
            atomic_write_bytes(raw_path, data)
            atomic_write_bytes(text_path, converted_bytes)
            att.text_path = str(text_path)
        att.raw_path = str(raw_path.relative_to(data_dir))
        if att.text_path:
            att.text_path = str(text_path.relative_to(data_dir))
    except Exception as exc:  # noqa: BLE001 - failure belongs in metadata.
        att.status = ATTACHMENT_FAILED
        att.error = str(exc)
        att.text_path = ""
        mark_quality_failure(att, "conversion_failed")

    if att.status != ATTACHMENT_CONVERTED or not att.text_path:
        if previous_doc is not None:
            message = att.error or "English conversion failed"
            return stale_document(previous_doc, message), att, message
        return None, att, att.error or "English conversion failed"

    body = (data_dir / att.text_path).read_text(encoding="utf-8").strip()
    english_doc.body = body
    english_doc.raw_path = att.raw_path
    english_doc.text_path = att.text_path
    english_doc.file_content_hash = att.content_hash
    english_doc.raw_file_hash = att.raw_file_hash or att.content_hash
    english_doc.content_hash = hash_text(english_doc.title + "\n" + english_doc.body)
    english_doc.body_hash = canonical_text_hash(english_doc.body)
    english_doc.preservation_status = att.preservation_status
    english_doc.searchable = att.searchable
    english_doc.quality_status = att.quality_status
    english_doc.quality_codes = list(att.quality_codes)
    return english_doc, att, ""


def write_source_provenance(data_dir: Path, doc: Document) -> None:
    if not doc.source_content_html:
        return
    bundle = document_bundle_dir(data_dir, doc)
    source_path = bundle / "raw" / "source.html"
    request_path = bundle / "raw" / "request.json"
    atomic_write_text(source_path, doc.source_content_html + "\n")
    atomic_write_json(request_path, doc.source_request)
    doc.source_content_path = source_path.relative_to(data_dir).as_posix()
    doc.source_request_path = request_path.relative_to(data_dir).as_posix()
    doc.preservation_status = "preserved"


def stale_attachment(previous, message: str):
    att = copy.deepcopy(previous)
    att.last_refresh_error = message
    att.last_refresh_failed_at = now_utc()
    att.quality_codes = add_quality_code(att.quality_codes or att.quality_flags, "stale_due_to_refresh_failure")
    att.quality_flags = ",".join(att.quality_codes)
    if att.quality_status in {"", "ok"}:
        att.quality_status = "warn"
    return att


def stale_document(previous: Document | None, message: str) -> Document | None:
    if previous is None:
        return None
    doc = copy.deepcopy(previous)
    doc.quality_codes = add_quality_code(doc.quality_codes, "stale_due_to_refresh_failure")
    if doc.quality_status in {"", "ok"}:
        doc.quality_status = "warn"
    return doc


def first_document_id(item: Item) -> str:
    return item.book_id or item.id


def prune_staging_documents(data_dir: Path) -> None:
    manifest = data_dir / "manifest.json"
    if not manifest.exists():
        return
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    referenced = {str(item.get("path") or "") for item in payload.get("documents", []) if item.get("path")}
    for language in (LANGUAGE_KO, LANGUAGE_EN):
        for folder in ("rules", "notices"):
            base = data_dir / language / folder
            if not base.exists():
                continue
            for index_path in base.glob("*/index.md"):
                relative = index_path.relative_to(data_dir).as_posix()
                if relative not in referenced:
                    shutil.rmtree(index_path.parent)


def write_sync_run_report(
    data_dir: Path,
    documents: list[dict[str, str]],
    result: int,
    *,
    error: str = "",
) -> None:
    report_dir = Path(data_dir).parent / ".krx-rule-runs"
    atomic_write_json(
        report_dir / "latest.json",
        {
            "finished_at": now_utc(),
            "result": "ok" if result == 0 else "failed",
            "documents": documents,
            **({"error": error} if error else {}),
        },
    )


def english_rule_title(file_name: str, fallback_title: str) -> str:
    stem = Path(file_name).stem.strip()
    if stem:
        stem = re.sub(r"^\d{8}[_-]?", "", stem)
        title = re.sub(r"[_-]+", " ", stem).strip()
        if title:
            return title
    return f"{fallback_title} (English)"
