from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from krx_rule_markdown.assets import preserve_inline_document_assets
from krx_rule_markdown.collector import parse_rule_document
from krx_rule_markdown.markdown import load_documents, write_document
from krx_rule_markdown.models import Item
from krx_rule_markdown.quality import audit_data_quality
from krx_rule_markdown.reconvert import reconvert_data
from krx_rule_markdown.sync import write_manifest, write_source_provenance
from krx_rule_markdown.validate import validate_data


BASE_URL = "https://rule.krx.co.kr"
IMAGE_URL = BASE_URL + "/dataFile/law/img/formula.gif"
GIF = b"GIF89a\x02\x00\x03\x00"


class ReconvertInlineAssetTests(unittest.TestCase):
    def write_release(self, root: Path, image_src: str, *, preserve: bool = True):
        document = parse_rule_document(
            '<div id="innerbody"><p>제1조 수식은 다음과 같다.</p>'
            f'<img src="{image_src}"><p>제2조 적용 범위</p></div>',
            Item(id="inline-rule", title="이미지 규정", document_type="rule"),
            BASE_URL,
        )
        document.source_request = {
            "endpoint": "/out/regulation/regulationViewPop.do",
            "bookid": document.id,
            "noformyn": "N",
            "source_content_hash": document.source_content_hash,
        }
        if preserve:
            def download(url):
                self.assertEqual(url, IMAGE_URL)
                return GIF, "image/gif"

            preserve_inline_document_assets(root, document, download)
        write_source_provenance(root, document)
        write_document(root, document)
        write_manifest(root, [document], [], BASE_URL)
        return document

    def test_reconvert_preserves_inline_assets_and_is_repeatable(self):
        for image_src in ("../../dataFile/law/img/formula.gif", "/dataFile/law/img/formula.gif", IMAGE_URL):
            with self.subTest(src=image_src), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "data"
                original = self.write_release(root, image_src)
                expected_body = original.body

                result = reconvert_data(root)
                converted = load_documents(root)[0]
                self.assertFalse(result.has_blocking_failures)
                self.assertEqual(converted.body, expected_body)
                self.assertNotIn("inline_image_missing", converted.quality_codes)
                self.assertEqual((root / converted.assets[0].path).read_bytes(), GIF)
                self.assertEqual(validate_data(root, release_mode=True), [])
                report = audit_data_quality(root, release_gate=True)
                self.assertNotIn("inline_image_missing", {issue["code"] for issue in report["issues"]})

                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                reconvert_data(root)
                after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                self.assertEqual(after, before)

    def test_reconvert_keeps_unpreserved_image_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            self.write_release(root, "../../dataFile/law/img/formula.gif", preserve=False)
            reconvert_data(root)
            converted = load_documents(root)[0]
            self.assertIn(f"[이미지: {IMAGE_URL}]", converted.body)
            self.assertIn("inline_image_missing", converted.quality_codes)
            self.assertNotIn("krx-asset:", converted.body)

    def test_reconvert_repairs_legacy_relative_marker_using_preserved_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            document = self.write_release(root, "../../dataFile/law/img/formula.gif")
            expected_body = document.body
            document.body = (
                "제1조 수식은 다음과 같다.\n\n"
                "[이미지: ../../dataFile/law/img/formula.gif]\n\n제2조 적용 범위"
            )
            document.quality_codes.append("inline_image_missing")
            write_document(root, document)
            write_manifest(root, [document], [], BASE_URL)

            reconvert_data(root)
            repaired = load_documents(root)[0]
            self.assertEqual(repaired.body, expected_body)
            self.assertNotIn("inline_image_missing", repaired.quality_codes)
            self.assertEqual((root / repaired.assets[0].path).read_bytes(), GIF)


if __name__ == "__main__":
    unittest.main()
