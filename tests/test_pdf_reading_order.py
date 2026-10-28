from pathlib import Path
import unittest
import tempfile

from krx_rule_markdown.converters.pdf import extract_pdf_details, postprocess_pdf_text
from krx_rule_markdown.contracts import CONVERTER_VERSION, canonical_text_hash, converter_version_for_source, sha256_file
from krx_rule_markdown.models import Attachment
from krx_rule_markdown.reconvert import attachment_is_current


class PDFReadingOrderTests(unittest.TestCase):
    def test_rotated_form_headers_remain_readable(self):
        source = Path(__file__).parent / "fixtures/pdf_vertical_headers_86.pdf"
        text, pages = extract_pdf_details(source)
        self.assertEqual(pages, 1)
        for label in ("Postal code (office)", "Telephone no. (mobile)", "Securities savings owned"):
            self.assertIn(label, " ".join(text.split()))

    def test_repeated_article_headings_are_not_running_headers(self):
        heading = "§2. Amendment of Other Regulation"
        text = "\n\n".join(f"Korea Exchange\n{heading}\nAmended provisions {i}." for i in range(3))
        result = postprocess_pdf_text(text)
        self.assertEqual(result.count(heading), 3)
        self.assertNotIn("Korea Exchange", result)

    def test_pdf_algorithm_change_invalidates_only_pdf_cache(self):
        self.assertEqual(converter_version_for_source("source.hwp"), CONVERTER_VERSION)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "source.pdf"
            raw.write_bytes(b"pdf source")
            (root / "text.md").write_text("converted text")
            att = Attachment(id="pdf", title="PDF", status="converted", converter_version=CONVERTER_VERSION,
                raw_file_hash=sha256_file(raw), text_path="text.md", converted_text_hash=canonical_text_hash("converted text"))
            self.assertFalse(attachment_is_current(root, att, raw))
            att.converter_version = f"{CONVERTER_VERSION}+pdf-coordinate-order"
            self.assertFalse(attachment_is_current(root, att, raw))
            att.converter_version = converter_version_for_source(raw)
            self.assertTrue(attachment_is_current(root, att, raw))

    def test_centred_chapter_precedes_owning_article(self):
        path = Path(__file__).resolve().parents[1] / "data/en/rules/guidelines-for-the-management-of-joint-compensation-funds-20250227/raw/english-full-text.pdf"
        if not path.exists():
            self.skipTest("collected JCF PDF unavailable")
        text, pages = extract_pdf_details(path)
        self.assertEqual(pages, 16)
        text = " ".join(text.split())
        chapter = text.index("CHAPTER 3. USE OF JCF")
        article = text.index("§10. Use of JCF")
        body = text.index("(1) In the event that a settlement default", article)
        following = text.index("CHAPTER 4. Supplementary Provisions", article)
        self.assertLess(chapter, article)
        self.assertLess(article, body)
        self.assertLess(body, following)
        self.assertIn("CCP", text[article:following])
