from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from krx_rule_markdown.cli import main as cli_main
from krx_rule_markdown.markdown import load_documents, write_document
from krx_rule_markdown.models import (
    ATTACHMENT_CONVERTED,
    ATTACHMENT_FAILED,
    Attachment,
    Document,
)
from krx_rule_markdown.quality import audit_data_quality
from krx_rule_markdown.repository import CorpusMutationError
from krx_rule_markdown.sync import write_manifest as write_sync_manifest
from krx_rule_markdown.validate import validate_data


class QualityGateDefaultTests(unittest.TestCase):
    def test_quality_update_defaults_to_error_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            index_path = create_failed_attachment_corpus(root)
            active_inode = root.stat().st_ino
            before = snapshot_files(root)

            with self.assertRaises(CorpusMutationError):
                audit_data_quality(root, update_metadata=True)

            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_files(root), before)
            self.assertEqual(index_path.read_bytes(), before[index_path.relative_to(root).as_posix()])

    def test_cli_quality_update_metadata_defaults_to_error_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            create_empty_converted_text_corpus(root)
            active_inode = root.stat().st_ino
            before = snapshot_files(root)
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli_main(["quality", "--data-dir", str(root), "--update-metadata"])

            self.assertEqual(result, 1)
            self.assertIn("quality gate", stderr.getvalue())
            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_files(root), before)

    def test_cli_quality_read_only_defaults_to_no_failure_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            create_empty_converted_text_corpus(root)
            active_inode = root.stat().st_ino
            before = snapshot_files(root)
            output = base / "quality.json"
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli_main(
                    ["quality", "--data-dir", str(root), "--output", str(output)]
                )

            self.assertEqual(result, 0)
            self.assertIn("quality documents=1", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue(output.is_file())
            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_files(root), before)

    def test_cli_quality_update_allows_explicit_fail_on_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            create_empty_converted_text_corpus(root)
            active_inode = root.stat().st_ino
            output = base / "quality.json"
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli_main(
                    [
                        "quality",
                        "--data-dir",
                        str(root),
                        "--output",
                        str(output),
                        "--update-metadata",
                        "--fail-on",
                        "none",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertIn(
                "warning: quality metadata update is running without an error gate",
                stderr.getvalue(),
            )
            self.assertNotEqual(root.stat().st_ino, active_inode)
            attachment = load_documents(root)[0].attachments[0]
            self.assertEqual(attachment.quality_status, "fail")
            self.assertFalse(attachment.searchable)
            self.assertEqual(validate_data(root, release_mode=True), [])


def create_failed_attachment_corpus(root: Path) -> Path:
    bundle = "ko/rules/quality-default-gate"
    raw_relative = f"{bundle}/raw/source.txt"
    raw_path = root / raw_relative
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("preserved source", encoding="utf-8")
    doc = Document(
        id="quality-default-gate",
        title="quality default gate",
        source_url="https://example.test/quality-default-gate",
        document_type="rule",
        collected_at="2026-07-21T00:00:00Z",
        body="searchable document body",
        attachments=[
            Attachment(
                id="quality-default-gate-attachment",
                title="failed source",
                file_name="source.txt",
                raw_path=raw_relative,
                status=ATTACHMENT_FAILED,
                preservation_status="preserved",
                searchable=False,
                error="conversion failed",
            )
        ],
    )
    index_path = write_document(root, doc)
    write_sync_manifest(root, [doc], [], "https://example.test")
    if errors := validate_data(root, release_mode=True):
        raise AssertionError(f"invalid test corpus: {errors}")
    return index_path


def create_empty_converted_text_corpus(root: Path) -> None:
    bundle = "ko/rules/quality-empty-converted"
    raw_relative = f"{bundle}/raw/source.txt"
    text_relative = f"{bundle}/attachments/source.md"
    raw_path = root / raw_relative
    text_path = root / text_relative
    raw_path.parent.mkdir(parents=True)
    text_path.parent.mkdir(parents=True)
    raw_path.write_text("non-empty source", encoding="utf-8")
    text_path.write_text("", encoding="utf-8")
    doc = Document(
        id="quality-empty-converted",
        title="quality empty converted",
        source_url="https://example.test/quality-empty-converted",
        document_type="rule",
        collected_at="2026-07-21T00:00:00Z",
        body="searchable document body",
        attachments=[
            Attachment(
                id="quality-empty-converted-attachment",
                title="empty converted text",
                file_name="source.txt",
                raw_path=raw_relative,
                text_path=text_relative,
                status=ATTACHMENT_CONVERTED,
                preservation_status="preserved",
                searchable=True,
            )
        ],
    )
    write_document(root, doc)
    write_sync_manifest(root, [doc], [], "https://example.test")
    if errors := validate_data(root, release_mode=True):
        raise AssertionError(f"invalid test corpus: {errors}")


def snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
