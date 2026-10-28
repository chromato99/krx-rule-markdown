from pathlib import Path
import unittest
from unittest import mock

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTLine

from krx_rule_markdown.converters.pdf_comparison import (
    KNOWN_COMPARISON_PDFS,
    classify_comparison_pdf,
    walk_layout,
)


class PDFComparisonContinuationTests(unittest.TestCase):
    def test_short_final_page_keeps_comparison_columns(self):
        cases = (
            ("330014462", "330014462-330014464-pdf", "two_column", "소액주주", "시장이전기업"),
            ("330021118", "330021118-330021120-pdf", "three_column", "매매전문회원", "제27조제3항"),
        )
        for document_id, attachment_id, template, left, right in cases:
            with self.subTest(id=attachment_id):
                source = Path(__file__).parent / f"fixtures/pdf_comparison_{document_id}.pdf"
                with mock.patch.dict(KNOWN_COMPARISON_PDFS, {attachment_id: template}):
                    result = classify_comparison_pdf(source, attachment_id)
                self.assertEqual(result.status, "restored")
                self.assertEqual(result.table_pages, [3, 4])
                columns = ["".join(row[i] for row in result.pages[-1].rows).replace(" ", "")
                           for i in range(len(result.pages[-1].headers))]
                self.assertIn(left, columns[0])
                self.assertIn(right, columns[1])
                if template == "three_column":
                    self.assertIn("조문정비", columns[2])

    def test_short_grid_without_preceding_header_is_not_classified(self):
        attachment_id = "330014462-330014464-pdf"
        source = Path(__file__).parent / "fixtures/pdf_comparison_330014462.pdf"
        last_page = list(extract_pages(str(source)))[-1]
        with (
            mock.patch.dict(KNOWN_COMPARISON_PDFS, {attachment_id: "two_column"}),
            mock.patch("pdfminer.high_level.extract_pages", return_value=[last_page]),
        ):
            result = classify_comparison_pdf(source, attachment_id)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.table_pages, [])

    def test_short_grid_with_different_column_borders_is_not_joined(self):
        attachment_id = "330014462-330014464-pdf"
        source = Path(__file__).parent / "fixtures/pdf_comparison_330014462.pdf"
        pages = list(extract_pages(str(source)))
        for line in walk_layout(pages[-1]):
            if isinstance(line, LTLine) and abs(line.x1 - line.x0) <= 1:
                x0, y0, x1, y1 = line.bbox
                line.set_bbox((x0 + 12, y0, x1 + 12, y1))
        with (
            mock.patch.dict(KNOWN_COMPARISON_PDFS, {attachment_id: "two_column"}),
            mock.patch("pdfminer.high_level.extract_pages", return_value=pages),
        ):
            result = classify_comparison_pdf(source, attachment_id)
        self.assertEqual(result.table_pages, [3])


if __name__ == "__main__":
    unittest.main()
