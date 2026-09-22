from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from krx_rule_markdown.contracts import (
    CONVERTER_VERSION,
    canonical_json_hash,
    release_hash,
)
from krx_rule_markdown.manifest import build_manifest, write_manifest_atomic
from krx_rule_markdown.markdown import document_bundle_dir, load_documents, write_document
from krx_rule_markdown.models import (
    ATTACHMENT_CONVERTED,
    Attachment,
    Document,
    Item,
)
from krx_rule_markdown.sync import SyncRunner, fetch_english_rule_document, stale_attachment, write_sync_run_report
from krx_rule_markdown.validate import validate_data


STALE_CODE = "stale_due_to_refresh_failure"


class RefreshOperationalMetadataTests(unittest.TestCase):
    def make_document(self, **fields) -> Document:
        return Document(source_url="https://example.test", document_type="rule",
                        collected_at="2026-09-22T00:00:00Z", **fields)

    def test_refresh_keeps_suffixed_bundle_after_previous_revision_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self.make_document(id="old", title="같은 규정", body="이전 본문")
            current = self.make_document(id="current", title="같은 규정", body="현재 본문")
            old_path = write_document(root, old)
            current_path = write_document(root, current)
            self.assertNotEqual(old_path.parent, current_path.parent)
            old_path.unlink()
            old_path.parent.rmdir()
            reloaded = load_documents(root)[0]
            self.assertEqual(document_bundle_dir(root, reloaded), current_path.parent)

    def test_recent_refresh_preserves_category_missing_from_recent_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = self.make_document(id="current", title="규정", category="업무규정 / 시장", body="본문")
            write_document(root, previous)
            runner = SyncRunner(data_dir=root, base_url="https://example.test", limit=0,
                                rule_id="", recent_only=True, download_attachments=False,
                                language="ko", allowed_failure_ids=set())
            runner.existing_docs = {("ko", "rule", previous.id): previous}
            runner.client = mock.Mock()
            runner.client.fetch_document.return_value = self.make_document(id="current", title="규정", body="새 본문")
            refreshed = runner.fetch_document(Item(id="current", title="규정", document_type="rule"), 1, 1)
            self.assertEqual(refreshed.category, previous.category)
            self.assertEqual(refreshed.path, previous.path)

    def test_english_refresh_keeps_bundle_when_source_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = self.make_document(id="current-en", title="Rule", language="en", body="Old English text.")
            previous.path = str(root / "en/rules/rule-current-en/index.md")
            old_path = write_document(root, previous)
            client = mock.Mock()
            client.download_rule_file.return_value = (
                Attachment(id="current-en-file", title="Rule", file_name="Rule.txt"),
                b"Updated English rule text.",
            )
            korean = self.make_document(id="current", title="규정", body="본문", published_date="2026-09-22")
            refreshed, _, error = fetch_english_rule_document(
                root, client, Item(id="current", title="규정", document_type="rule"), korean, previous,
            )
            self.assertEqual(error, "")
            self.assertIsNotNone(refreshed)
            self.assertEqual(write_document(root, refreshed), old_path)
            self.assertEqual(len(load_documents(root)), 1)
            self.assertEqual((root / refreshed.raw_path).parent.parent, old_path.parent)

    def test_attachment_mapping_reads_but_omits_refresh_operational_fields(self) -> None:
        attachment = Attachment.from_mapping(
            {
                "id": "attachment-1",
                "title": "attachment",
                "file_name": "source.txt",
                "conversion_status": ATTACHMENT_CONVERTED,
                "last_refresh_error": "/tmp/private/source.txt: connection reset",
                "last_refresh_failed_at": "2026-07-21T01:02:03Z",
            }
        )

        self.assertEqual(
            attachment.last_refresh_error,
            "/tmp/private/source.txt: connection reset",
        )
        self.assertEqual(attachment.last_refresh_failed_at, "2026-07-21T01:02:03Z")
        self.assertNotIn("last_refresh_error", attachment.to_mapping())
        self.assertNotIn("last_refresh_failed_at", attachment.to_mapping())

    def test_release_hash_ignores_nested_refresh_operational_fields(self) -> None:
        first = {
            "generated_at": "2026-07-21T01:00:00Z",
            "source_response_hash": "first-response",
            "documents": [
                {
                    "id": "rule-1",
                    "attachments": [
                        {
                            "id": "attachment-1",
                            "last_refresh_error": "/tmp/first: timeout",
                            "last_refresh_failed_at": "2026-07-21T01:01:00Z",
                        }
                    ],
                }
            ],
            "attachment_log": [
                {
                    "id": "attachment-1",
                    "last_refresh_error": "/tmp/first: timeout",
                    "last_refresh_failed_at": "2026-07-21T01:01:00Z",
                }
            ],
        }
        second = copy.deepcopy(first)
        second["generated_at"] = "2026-07-21T02:00:00Z"
        second["source_response_hash"] = "second-response"
        second["documents"][0]["attachments"][0]["last_refresh_error"] = "different OS error"
        second["documents"][0]["attachments"][0]["last_refresh_failed_at"] = (
            "2026-07-21T02:01:00Z"
        )
        second["attachment_log"][0]["last_refresh_error"] = "different OS error"
        second["attachment_log"][0]["last_refresh_failed_at"] = "2026-07-21T02:01:00Z"

        self.assertEqual(release_hash(first), release_hash(second))

    def test_validate_rejects_legacy_v2_release_hash_with_refresh_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            document = make_converted_document(root)
            index_path = write_document(root, document)
            manifest = write_manifest_atomic(root, [document], source="https://example.test")

            inject_legacy_refresh_frontmatter(index_path)
            inject_legacy_refresh_manifest(manifest)
            manifest["release_hash"] = legacy_release_hash(manifest)
            self.assertNotEqual(manifest["release_hash"], release_hash(manifest))
            (root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            errors = validate_data(root, release_mode=True)

        self.assertTrue(any("release_hash mismatch" in error for error in errors), errors)

    def test_manifest_rewrite_does_not_preserve_refresh_fields_on_hash_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            document = make_converted_document(root)
            index_path = write_document(root, document)
            clean = write_manifest_atomic(root, [document], source="https://example.test")
            inject_legacy_refresh_frontmatter(index_path)
            existing = copy.deepcopy(clean)
            inject_legacy_refresh_manifest(existing)
            # Model the transitional case where a producer has already adopted
            # the new scrubbed hash but the old keys remain in the JSON.
            existing["release_hash"] = clean["release_hash"]
            (root / "manifest.json").write_text(
                json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            written = write_manifest_atomic(root, [document], source="https://example.test")
            on_disk = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            validation_errors = validate_data(root, release_mode=True)

        for payload in (written, on_disk):
            self.assertNotIn(
                "last_refresh_error",
                payload["documents"][0]["attachments"][0],
            )
            self.assertNotIn(
                "last_refresh_failed_at",
                payload["documents"][0]["attachments"][0],
            )
            self.assertNotIn("last_refresh_error", payload["attachment_log"][0])
            self.assertNotIn("last_refresh_failed_at", payload["attachment_log"][0])
        self.assertEqual(validation_errors, [])

    def test_sync_stale_lkg_writes_failure_details_only_to_run_report(self) -> None:
        class BrokenAttachmentClient:
            def download_attachment(self, attachment):
                failed = copy.deepcopy(attachment)
                return failed, b"not a real PDF"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            previous = make_converted_document(root, file_name="source.pdf")
            write_document(root, previous)
            current = Document(
                id=previous.id,
                title=previous.title,
                source_url=previous.source_url,
                document_type=previous.document_type,
                collected_at="2026-07-21T00:00:00Z",
                body=previous.body,
                attachments=[
                    Attachment(
                        id=previous.attachments[0].id,
                        title="attachment",
                        file_name="source.pdf",
                        server_file="source.pdf",
                    )
                ],
            )
            runner = SyncRunner(
                data_dir=root,
                base_url="https://example.test",
                limit=0,
                recent_only=False,
                rule_id="",
                download_attachments=True,
                language="ko",
            )
            runner.client = BrokenAttachmentClient()

            current.attachments = runner.download_and_convert_attachments(current, previous)
            write_document(root, current)
            manifest = write_manifest_atomic(root, [current], source="https://example.test")
            write_sync_run_report(root, runner.run_provenance, 0)

            index_text = Path(current.path).read_text(encoding="utf-8")
            manifest_text = json.dumps(manifest, ensure_ascii=False)
            report = json.loads(
                (root.parent / ".krx-rule-runs" / "latest.json").read_text(encoding="utf-8")
            )
            report_was_written_inside_release = (root / ".krx-rule-runs").exists()

            second = copy.deepcopy(current)
            second.attachments = [
                stale_attachment(current.attachments[0], "different transient failure")
            ]
            second_manifest = build_manifest(root, [second], source="https://example.test")

        self.assertIn(STALE_CODE, current.attachments[0].quality_codes)
        failure = report["documents"][-1]
        self.assertNotIn("last_refresh_error", index_text)
        self.assertNotIn("last_refresh_failed_at", index_text)
        self.assertNotIn(failure["error"], index_text)
        self.assertNotIn("last_refresh_error", manifest_text)
        self.assertNotIn("last_refresh_failed_at", manifest_text)
        self.assertNotIn(failure["error"], manifest_text)
        self.assertEqual(manifest["release_hash"], second_manifest["release_hash"])
        self.assertFalse(report_was_written_inside_release)
        self.assertEqual(report["operation"], "sync")
        self.assertEqual(failure["operation"], "sync")
        self.assertEqual(failure["document_id"], current.id)
        self.assertEqual(failure["attachment_id"], current.attachments[0].id)
        self.assertEqual(failure["outcome"], "stale")
        self.assertTrue(failure["failed_at"])
        self.assertIn("PDF", failure["error"])

    def test_sync_same_byte_success_clears_stale_refresh_state(self) -> None:
        class SameAttachmentClient:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def download_attachment(self, attachment):
                return copy.deepcopy(attachment), self.data

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            previous = make_converted_document(root)
            write_document(root, previous)
            stale = previous.attachments[0]
            stale.last_refresh_error = "temporary timeout"
            stale.last_refresh_failed_at = "2026-07-21T01:02:03Z"
            stale.quality_codes = [STALE_CODE]
            stale.quality_flags = STALE_CODE
            stale.quality_status = "warn"
            source_bytes = (root / stale.raw_path).read_bytes()
            current = Document(
                id=previous.id,
                title=previous.title,
                source_url=previous.source_url,
                document_type=previous.document_type,
                collected_at="2026-07-21T00:00:00Z",
                body=previous.body,
                attachments=[
                    Attachment(
                        id=stale.id,
                        title="fresh source title",
                        file_name=stale.file_name,
                        server_file="source.txt",
                    )
                ],
            )
            runner = SyncRunner(
                data_dir=root,
                base_url="https://example.test",
                limit=0,
                recent_only=False,
                rule_id="",
                download_attachments=True,
                language="ko",
            )
            runner.client = SameAttachmentClient(source_bytes)

            refreshed = runner.download_and_convert_attachments(current, previous)[0]

        self.assertEqual(refreshed.last_refresh_error, "")
        self.assertEqual(refreshed.last_refresh_failed_at, "")
        self.assertNotIn(STALE_CODE, refreshed.quality_codes)
        self.assertNotIn(STALE_CODE, refreshed.quality_flags)
        self.assertEqual(refreshed.quality_status, "ok")
        self.assertEqual(runner.run_provenance, [])


def make_converted_document(root: Path, *, file_name: str = "source.txt") -> Document:
    bundle = root / "ko" / "rules" / "refresh-metadata"
    raw_relative = f"ko/rules/refresh-metadata/raw/{file_name}"
    text_relative = "ko/rules/refresh-metadata/attachments/source.md"
    raw_path = root / raw_relative
    text_path = root / text_relative
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"same source bytes")
    text_path.write_text(
        "Converted attachment text with enough searchable content for a stable fixture.\n",
        encoding="utf-8",
    )
    return Document(
        id="rule-refresh",
        title="refresh metadata",
        source_url="https://example.test/rule-refresh",
        document_type="rule",
        collected_at="2026-07-01T00:00:00Z",
        body="Stable last-known-good document body.",
        attachments=[
            Attachment(
                id="attachment-refresh",
                title="attachment",
                file_name=file_name,
                raw_path=raw_relative,
                text_path=text_relative,
                status=ATTACHMENT_CONVERTED,
                preservation_status="preserved",
                searchable=True,
                converter_version=CONVERTER_VERSION,
                quality_status="ok",
            )
        ],
    )


def inject_legacy_refresh_frontmatter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    marker = '    conversion_status: "converted"\n'
    replacement = (
        marker
        + '    last_refresh_error: "/tmp/private/source.txt: timeout"\n'
        + '    last_refresh_failed_at: "2026-07-21T01:02:03Z"\n'
    )
    if marker not in text:
        raise AssertionError("attachment conversion_status marker was not rendered")
    path.write_text(text.replace(marker, replacement, 1), encoding="utf-8")


def inject_legacy_refresh_manifest(payload: dict) -> None:
    for attachment in (
        payload["documents"][0]["attachments"][0],
        payload["attachment_log"][0],
    ):
        attachment["last_refresh_error"] = "/tmp/private/source.txt: timeout"
        attachment["last_refresh_failed_at"] = "2026-07-21T01:02:03Z"


def legacy_release_hash(payload: dict) -> str:
    excluded = {
        "release_hash",
        "generated_at",
        "last_checked_at",
        "source_response_hash",
    }

    def scrub(value):
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in excluded
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return canonical_json_hash(scrub(payload))


if __name__ == "__main__":
    unittest.main()
