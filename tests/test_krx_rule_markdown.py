from __future__ import annotations

from pathlib import Path
import ast
import io
import json
import os
import tempfile
import unittest
import zipfile

from krx_rule_markdown.convert import (
    append_hwp_equations,
    convert_attachment,
    convert_bytes,
    hwp_equation_to_latex,
    infer_extension,
    parse_eqedit_payload,
)
from krx_rule_markdown.collector import (
    extract_state_history_id,
    parse_js_args,
    parse_notice_attachments,
    parse_notice_document,
    parse_recent_items,
    parse_rule_document,
    parse_rule_attachments,
    safe_base,
)
from krx_rule_markdown.clean import clean_unreferenced_attachments, clean_unreferenced_documents, drop_past_rule_attachments
from krx_rule_markdown.converters.hwp import render_hwp_paragraph, render_hwp_table, render_hwp_table_cells
from krx_rule_markdown.converters.pdf import postprocess_pdf_text
from krx_rule_markdown.converters.tables import normalize_angle_bracket_tables, render_html_table
from krx_rule_markdown.html import html_to_markdown
from krx_rule_markdown.markdown import load_documents, parse_markdown, write_document
from krx_rule_markdown.models import ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, Attachment, Document, Item, now_utc
from krx_rule_markdown.paths import converted_attachment_path, raw_attachment_path
from krx_rule_markdown.quality import audit_data_quality, inspect_attachment_quality
from krx_rule_markdown.reconvert import reconvert_data
from krx_rule_markdown.sync import (
    collection_guard_error,
    collect_items,
    includes_english,
    includes_korean,
    english_rule_title,
    normalize_sync_language,
    write_manifest as write_sync_manifest,
)
from krx_rule_markdown.validate import validate_data


class ToolTests(unittest.TestCase):
    def test_markdown_round_trip(self) -> None:
        doc = Document(
            id="rule-1",
            title="코스닥시장 상장규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="제1조 목적\n\n상장 심사",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_document(Path(tmp), doc)
            loaded = load_documents(Path(tmp))
        self.assertEqual(path, Path(tmp) / "ko" / "rules" / "코스닥시장-상장규정" / "index.md")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].title, doc.title)
        self.assertEqual(loaded[0].language, "ko")
        self.assertIn("상장 심사", loaded[0].body)

    def test_markdown_writes_english_rules_under_language_directory(self) -> None:
        doc = Document(
            id="rule-1-en",
            title="KOSPI Market Listing Regulation",
            source_url="https://example.test/rule",
            document_type="rule",
            language="en",
            source_id="rule-1",
            collected_at=now_utc(),
            content_hash="hash-rule-1-en",
            body="Article 1 Purpose",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_document(Path(tmp), doc)
            loaded = load_documents(Path(tmp))
        self.assertEqual(path, Path(tmp) / "en" / "rules" / "kospi-market-listing-regulation" / "index.md")
        self.assertEqual(loaded[0].language, "en")
        self.assertEqual(loaded[0].source_id, "rule-1")

    def test_markdown_avoids_silent_overwrite_on_title_slug_collision(self) -> None:
        first = Document(
            id="rule-1",
            title="동일 제목",
            source_url="https://example.test/rule-1",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="first body",
        )
        second = Document(
            id="rule-2",
            title="동일 제목",
            source_url="https://example.test/rule-2",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-2",
            body="second body",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = write_document(root, first)
            second_path = write_document(root, second)
            loaded = sorted(load_documents(root), key=lambda doc: doc.id)
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(second_path.parent.name.endswith("-rule-2"))
        self.assertEqual([doc.body for doc in loaded], ["first body", "second body"])

    def test_write_document_preserves_existing_bundle_path_for_loaded_document(self) -> None:
        doc = Document(
            id="rule-1-en",
            title="20250922_Membership_Regulation",
            source_url="https://example.test/rule",
            document_type="rule",
            language="en",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="body",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_path = write_document(root, doc)
            loaded = load_documents(root)[0]
            loaded.title = "Membership Regulation"
            rewritten_path = write_document(root, loaded)
            loaded_again = load_documents(root)[0]
        self.assertEqual(rewritten_path, original_path)
        self.assertEqual(loaded_again.title, "Membership Regulation")

    def test_load_documents_ignores_bundle_attachment_markdown(self) -> None:
        raw = """---
id: "rule-1"
title: Bundle Rule
source_url: https://example.test/rule
collected_at: 2026-06-16T14:33:12Z
content_hash: hash
document_type: rule
---

body
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "ko" / "rules" / "bundle-rule"
            (bundle / "attachments").mkdir(parents=True)
            (bundle / "index.md").write_text(raw, encoding="utf-8")
            (bundle / "attachments" / "별표.md").write_text("converted attachment", encoding="utf-8")
            loaded = load_documents(root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, "rule-1")

    def test_parse_existing_frontmatter_shape(self) -> None:
        raw = """---
id: "210207961"
title: 코스닥시장 상장규정
source_url: https://example.test/rule
collected_at: 2026-06-16T14:33:12Z
content_hash: hash
attachments:
  - id: att-1
    title: 전문
    file_name: source.hwp
    status: failed
    error: stored rule attachment not available
document_type: rule
---

본문
"""
        doc = parse_markdown(raw)
        self.assertEqual(doc.id, "210207961")
        self.assertEqual(doc.attachments[0].error, "stored rule attachment not available")

    def test_recent_items_parse_onclick_arguments(self) -> None:
        html = """
<div class="boardA"><strong>최근개정 규정</strong><ul>
<li><p title="코스닥시장 상장규정" onclick="goView('210207961','N')">코스닥시장 상장규정</p><span>2026. 7. 1</span></li>
</ul></div>
<div class="boardA"><strong>규정 제·개정예고</strong><ul>
<li><p title="파생상품시장 업무규정 시행세칙 개정 예고" onclick="goViewpds('210217910','10000016')">예고</p><span>2026. 6. 16.</span></li>
</ul></div>
"""
        items = parse_recent_items(html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].id, "210207961")
        self.assertEqual(items[0].effective_date, "2026-07-01")
        self.assertEqual(items[1].id, "210217910")
        self.assertEqual(items[1].published_date, "2026-06-16")

    def test_sync_language_selection_defaults_to_bilingual(self) -> None:
        self.assertEqual(normalize_sync_language(""), "all")
        self.assertTrue(includes_korean("all"))
        self.assertTrue(includes_english("all"))
        self.assertTrue(includes_korean("ko"))
        self.assertFalse(includes_english("ko"))
        self.assertFalse(includes_korean("en"))
        self.assertTrue(includes_english("en"))

    def test_collect_items_filters_for_english_only(self) -> None:
        client = FakeClient()
        all_items = collect_items(client, limit=0, recent_only=False, language="all")
        english_items = collect_items(client, limit=0, recent_only=False, language="en")
        korean_items = collect_items(client, limit=0, recent_only=False, language="ko")
        self.assertEqual([item.id for item in all_items], ["rule-1", "notice-1"])
        self.assertEqual([item.id for item in english_items], ["rule-1"])
        self.assertEqual([item.id for item in korean_items], ["rule-1", "notice-1"])

    def test_collection_guard_refuses_empty_sync_result(self) -> None:
        error = collection_guard_error(Path("/tmp/nonexistent-krx-data"), [], "all", partial=False)
        self.assertIn("0 items", error)

    def test_collection_guard_refuses_large_full_sync_drop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in range(10):
                write_document(
                    root,
                    Document(
                        id=f"rule-{idx}",
                        title=f"규정 {idx}",
                        source_url=f"https://example.test/rule-{idx}",
                        document_type="rule",
                        language="ko",
                        collected_at=now_utc(),
                        content_hash=f"hash-rule-{idx}",
                        body="본문",
                    ),
                )
            items = [
                Item(
                    id="rule-new",
                    book_id="rule-new",
                    title="새 규정",
                    document_type="rule",
                    noformyn="N",
                )
            ]
            full_error = collection_guard_error(root, items, "all", partial=False)
            partial_error = collection_guard_error(root, items, "all", partial=True)
        self.assertIn("below half", full_error)
        self.assertEqual(partial_error, "")

    def test_partial_sync_manifest_preserves_existing_documents(self) -> None:
        existing = Document(
            id="rule-1",
            title="기존 규정",
            source_url="https://example.test/rule-1",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="existing body",
        )
        updated = Document(
            id="rule-2",
            title="부분 동기화 규정",
            source_url="https://example.test/rule-2",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-2",
            body="updated body",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, existing)
            updated_path = write_document(root, updated)
            updated.path = str(updated_path)
            write_sync_manifest(root, [updated], [], "https://example.test", preserve_existing=True)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({doc["id"] for doc in manifest["documents"]}, {"rule-1", "rule-2"})

    def test_rule_attachments_skip_jun_and_collect_appendix_forms(self) -> None:
        html = """
<script>var statehistoryid = "210203912";</script>
<p class="byulText"><span onclick="downFile('유가증권시장 업무규정 시행세칙'+'_'+'172차'+'_'+'이론가격산출기준.hwp','210032775.hwp','ATTACH');">[별표 1] 이론가격 산출기준</span></p>
<p class="byulText"><span onclick="downFile('유가증권시장 업무규정 시행세칙'+'_'+'172차'+'_'+'신청서.hwp','210032776.hwp','ATTACH');">[별지 제1호 서식] 신청서</span></p>
<p class="byulText"><span onclick="downFile('유가증권시장 업무규정 시행세칙_개정이유.hwp','210032777.hwp','ATTACH');">개정이유</span></p>
<p class="byulText"><span onclick="downFile('유가증권시장 업무규정 시행세칙_신구조문.hwp','210032778.hwp','ATTACH');">신구조문</span></p>
<p class="byulText"><span onclick="downFile('유가증권시장 업무규정 시행세칙_전문.hwp','210032779-jun.hwp','ATTACH');">전문</span></p>
"""
        item = Item(
            id="210203562",
            book_id="210203562",
            title="유가증권시장 업무규정 시행세칙",
            document_type="rule",
            state_history_id="210203912",
        )
        attachments = parse_rule_attachments(html, item)
        self.assertNotIn("전문", {att.title for att in attachments})
        self.assertFalse(any(att.id.endswith("-jun") for att in attachments))
        self.assertNotIn("개정이유", {att.title for att in attachments})
        self.assertNotIn("신구조문", {att.title for att in attachments})
        byul = [att for att in attachments if "이론가격" in att.title][0]
        self.assertEqual(byul.file_name, "유가증권시장 업무규정 시행세칙_172차_이론가격산출기준.hwp")
        self.assertEqual(byul.server_file, "210032775.hwp")
        self.assertEqual(byul.source_url, "/Download.do")

    def test_rule_document_keeps_nested_innerbody_articles(self) -> None:
        html = """
<html>
<p class="title">유가증권시장 상장규정</p>
<p class="jang">시행일 : 2026. 7. 2</p>
<div id="innerbody">
  <div class="article">
    <p><strong>제1조(목적)</strong>첫 번째 조문입니다.</p>
  </div>
  <div class="article">
    <p><strong>제2조(정의)</strong>두 번째 조문입니다.</p>
  </div>
</div>
<div id="footer">닫기</div>
</html>
"""
        item = Item(
            id="210220143",
            book_id="210220143",
            title="유가증권시장 상장규정",
            document_type="rule",
            effective_date="2026-07-02",
        )
        doc = parse_rule_document(html, item, "https://rule.krx.co.kr")
        self.assertIn("제1조", doc.body)
        self.assertIn("제2조", doc.body)
        self.assertIn("두 번째 조문입니다.", doc.body)
        self.assertNotIn("닫기", doc.body)

    def test_extract_state_history_id_for_english_download(self) -> None:
        html = """
obj.put("statehistoryid","210016751");
$(".goRdoc").click(function(){});
"""
        self.assertEqual(extract_state_history_id(html), "210016751")

    def test_notice_attachments_keep_future_amendment_files(self) -> None:
        html = """
<li class="filename" onclick="downFile('(붙임2) 파생상품시장 업무규정 시행세칙 일부개정세칙안.pdf','210217917.pdf','BBS');">일부개정세칙안</li>
<li class="filename" onclick="downFile('(붙임1) 신구조문 대비표.pdf','210217916.pdf','BBS');">신구조문 대비표</li>
"""
        item = Item(id="210217910", title="파생상품시장 업무규정 시행세칙 개정 예고", document_type="notice")
        attachments = parse_notice_attachments(html, item)
        self.assertEqual(len(attachments), 2)
        self.assertIn("신구조문 대비표", {att.title for att in attachments})

    def test_notice_document_uses_recent_list_title_without_close_button_text(self) -> None:
        html = """
<div class="popTT">파생상품시장 업무규정 시행세칙 개정 예고 <button>닫기</button></div>
<table><tr><th>내용</th><td>미래 개정 예고 내용</td></tr></table>
"""
        item = Item(
            id="210217910",
            title="파생상품시장 업무규정 시행세칙 개정 예고",
            document_type="notice",
        )
        doc = parse_notice_document(html, item, "https://rule.krx.co.kr")
        self.assertEqual(doc.title, "파생상품시장 업무규정 시행세칙 개정 예고")

    def test_html_to_markdown_preserves_body_tables_as_markdown_tables(self) -> None:
        html = """
<table>
  <tr><th>적용기간</th><th>매출액</th><th>시가총액</th></tr>
  <tr><td>2027년</td><td>100억원</td><td>500억원</td></tr>
</table>
"""
        markdown = html_to_markdown(html)
        self.assertIn("| 적용기간 | 매출액 | 시가총액 |", markdown)
        self.assertIn("| --- | --- | --- |", markdown)
        self.assertIn("| 2027년 | 100억원 | 500억원 |", markdown)

    def test_html_to_markdown_preserves_br_in_merged_html_table_cells(self) -> None:
        html = """
<table>
  <tr><th rowspan="2">구분<br>항목</th><td>값</td></tr>
  <tr><td>100억원</td></tr>
</table>
"""
        markdown = html_to_markdown(html)
        self.assertIn("구분<br>항목", markdown)
        self.assertNotIn("&lt;br&gt;", markdown)

    def test_rule_document_keeps_content_image_placeholder_but_ignores_ui_icons(self) -> None:
        html = """
<html>
<p class="title">유가증권시장 상장규정</p>
<div id="innerbody">
  <p>상장주식수와 상장시가총액은 다음 표에 따른다.</p>
  <img src="/resources/images/btn_print.gif" alt="print">
  <img src="../../dataFile/law/img/204817634.gif" alt="표">
</div>
</html>
"""
        item = Item(id="210220143", title="유가증권시장 상장규정", document_type="rule")
        doc = parse_rule_document(html, item, "https://rule.krx.co.kr")
        self.assertIn("[이미지: https://rule.krx.co.kr/dataFile/law/img/204817634.gif]", doc.body)
        self.assertNotIn("btn_print", doc.body)
        self.assertNotIn("닫기", doc.title)

    def test_js_args_concatenates_string_literals_per_argument(self) -> None:
        args = parse_js_args("downFile('규정'+'_'+'별표.hwp','server.hwp','ATTACH');")
        self.assertEqual(args, ["규정_별표.hwp", "server.hwp", "ATTACH"])

    def test_safe_base_uses_server_file_extension_when_display_name_has_none(self) -> None:
        self.assertEqual(safe_base("증권시장 청산결제 업무규정 시행세칙_1차_", "210199239.hwp"), "증권시장 청산결제 업무규정 시행세칙_1차_.hwp")

    def test_safe_base_truncates_long_display_names(self) -> None:
        name = "파생상품시장 업무규정 시행세칙_" + ("증거금률" * 80) + ".hwp"
        base = safe_base(name, "210064740.hwp")
        self.assertLessEqual(len(base.encode("utf-8")), 180)
        self.assertTrue(base.endswith(".hwp"))

    def test_converted_attachment_path_uses_attachment_title(self) -> None:
        doc = Document(
            id="210203562",
            title="유가증권시장 업무규정 시행세칙",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
        )
        att = Attachment(
            id="210203562-210032775-hwp",
            title="[별표 1] 이론가격 산출기준",
            file_name="210032775.hwp",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = converted_attachment_path(root, doc, att)
        self.assertEqual(
            path,
            root / "ko/rules/유가증권시장-업무규정-시행세칙/attachments/별표-1-이론가격-산출기준.md",
        )

    def test_raw_attachment_path_uses_attachment_title_and_original_extension(self) -> None:
        doc = Document(
            id="210203562",
            title="유가증권시장 업무규정 시행세칙",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
        )
        att = Attachment(
            id="210203562-210032775-hwp",
            title="[별표 1] 이론가격 산출기준",
            file_name="210032775.hwp",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = raw_attachment_path(root, doc, att)
        self.assertEqual(
            path,
            root / "ko/rules/유가증권시장-업무규정-시행세칙/raw/별표-1-이론가격-산출기준.hwp",
        )

    def test_infer_extension_uses_attachment_id_and_file_signature(self) -> None:
        self.assertEqual(infer_extension(Path("/tmp/att-1-hwp/download"), b""), ".hwp")
        self.assertEqual(infer_extension(Path("/tmp/download"), b"%PDF-1.7"), ".pdf")

    def test_hwpx_conversion_preserves_table_and_formula_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "sample.hwpx"
            raw_path.write_bytes(hwpx_bytes())
            text = convert_bytes(raw_path, raw_path.read_bytes())
            quality = inspect_attachment_quality(text, raw_path)
        self.assertIn("| 구분 | 산식 |", text)
        self.assertIn("| --- | --- |", text)
        self.assertIn("| A | B+1 |", text)
        self.assertIn("수식: A=B+1", text)
        self.assertGreaterEqual(quality.table_row_count, 1)
        self.assertGreaterEqual(quality.formula_hint_count, 1)
        self.assertEqual(quality.formula_block_count, 0)
        self.assertNotIn("raw_table_hints_without_table_text", quality.flags)
        self.assertNotIn("raw_formula_hints_without_formula_text", quality.flags)

    def test_hwpx_conversion_preserves_merged_table_cells_as_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "merged.hwpx"
            raw_path.write_bytes(hwpx_bytes_with_merged_cells())
            text = convert_bytes(raw_path, raw_path.read_bytes())
            quality = inspect_attachment_quality(text, raw_path)
        self.assertIn("<table>", text)
        self.assertIn('<td rowspan="2">구분</td>', text)
        self.assertIn('<td colspan="2">산식</td>', text)
        self.assertIn("<td>가격</td>", text)
        self.assertIn("<td>변동률</td>", text)
        self.assertGreaterEqual(quality.table_row_count, 1)
        self.assertNotIn("raw_table_hints_without_table_text", quality.flags)

    def test_hwp_angle_bracket_tables_are_preserved_as_markdown_tables(self) -> None:
        text = normalize_angle_bracket_tables(
            "\n".join(
                [
                    "가격변동폭은 다음 표에 따른다.",
                    "<구 분><가격변동폭 산출방법>",
                    "<선물거래><선물거래의 기준가격 × 가격변동률>",
                    "<옵션거래><Max(①, ②)>",
                    "비고. 산식은 원문을 확인한다.",
                ]
            )
        )
        self.assertIn("| 구 분 | 가격변동폭 산출방법 |", text)
        self.assertIn("| --- | --- |", text)
        self.assertIn("| 선물거래 | 선물거래의 기준가격 × 가격변동률 |", text)
        self.assertIn("| 옵션거래 | Max(①, ②) |", text)
        self.assertIn("비고. 산식은 원문을 확인한다.", text)

    def test_hwp_angle_bracket_table_normalizer_does_not_rewrite_html_cells(self) -> None:
        text = normalize_angle_bracket_tables(
            "\n".join(
                [
                    "<table>",
                    "  <tr>",
                    '    <td rowspan="3" colspan="5"></td>',
                    "  </tr>",
                    "</table>",
                ]
            )
        )
        self.assertIn('<td rowspan="3" colspan="5"></td>', text)
        self.assertNotIn("| td rowspan=", text)

    def test_hwp_table_cells_preserve_spans_and_line_breaks_as_html(self) -> None:
        text = render_hwp_table_cells(
            [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 2, "text": "구 분"},
                {"row": 0, "col": 2, "rowspan": 1, "colspan": 1, "text": "가격변동률"},
                {"row": 1, "col": 0, "rowspan": 2, "colspan": 1, "text": "선물거래"},
                {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "코스피200<br>미니코스피200"},
                {"row": 1, "col": 2, "rowspan": 1, "colspan": 1, "text": "1.0%"},
            ]
        )
        self.assertIn("<table>", text)
        self.assertIn('<td colspan="2">구 분</td>', text)
        self.assertIn('<td rowspan="2">선물거래</td>', text)
        self.assertIn("<td>코스피200<br>미니코스피200</td>", text)

    def test_hwp_table_cells_preserve_explicit_empty_column_gaps(self) -> None:
        text = render_hwp_table_cells(
            [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "A"},
                {"row": 0, "col": 2, "rowspan": 1, "colspan": 1, "text": "C"},
            ]
        )
        self.assertIn("| A |  | C |", text)

    def test_hwp_table_cells_do_not_duplicate_rowspan_covered_columns(self) -> None:
        text = render_hwp_table_cells(
            [
                {"row": 0, "col": 0, "rowspan": 2, "colspan": 1, "text": "구분"},
                {"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "text": "값"},
                {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "10"},
            ]
        )
        self.assertIn('<td rowspan="2">구분</td>', text)
        self.assertIn("  <tr>\n    <td>10</td>\n  </tr>", text)
        self.assertNotIn("<td></td>", text)

    def test_hwp_table_cells_preserve_blank_input_rows(self) -> None:
        text = render_hwp_table_cells(
            [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "번호"},
                {"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "text": "내용"},
                {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": ""},
                {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": ""},
            ]
        )
        self.assertIn("<table>", text)
        self.assertIn("<td>번호</td>", text)
        self.assertIn("  <tr>\n    <td></td>\n    <td></td>\n  </tr>", text)

    def test_hwp_layout_table_unwraps_nested_table_structure(self) -> None:
        models = [
            {"tagname": "HWPTAG_TABLE", "level": 2, "content": {"rows": 1, "cols": 1, "rowcols": [1]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 2, "content": {"row": 0, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 3, "content": {"chunks": [((0, 3), "신청서")]}},
            {"tagname": "HWPTAG_TABLE", "level": 4, "content": {"rows": 2, "cols": 2, "rowcols": [2, 2]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 0, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 2), "항목")]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 0, "col": 1}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 1), "값")]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 1, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 2), "수량")]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 1, "col": 1}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 2), "10")]}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 3, "content": {"chunks": [((0, 1), "끝")]}},
        ]
        text, next_index, formula_index, used = render_hwp_table(models, 0, [], 0)
        self.assertEqual(next_index, len(models))
        self.assertEqual(formula_index, 0)
        self.assertEqual(used, 0)
        self.assertIn("신청서", text)
        self.assertIn("| 항목 | 값 |", text)
        self.assertIn("| 수량 | 10 |", text)
        self.assertIn("끝", text)
        self.assertFalse(text.lstrip().startswith("<table>"))

    def test_hwp_nested_table_inside_data_cell_is_not_escaped(self) -> None:
        models = [
            {"tagname": "HWPTAG_TABLE", "level": 2, "content": {"rows": 1, "cols": 2, "rowcols": [2]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 2, "content": {"row": 0, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 3, "content": {"chunks": [((0, 2), "요약")]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 2, "content": {"row": 0, "col": 1}},
            {"tagname": "HWPTAG_TABLE", "level": 4, "content": {"rows": 2, "cols": 2, "rowcols": [1, 2]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 0, "col": 0, "colspan": 2}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 2), "상세")]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 1, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 1), "A")]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 1, "col": 1}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": [((0, 1), "B")]}},
        ]
        text, next_index, _, _ = render_hwp_table(models, 0, [], 0)
        self.assertEqual(next_index, len(models))
        self.assertEqual(text.count("<table>"), 2)
        self.assertIn("<td>요약</td>", text)
        self.assertIn('<td colspan="2">상세</td>', text)
        self.assertIn("<td>A</td>", text)
        self.assertIn("<td>B</td>", text)
        self.assertNotIn("&lt;table&gt;", text)

    def test_html_table_normalizes_rows_and_spans_together(self) -> None:
        text = render_html_table(
            [[""], ["A", "B"]],
            [[(9, 9)], [(1, 2), (1, 1)]],
        )
        self.assertNotIn('rowspan="9"', text)
        self.assertIn('<td colspan="2">A</td>', text)
        self.assertIn("<td>B</td>", text)

    def test_convert_attachment_trims_trailing_whitespace_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "sample.txt"
            raw_path.write_text("alpha   \ncontracts" + (" " * 2000) + "\n", encoding="utf-8")
            out_path = root / "sample.md"
            att = Attachment(id="att-1", title="sample", file_name="sample.txt")
            converted = convert_attachment(raw_path, out_path, att)
            output = out_path.read_text(encoding="utf-8")
        self.assertEqual(converted.status, ATTACHMENT_CONVERTED)
        self.assertEqual(output, "alpha\ncontracts\n")

    def test_convert_attachment_does_not_mask_cleanup_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "sample.txt"
            raw_path.write_text("body", encoding="utf-8")
            out_path = root / "sample.md"
            out_path.mkdir()
            att = Attachment(id="att-1", title="sample", file_name="sample.txt")
            converted = convert_attachment(raw_path, out_path, att)
        self.assertEqual(converted.status, ATTACHMENT_FAILED)
        self.assertIn("Is a directory", converted.error)

    def test_hwp_eqedit_payload_decodes_script(self) -> None:
        script = "WC=Min LEFT { sum _{i=1} ^{m} |`k _{i}`| RIGHT }"
        payload = (
            b"\x00\x00\x00\x00"
            + len(script).to_bytes(2, "little")
            + script.encode("utf-16le")
            + (19).to_bytes(2, "little")
            + "Equation Version 60".encode("utf-16le")
            + (7).to_bytes(2, "little")
            + "HYhwpEQ".encode("utf-16le")
        )
        self.assertEqual(parse_eqedit_payload(payload), script)

    def test_hwp_equations_are_appended_as_markdown_blocks(self) -> None:
        text = append_hwp_equations("본문", ["hat{beta _{j}}", "Isum _{i=1}^{m} value"])
        quality = inspect_attachment_quality(text)
        self.assertIn("## HWP 수식", text)
        self.assertIn("HWP EqEdit 원본 수식과 Markdown/RAG 참조용 LaTeX 자동 변환", text)
        self.assertIn("```hwp-equation", text)
        self.assertIn("```math", text)
        self.assertIn("hat{beta _{j}}", text)
        self.assertIn(r"\hat{\beta_{j}}", text)
        self.assertEqual(quality.formula_block_count, 2)
        self.assertGreaterEqual(quality.formula_hint_count, 2)

    def test_quality_formula_hints_ignore_checkbox_dates_and_ids(self) -> None:
        text = "\n".join(
            [
                "신청 구분: √ 신규  √ 변경",
                "연락처: 02-3774-9000",
                "기준일: 2026/07/04",
                "일련번호: 1-1",
                "HTML fragment g<b should not be formula.",
            ]
        )
        quality = inspect_attachment_quality(text)
        self.assertEqual(quality.formula_block_count, 0)
        self.assertEqual(quality.formula_hint_count, 0)

    def test_hwp_paragraph_places_equations_near_placeholders(self) -> None:
        chunks = [
            ((0, 1), " "),
            ((1, 9), {"code": 11, "chid": "eqed", "param": b"\x00" * 8}),
            ((9, 20), " : 충격소멸계수"),
            ((20, 21), {"code": 13}),
        ]
        text, next_index, used = render_hwp_paragraph(chunks, ["lambda _{}^{}"], 0)
        self.assertEqual(next_index, 1)
        self.assertEqual(used, 1)
        self.assertLess(text.index("LaTeX(best-effort)"), text.index("충격소멸계수"))
        self.assertIn("수식 1 원본(HWP EqEdit):", text)
        self.assertIn("```hwp-equation", text)
        self.assertIn("lambda _{}^{}", text)
        self.assertIn(r"\lambda", text)

    def test_hwp_standalone_equation_renders_as_nearby_block(self) -> None:
        chunks = [
            ((0, 8), {"code": 11, "chid": "eqed", "param": b"\x00" * 8}),
            ((8, 9), {"code": 13}),
        ]
        text, next_index, used = render_hwp_paragraph(chunks, ["sigma _{t} = 1"], 0)
        self.assertEqual(next_index, 1)
        self.assertEqual(used, 1)
        self.assertTrue(text.startswith("수식 1 원본(HWP EqEdit):"))
        self.assertIn("```math", text)

    def test_hwp_equation_to_latex_converts_common_eqedit_syntax(self) -> None:
        latex = hwp_equation_to_latex(
            "sum _{i=1} ^{m} 선형화된`증거금 _{i} `/ {dmatrix{sum _{i=1} ^{m} 표준계약수량 _{i}}}"
        )
        self.assertIn(r"\frac", latex)
        self.assertIn(r"\sum_{i = 1}^{m}", latex)
        self.assertIn(r"\text{선형화된 증거금}_{i}", latex)
        self.assertIn(r"\displaystyle \sum_{i = 1}^{m}", latex)
        self.assertIn(r"\text{표준계약수량}_{i}", latex)

    def test_hwp_equation_to_latex_preserves_min_left_right(self) -> None:
        latex = hwp_equation_to_latex("C=Min LEFT { sum _{i=1} ^{m} |`k _{i}`| RIGHT }")
        self.assertIn(r"C = \min \left\{", latex)
        self.assertIn(r"\sum_{i = 1}^{m}", latex)
        self.assertIn(r"| k_{i} |", latex)
        self.assertIn(r"\right\}", latex)

    def test_hwp_equation_to_latex_converts_over_times_and_comparison_words(self) -> None:
        latex = hwp_equation_to_latex("{의무충족일수} over {시장조성일수} GEQ 기간의무이행률")
        self.assertEqual(
            latex,
            r"\frac{\text{의무충족일수}}{\text{시장조성일수}} \ge \text{기간의무이행률}",
        )

    def test_hwp_equation_to_latex_converts_root_and_hwp_text_literals(self) -> None:
        latex = hwp_equation_to_latex(
            'D _{i} = {"Div " _{i}} over {MC} TIMES 100 TIMES (1+f _{i} TIMES {t _{i}} over {365} )'
        )
        self.assertIn(r"D_{i} = \frac{\mathrm{Div}_{i}}{MC}", latex)
        self.assertIn(r"\times 100 \times", latex)
        self.assertIn(r"\frac{f_{i} \times {t_{i}}}{365}", latex)

    def test_hwp_equation_to_latex_uses_roman_for_quoted_english_identifiers(self) -> None:
        self.assertEqual(hwp_equation_to_latex('"Div"_i'), r"\mathrm{Div}_{i}")
        self.assertEqual(hwp_equation_to_latex('"Foo Bar"'), r"\text{Foo Bar}")

    def test_hwp_equation_to_latex_balances_malformed_hwp_groups(self) -> None:
        latex = hwp_equation_to_latex("KOFR_{T-1D")
        self.assertEqual(latex, "KOFR_{T - 1D}")

    def test_hwp_equation_to_latex_ignores_division_inside_parentheses(self) -> None:
        latex = hwp_equation_to_latex("ln(S/X)")
        self.assertEqual(latex, r"\ln(S/X)")
        self.assertNotIn(r"\frac{\ln(S}{X)}", latex)

    def test_hwp_equation_to_latex_drops_trailing_bare_script_operator(self) -> None:
        latex = hwp_equation_to_latex("sigma _{0}^")
        self.assertEqual(latex, r"\sigma_{0}")

    def test_hwp_equation_to_latex_keeps_thousands_commas_tight(self) -> None:
        latex = hwp_equation_to_latex("MC = 1,000")
        self.assertIn("1,000", latex)
        self.assertNotIn("1 , 000", latex)

    def test_pdf_postprocess_removes_toc_dots_headers_and_compresses_blanks(self) -> None:
        text = "\n".join(
            [
                "Korea Exchange Regulation",
                "",
                "",
                "",
                "Chapter 1 General Provisions .....1",
                "Article 1 Purpose",
                "",
                "",
                "",
                "Korea Exchange Regulation",
                "Article 2 Definitions",
                "Korea Exchange Regulation",
            ]
        )
        processed = postprocess_pdf_text(text)
        self.assertNotIn(".....1", processed)
        self.assertNotIn("Korea Exchange Regulation", processed)
        self.assertNotIn("\n\n\n", processed)
        self.assertIn("Article 1 Purpose", processed)

    def test_english_rule_title_removes_date_prefix_and_separators(self) -> None:
        self.assertEqual(english_rule_title("20250922_Membership_Regulation.pdf", "회원관리규정"), "Membership Regulation")
        self.assertEqual(
            english_rule_title("20260417-enforcement-rules-of-securities-market-clearing.pdf", "증권시장 청산결제업무규정 시행세칙"),
            "enforcement rules of securities market clearing",
        )

    def test_quality_audit_updates_attachment_metadata(self) -> None:
        doc = Document(
            id="rule-1",
            title="상장규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="상장 심사",
            attachments=[
                Attachment(
                    id="att-1",
                    title="별표",
                    file_name="sample.hwpx",
                    raw_path="ko/rules/상장규정/raw/sample.hwpx",
                    text_path="ko/rules/상장규정/attachments/att-1.md",
                    status=ATTACHMENT_CONVERTED,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ko" / "rules" / "상장규정" / "raw").mkdir(parents=True)
            (root / "ko" / "rules" / "상장규정" / "raw" / "sample.hwpx").write_bytes(hwpx_bytes())
            (root / "ko" / "rules" / "상장규정" / "attachments").mkdir(parents=True)
            (root / "ko" / "rules" / "상장규정" / "attachments" / "att-1.md").write_text(
                "| 구분 | 산식 |\n| A | B+1 |\n수식: A=B+1\n상장 규정 별표의 산식과 표를 보존한 변환 결과입니다.\n",
                encoding="utf-8",
            )
            write_document(root, doc)
            report = audit_data_quality(root, update_metadata=True)
            loaded = load_documents(root)[0]
            index_path = root / "ko" / "rules" / "상장규정" / "index.md"
            manifest_path = root / "manifest.json"
            fixed_time_ns = 1_700_000_000_000_000_000
            os.utime(index_path, ns=(fixed_time_ns, fixed_time_ns))
            os.utime(manifest_path, ns=(fixed_time_ns, fixed_time_ns))
            audit_data_quality(root, update_metadata=True)
            index_mtime_ns = index_path.stat().st_mtime_ns
            manifest_mtime_ns = manifest_path.stat().st_mtime_ns
        self.assertEqual(report["summary"]["quality_status"]["ok"], 1)
        self.assertEqual(loaded.attachments[0].quality_status, "ok")
        self.assertGreaterEqual(loaded.attachments[0].table_row_count, 1)
        self.assertEqual(loaded.attachments[0].formula_block_count, 0)
        self.assertEqual(index_mtime_ns, fixed_time_ns)
        self.assertEqual(manifest_mtime_ns, fixed_time_ns)

    def test_reconvert_rebuilds_attachment_markdown_from_existing_raw_file(self) -> None:
        doc = Document(
            id="rule-1",
            title="샘플 규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="본문",
            attachments=[
                Attachment(
                    id="att-1",
                    title="별표 산식",
                    file_name="formula.txt",
                    raw_path="ko/rules/샘플-규정/raw/formula.txt",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, doc)
            raw_path = root / "ko/rules/샘플-규정/raw/formula.txt"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("표와 수식", encoding="utf-8")
            result = reconvert_data(root)
            loaded = load_documents(root)[0]
            text_path = root / loaded.attachments[0].text_path
            converted_text = text_path.read_text(encoding="utf-8").strip()
        self.assertEqual(result.converted, 1)
        self.assertEqual(loaded.attachments[0].status, ATTACHMENT_CONVERTED)
        self.assertTrue(loaded.attachments[0].text_path.endswith(".md"))
        self.assertEqual(converted_text, "표와 수식")

    def test_reconvert_refreshes_document_body_from_converted_text_path(self) -> None:
        doc = Document(
            id="rule-1-en",
            title="Sample Rule",
            source_url="https://example.test/rule",
            document_type="rule",
            language="en",
            collected_at=now_utc(),
            content_hash="old-hash",
            body="old body",
            text_path="en/rules/sample-rule/attachments/english-full-text.md",
            attachments=[
                Attachment(
                    id="att-1",
                    title="English full text",
                    file_name="english.txt",
                    raw_path="en/rules/sample-rule/raw/english.txt",
                    text_path="en/rules/sample-rule/attachments/english-full-text.md",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, doc)
            raw_path = root / "en/rules/sample-rule/raw/english.txt"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("fresh body   \ncontracts" + (" " * 2000), encoding="utf-8")
            result = reconvert_data(root)
            loaded = load_documents(root)[0]
        self.assertEqual(result.converted, 1)
        self.assertEqual(loaded.body, "fresh body\ncontracts")
        self.assertNotEqual(loaded.content_hash, "old-hash")

    def test_reconvert_rebuilds_document_level_markdown_from_existing_raw_file(self) -> None:
        doc = Document(
            id="rule-1-en",
            title="Sample Rule",
            source_url="https://example.test/rule",
            document_type="rule",
            language="en",
            collected_at=now_utc(),
            content_hash="old-hash",
            body="old body",
            raw_path="en/rules/sample-rule/raw/english.txt",
            text_path="en/rules/sample-rule/attachments/english-full-text.md",
            file_name="english.txt",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, doc)
            raw_path = root / "en/rules/sample-rule/raw/english.txt"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("fresh body   \ncontracts" + (" " * 2000), encoding="utf-8")
            result = reconvert_data(root)
            loaded = load_documents(root)[0]
            converted_text = (root / loaded.text_path).read_text(encoding="utf-8")
        self.assertEqual(result.converted, 1)
        self.assertEqual(loaded.body, "fresh body\ncontracts")
        self.assertEqual(converted_text, "fresh body\ncontracts\n")
        self.assertNotEqual(loaded.content_hash, "old-hash")

    def test_reconvert_normalizes_existing_english_document_title_without_moving_bundle(self) -> None:
        doc = Document(
            id="rule-1-en",
            title="20250922_Membership_Regulation",
            source_url="https://example.test/rule",
            document_type="rule",
            language="en",
            collected_at=now_utc(),
            content_hash="old-hash",
            body="old body",
            raw_path="en/rules/20250922-membership-regulation/raw/20250922-membership-regulation.txt",
            text_path="en/rules/20250922-membership-regulation/attachments/english-full-text.md",
            file_name="20250922_Membership_Regulation.pdf",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_document(root, doc)
            raw_path = root / doc.raw_path
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("fresh body", encoding="utf-8")
            result = reconvert_data(root)
            loaded = load_documents(root)[0]
        self.assertEqual(result.converted, 1)
        self.assertEqual(loaded.title, "Membership Regulation")
        self.assertEqual(Path(loaded.path), path)

    def test_clean_removes_unreferenced_attachment_files(self) -> None:
        doc = Document(
            id="rule-1",
            title="상장규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="상장 심사",
            attachments=[
                Attachment(
                    id="att-1",
                    title="별표",
                    file_name="keep.hwp",
                    raw_path="ko/rules/상장규정/raw/keep.hwp",
                    text_path="ko/rules/상장규정/attachments/att-1.md",
                    status=ATTACHMENT_CONVERTED,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ko" / "rules" / "상장규정" / "raw").mkdir(parents=True)
            (root / "ko" / "rules" / "상장규정" / "attachments").mkdir(parents=True)
            keep = root / "ko" / "rules" / "상장규정" / "raw" / "keep.hwp"
            old = root / "ko" / "rules" / "상장규정" / "raw" / "old.hwp"
            keep.write_bytes(b"keep")
            old.write_bytes(b"old")
            converted = root / "ko" / "rules" / "상장규정" / "attachments" / "att-1.md"
            converted.write_text("converted", encoding="utf-8")
            write_document(root, doc)
            result = clean_unreferenced_attachments(root)
            self.assertTrue(keep.exists())
            self.assertTrue(converted.exists())
            self.assertFalse(old.exists())
        self.assertEqual(result.removed, 1)

    def test_clean_removes_unreferenced_document_bundles(self) -> None:
        current = Document(
            id="rule-1",
            title="새 규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at="2026-07-03T00:00:00Z",
            content_hash="new",
            body="new body",
        )
        stale = Document(
            id="rule-1",
            title="옛 규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at="2026-01-01T00:00:00Z",
            content_hash="old",
            body="old body",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_path = write_document(root, current)
            stale_path = write_document(root, stale)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": [
                            current.to_mapping() | {"path": str(current_path.relative_to(root))}
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = clean_unreferenced_documents(root)
            loaded = load_documents(root)
        self.assertEqual(result.removed, 1)
        self.assertFalse(stale_path.exists())
        self.assertEqual([doc.id for doc in loaded], ["rule-1"])
        self.assertEqual(loaded[0].title, "새 규정")

    def test_clean_refuses_to_prune_when_manifest_looks_truncated(self) -> None:
        docs = [
            Document(
                id=f"rule-{idx}",
                title=f"규정 {idx}",
                source_url=f"https://example.test/rule-{idx}",
                document_type="rule",
                collected_at=now_utc(),
                content_hash=f"hash-rule-{idx}",
                body="body",
            )
            for idx in range(4)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [write_document(root, doc) for doc in docs]
            (root / "manifest.json").write_text(
                json.dumps(
                    {"documents": [docs[0].to_mapping() | {"path": str(paths[0].relative_to(root))}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "far fewer documents"):
                clean_unreferenced_documents(root)

    def test_validate_detects_manifest_document_count_mismatch(self) -> None:
        docs = [
            Document(
                id=f"rule-{idx}",
                title=f"검증 규정 {idx}",
                source_url=f"https://example.test/rule-{idx}",
                document_type="rule",
                collected_at=now_utc(),
                content_hash=f"hash-rule-{idx}",
                body="body",
            )
            for idx in range(2)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [write_document(root, doc) for doc in docs]
            (root / "manifest.json").write_text(
                json.dumps(
                    {"documents": [docs[0].to_mapping() | {"path": str(paths[0].relative_to(root))}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            errors = validate_data(root)
        self.assertTrue(any("manifest document count" in error for error in errors))
        self.assertTrue(any("missing document path" in error for error in errors))

    def test_clean_drops_past_rule_attachment_metadata_but_keeps_notice_attachments(self) -> None:
        rule_doc = Document(
            id="rule-1",
            title="상장규정",
            source_url="https://example.test/rule",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="상장 심사",
            attachments=[
                Attachment(id="rule-1-jun", title="전문", file_name="jun.hwp"),
                Attachment(id="rule-1-rea", title="개정이유", file_name="rea.hwp"),
                Attachment(id="rule-1-sin", title="신구조문", file_name="sin.hwp"),
                Attachment(id="rule-1-byl", title="[별표 1] 산식", file_name="byl.hwp"),
            ],
        )
        notice_doc = Document(
            id="notice-1",
            title="상장규정 개정 예고",
            source_url="https://example.test/notice",
            document_type="notice",
            collected_at=now_utc(),
            content_hash="hash-notice-1",
            body="미래 개정 예고",
            attachments=[Attachment(id="notice-1-sin", title="신구조문 대비표", file_name="future.pdf")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, rule_doc)
            write_document(root, notice_doc)
            result = drop_past_rule_attachments(root)
            loaded = {doc.id: doc for doc in load_documents(root)}
        self.assertEqual(result.removed, 3)
        self.assertEqual([att.id for att in loaded["rule-1"].attachments], ["rule-1-byl"])
        self.assertEqual([att.id for att in loaded["notice-1"].attachments], ["notice-1-sin"])

    def test_python_tool_does_not_call_external_commands(self) -> None:
        banned = {"subprocess", "Popen", "system", "spawn", "execv", "execl"}
        for path in Path("krx_rule_markdown").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], banned, str(path))
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[0], banned, str(path))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned, str(path))
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned, str(path))


def hwpx_bytes() -> bytes:
    buf = io.BytesIO()
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<hp:body xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hp:p><hp:run><hp:t>본문 문단</hp:t></hp:run></hp:p>
  <hp:tbl>
    <hp:tr>
      <hp:tc><hp:p><hp:run><hp:t>구분</hp:t></hp:run></hp:p></hp:tc>
      <hp:tc><hp:p><hp:run><hp:t>산식</hp:t></hp:run></hp:p></hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc><hp:p><hp:run><hp:t>A</hp:t></hp:run></hp:p></hp:tc>
      <hp:tc><hp:p><hp:run><hp:t>B+1</hp:t></hp:run></hp:p></hp:tc>
    </hp:tr>
  </hp:tbl>
  <hp:equation script="A=B+1" />
</hp:body>
"""
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Contents/section0.xml", xml)
    return buf.getvalue()


def hwpx_bytes_with_merged_cells() -> bytes:
    buf = io.BytesIO()
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<hp:body xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hp:tbl>
    <hp:tr>
      <hp:tc rowSpan="2"><hp:p><hp:run><hp:t>구분</hp:t></hp:run></hp:p></hp:tc>
      <hp:tc colSpan="2"><hp:p><hp:run><hp:t>산식</hp:t></hp:run></hp:p></hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc><hp:p><hp:run><hp:t>가격</hp:t></hp:run></hp:p></hp:tc>
      <hp:tc><hp:p><hp:run><hp:t>변동률</hp:t></hp:run></hp:p></hp:tc>
    </hp:tr>
  </hp:tbl>
</hp:body>
"""
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Contents/section0.xml", xml)
    return buf.getvalue()


class FakeClient:
    def current_rule_items(self, limit: int) -> list[Item]:
        return [Item(id="rule-1", title="규정", document_type="rule")]

    def recent_items(self) -> list[Item]:
        return [Item(id="notice-1", title="예고", document_type="notice")]


if __name__ == "__main__":
    unittest.main()
