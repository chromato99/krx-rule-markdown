from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

from krx_rule_markdown.contracts import CONVERTER_VERSION
from krx_rule_markdown.markdown import load_documents, write_document
from krx_rule_markdown.models import (
    ATTACHMENT_CONVERTED,
    ATTACHMENT_FAILED,
    Attachment,
    Document,
)
from krx_rule_markdown.reconvert import reconvert_data
from krx_rule_markdown.repository import CorpusMutationError
from krx_rule_markdown.sync import write_manifest


def snapshot_tree(root: Path) -> dict[str, tuple[str, int, bytes]]:
    snapshot: dict[str, tuple[str, int, bytes]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        stat = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("directory", stat.st_mtime_ns, b"")
        else:
            snapshot[relative] = ("file", stat.st_mtime_ns, path.read_bytes())
    return snapshot


class ReconvertMergeBlockerTests(unittest.TestCase):
    def make_document(self, attachment: Attachment, *, language: str = "ko") -> Document:
        return Document(
            id="reconvert-blocker",
            title="Reconvert Blocker",
            source_url="https://example.test/reconvert-blocker",
            document_type="rule",
            language=language,
            collected_at="2026-07-01T00:00:00Z",
            body="A stable and searchable last-known-good document body.",
            attachments=[attachment],
        )

    def write_release(
        self,
        root: Path,
        doc: Document,
        *,
        allowed_failure_ids: set[str] | None = None,
    ) -> None:
        write_document(root, doc)
        write_manifest(
            root,
            [doc],
            [],
            "https://example.test",
            allowed_failure_ids=allowed_failure_ids,
        )

    def test_reconvert_dry_run_with_english_title_normalization_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            bundle = "en/rules/20250922-membership-regulation"
            current_raw = f"{bundle}/raw/current.txt"
            current_text = f"{bundle}/attachments/current.md"
            stale_raw = f"{bundle}/raw/stale.txt"
            stale_text = f"{bundle}/attachments/stale.md"
            for relative, content in (
                (current_raw, "current source"),
                (current_text, "current source\n"),
                (stale_raw, "stale source"),
                (stale_text, "old converted text\n"),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            doc = Document(
                id="reconvert-dry-run",
                title="20250922_Membership_Regulation",
                source_url="https://example.test/reconvert-dry-run",
                document_type="rule",
                language="en",
                collected_at="2026-07-01T00:00:00Z",
                body="Stable English body.",
                file_name="20250922_Membership_Regulation.pdf",
                attachments=[
                    Attachment(
                        id="current",
                        file_name="current.txt",
                        raw_path=current_raw,
                        text_path=current_text,
                        status=ATTACHMENT_CONVERTED,
                        converter_version=CONVERTER_VERSION,
                    ),
                    Attachment(
                        id="stale",
                        file_name="stale.txt",
                        raw_path=stale_raw,
                        text_path=stale_text,
                        status=ATTACHMENT_CONVERTED,
                        converter_version="legacy",
                    ),
                ],
            )
            self.write_release(root, doc)
            report = root / "reports/data-quality.json"
            report.parent.mkdir(parents=True)
            report.write_text('{"stable": true}\n', encoding="utf-8")
            asset = root / f"{bundle}/assets/candidate.bin"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"stable asset")
            fixed_time_ns = 1_700_000_000_000_000_000
            for path in sorted(root.rglob("*"), reverse=True):
                os.utime(path, ns=(fixed_time_ns, fixed_time_ns))
            os.utime(root, ns=(fixed_time_ns, fixed_time_ns))
            before = snapshot_tree(root)

            with (
                mock.patch(
                    "krx_rule_markdown.reconvert.convert_attachment",
                    side_effect=AssertionError("dry-run must not invoke conversion"),
                ),
                mock.patch(
                    "krx_rule_markdown.reconvert.preserve_hwp_attachment_assets",
                    side_effect=AssertionError("dry-run must not preserve assets"),
                ),
            ):
                result = reconvert_data(root, dry_run=True)

            self.assertEqual(snapshot_tree(root), before)
            self.assertEqual(result.documents, 1)
            self.assertEqual(result.attachments, 2)
            self.assertEqual((result.converted, result.skipped, result.failed), (1, 1, 0))
            self.assertEqual(result.metadata_updates, 1)
            self.assertFalse((root.parent / ".krx-rule-runs").exists())

    def test_reconvert_dry_run_with_current_hwp_asset_candidate_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            raw_relative = "ko/rules/reconvert-blocker/raw/current.hwp"
            text_relative = "ko/rules/reconvert-blocker/attachments/current.md"
            raw = root / raw_relative
            text = root / text_relative
            raw.parent.mkdir(parents=True)
            text.parent.mkdir(parents=True)
            raw.write_bytes(b"current HWP bytes with an uninspected asset candidate")
            text.write_text("Current converted HWP text remains unchanged.\n", encoding="utf-8")
            attachment = Attachment(
                id="hwp-asset-candidate",
                file_name="current.hwp",
                raw_path=raw_relative,
                text_path=text_relative,
                status=ATTACHMENT_CONVERTED,
                converter_version=CONVERTER_VERSION,
                asset_inspection_version="",
                searchable=True,
            )
            self.write_release(root, self.make_document(attachment))
            before = snapshot_tree(root)

            with mock.patch(
                "krx_rule_markdown.reconvert.preserve_hwp_attachment_assets",
                side_effect=AssertionError("dry-run must not preserve HWP assets"),
            ):
                result = reconvert_data(root, dry_run=True)

            self.assertEqual(snapshot_tree(root), before)
            self.assertEqual((result.converted, result.skipped, result.failed), (1, 0, 0))
            self.assertFalse((root.parent / ".krx-rule-runs").exists())

    def test_reconvert_dry_run_reports_rawless_pending_attachment_as_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            attachment = Attachment(
                id="rawless-pending",
                file_name="missing.txt",
            )
            self.write_release(root, self.make_document(attachment))
            before = snapshot_tree(root)
            active_inode = root.stat().st_ino

            result = reconvert_data(root, dry_run=True)

            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_tree(root), before)
            self.assertEqual((result.required_failed, result.failed, result.skipped), (1, 1, 0))
            self.assertFalse((root.parent / ".krx-rule-runs").exists())

    def test_reconvert_conversion_failure_does_not_publish_staging_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            raw_relative = "ko/rules/reconvert-blocker/raw/source.txt"
            raw = root / raw_relative
            raw.parent.mkdir(parents=True)
            raw.write_text("source to reconvert", encoding="utf-8")
            attachment = Attachment(
                id="fresh-failure",
                file_name="source.txt",
                raw_path=raw_relative,
            )
            self.write_release(root, self.make_document(attachment))
            before = snapshot_tree(root)
            active_inode = root.stat().st_ino

            def fail_after_partial_write(
                _raw_path: Path,
                text_path: Path,
                att: Attachment,
                **_kwargs,
            ) -> Attachment:
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text("partial staging output", encoding="utf-8")
                att.status = ATTACHMENT_FAILED
                att.error = "injected conversion failure"
                att.text_path = ""
                att.searchable = False
                return att

            with (
                mock.patch(
                    "krx_rule_markdown.reconvert.convert_attachment",
                    side_effect=fail_after_partial_write,
                ),
                self.assertRaises(CorpusMutationError) as caught,
            ):
                reconvert_data(root)

            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_tree(root), before)
            result = getattr(caught.exception, "reconvert_result")
            self.assertEqual((result.required_failed, result.failed), (1, 1))
            self.assertEqual(result.failure_events[0]["attachment_id"], "fresh-failure")
            self.assertEqual(result.failure_events[0]["outcome"], "failed")
            run_report = json.loads(
                (root.parent / ".krx-rule-runs" / "latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_report["operation"], "reconvert")
            self.assertEqual(run_report["result"], "failed")
            self.assertIn("CorpusMutationError", run_report["error"])
            self.assertEqual(
                run_report["documents"][0],
                result.failure_events[0] | {"operation": "reconvert"},
            )

    def test_reconvert_lkg_refresh_failure_is_blocking_and_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            raw_relative = "ko/rules/reconvert-blocker/raw/broken.pdf"
            text_relative = "ko/rules/reconvert-blocker/attachments/broken.md"
            raw = root / raw_relative
            text = root / text_relative
            raw.parent.mkdir(parents=True)
            text.parent.mkdir(parents=True)
            raw.write_bytes(b"not a pdf")
            lkg = ("Last-known-good searchable attachment text remains intact. " * 10) + "\n"
            text.write_text(lkg, encoding="utf-8")
            attachment = Attachment(
                id="lkg-failure",
                file_name="broken.pdf",
                raw_path=raw_relative,
                text_path=text_relative,
                status=ATTACHMENT_CONVERTED,
                converter_version=CONVERTER_VERSION,
                preservation_status="preserved",
                searchable=True,
                quality_status="ok",
            )
            self.write_release(root, self.make_document(attachment))
            before = snapshot_tree(root)
            active_inode = root.stat().st_ino

            with self.assertRaises(CorpusMutationError) as caught:
                reconvert_data(root, force=True)

            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_tree(root), before)
            result = getattr(caught.exception, "reconvert_result")
            self.assertEqual((result.stale_retained, result.failed, result.converted), (1, 1, 0))
            self.assertEqual(result.failure_events[0]["outcome"], "stale")

    def test_reconvert_allowlisted_failure_is_untouched_unless_forced(self) -> None:
        def prepare(root: Path) -> None:
            raw_relative = "ko/rules/reconvert-blocker/raw/allowed.pdf"
            raw = root / raw_relative
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"not a pdf")
            attachment = Attachment(
                id="allowed-failure",
                file_name="allowed.pdf",
                raw_path=raw_relative,
                status=ATTACHMENT_FAILED,
                preservation_status="preserved",
                searchable=False,
                error="reviewed historical failure",
            )
            self.write_release(
                root,
                self.make_document(attachment),
                allowed_failure_ids={"allowed-failure"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            prepare(root)
            before = snapshot_tree(root)
            with mock.patch(
                "krx_rule_markdown.reconvert.convert_attachment",
                side_effect=AssertionError("allowlisted failure must remain untouched"),
            ):
                result = reconvert_data(root)
            loaded = load_documents(root)[0].attachments[0]
            self.assertEqual(result.allowed_failed, 1)
            self.assertEqual(result.failed, 0)
            self.assertEqual(loaded.error, "reviewed historical failure")
            self.assertEqual(snapshot_tree(root), before)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            prepare(root)
            before = snapshot_tree(root)
            active_inode = root.stat().st_ino
            with self.assertRaises(CorpusMutationError) as caught:
                reconvert_data(root, force=True)
            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_tree(root), before)
            result = getattr(caught.exception, "reconvert_result")
            self.assertEqual(result.required_failed, 1)
            self.assertEqual(result.allowed_failed, 0)

    def test_reconvert_hwp_inspection_failure_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            raw_relative = "ko/rules/reconvert-blocker/raw/current.hwp"
            text_relative = "ko/rules/reconvert-blocker/attachments/current.md"
            raw = root / raw_relative
            text = root / text_relative
            raw.parent.mkdir(parents=True)
            text.parent.mkdir(parents=True)
            raw.write_bytes(b"current HWP bytes")
            text.write_text("Current converted HWP text remains active.\n", encoding="utf-8")
            attachment = Attachment(
                id="hwp-inspection",
                file_name="current.hwp",
                raw_path=raw_relative,
                text_path=text_relative,
                status=ATTACHMENT_CONVERTED,
                converter_version=CONVERTER_VERSION,
                asset_inspection_version="",
                searchable=True,
            )
            self.write_release(root, self.make_document(attachment))
            before = snapshot_tree(root)
            active_inode = root.stat().st_ino

            with (
                mock.patch(
                    "krx_rule_markdown.reconvert.SourceInspectionCache.hwp_images",
                    return_value=([], "injected HWP inspection failure"),
                ),
                self.assertRaises(CorpusMutationError) as caught,
            ):
                reconvert_data(root)

            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_tree(root), before)
            result = getattr(caught.exception, "reconvert_result")
            self.assertEqual((result.inspection_failed, result.failed), (1, 1))
            self.assertEqual(result.failure_events[0]["outcome"], "inspection_failed")

    def test_reconvert_release_quality_error_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            raw_relative = "ko/rules/reconvert-blocker/raw/current.txt"
            text_relative = "ko/rules/reconvert-blocker/attachments/current.md"
            raw = root / raw_relative
            text = root / text_relative
            raw.parent.mkdir(parents=True)
            text.parent.mkdir(parents=True)
            raw.write_text("current source", encoding="utf-8")
            text.write_text("current source\n", encoding="utf-8")
            attachment = Attachment(
                id="quality-gate",
                file_name="current.txt",
                raw_path=raw_relative,
                text_path=text_relative,
                status=ATTACHMENT_CONVERTED,
                converter_version=CONVERTER_VERSION,
                searchable=True,
            )
            self.write_release(root, self.make_document(attachment))
            before = snapshot_tree(root)
            active_inode = root.stat().st_ino
            report = {
                "issues": [
                    {
                        "severity": "error",
                        "code": "empty_text",
                        "document_id": "reconvert-blocker",
                        "attachment_id": "quality-gate",
                        "message": "injected release quality failure",
                    }
                ]
            }

            with (
                mock.patch(
                    "krx_rule_markdown.reconvert.audit_data_quality",
                    return_value=report,
                ),
                self.assertRaises(CorpusMutationError) as caught,
            ):
                reconvert_data(root)

            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_tree(root), before)
            result = getattr(caught.exception, "reconvert_result")
            self.assertEqual((result.quality_failed, result.failed), (1, 1))
            self.assertEqual(result.failure_events[0]["outcome"], "quality_failed")


if __name__ == "__main__":
    unittest.main()
