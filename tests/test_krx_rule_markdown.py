from __future__ import annotations

from pathlib import Path
import ast
import base64
import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib import error as urlerror, request as urlrequest

from krx_rule_markdown.convert import (
    append_hwp_equations,
    convert_attachment,
    convert_bytes,
    hwp_equation_to_latex,
    infer_extension,
    parse_eqedit_payload,
)
from krx_rule_markdown.collector import (
    SameHostRedirectHandler,
    extract_state_history_id,
    parse_js_args,
    parse_notice_attachments,
    parse_notice_document,
    parse_recent_items,
    parse_rule_document,
    parse_rule_attachments,
    safe_base,
    sanitize_source_html,
    validate_download,
)
from krx_rule_markdown.assets import (
    MAX_ASSET_BYTES,
    inspect_image,
    preserve_inline_document_assets,
    read_hwp_image_streams,
)
from krx_rule_markdown.asset_migration import migrate_assets
from krx_rule_markdown.contracts import (
    CONVERTER_VERSION,
    canonical_json_bytes,
    canonical_text,
    canonical_text_hash,
    effective_searchable,
    index_source_hash,
    index_source_payload,
    release_hash,
    sha256_bytes,
    status_combination_errors,
)
from krx_rule_markdown.clean import clean_unreferenced_attachments, clean_unreferenced_documents, drop_past_rule_attachments
from krx_rule_markdown.converters.base import ConversionError
from krx_rule_markdown.converters.cache import SourceInspectionCache
from krx_rule_markdown.converters.core import convert_bytes_outcome
from krx_rule_markdown.converters.hwp import extract_hwp_with_diagnostics, render_hwp_paragraph, render_hwp_table, render_hwp_table_cells
from krx_rule_markdown.converters.hwpx import extract_hwpx
from krx_rule_markdown.converters.inspection import inspect_converted_source
from krx_rule_markdown.converters.pdf import postprocess_pdf_text
from krx_rule_markdown.converters.pdf_comparison import (
    KNOWN_COMPARISON_PDFS,
    ComparisonClassification,
    classify_comparison_pdf,
    render_comparison_page,
)
from krx_rule_markdown.pdf_migration import migrate_pdf_comparisons
from krx_rule_markdown.converters.tables import normalize_angle_bracket_tables, render_html_table, render_markdown_table
from krx_rule_markdown.html import html_to_markdown
from krx_rule_markdown.markdown import load_documents, parse_markdown, write_document
from krx_rule_markdown.models import ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, Asset, Attachment, Document, Item, hash_text, now_utc
from krx_rule_markdown.paths import (
    MAX_RAW_NAME_BYTES,
    converted_attachment_path,
    raw_attachment_path,
    truncate_name,
    unique_name,
)
from krx_rule_markdown.quality import audit_data_quality, inspect_attachment_quality
from krx_rule_markdown.reconvert import reconvert_data
from krx_rule_markdown.repository import (
    CorpusMutationError,
    WriterLock,
    WriterLockError,
    atomic_exchange_paths,
    mutate_staged_corpus,
)
from krx_rule_markdown.sync import (
    SyncRunner,
    collection_guard_error,
    collect_items,
    includes_english,
    includes_korean,
    english_rule_title,
    normalize_sync_language,
    sync_rules,
    write_manifest as write_sync_manifest,
)
from krx_rule_markdown.validate import validate_asset, validate_data


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

    def test_unique_name_reserves_suffix_space_after_max_length_collision(self) -> None:
        name = truncate_name(("파생상품시장" * 40) + ".hwp", MAX_RAW_NAME_BYTES)
        used = {name}

        second = unique_name(name, used, max_bytes=MAX_RAW_NAME_BYTES)
        third = unique_name(name, used, max_bytes=MAX_RAW_NAME_BYTES)

        self.assertNotEqual(second, name)
        self.assertTrue(second.endswith("-2.hwp"))
        self.assertTrue(third.endswith("-3.hwp"))
        self.assertLessEqual(len(second.encode("utf-8")), MAX_RAW_NAME_BYTES)
        self.assertLessEqual(len(third.encode("utf-8")), MAX_RAW_NAME_BYTES)

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

    def test_markdown_table_cells_preserve_latex_backslashes(self) -> None:
        text = render_markdown_table(
            [
                ["구분", "산식"],
                ["증거금", r"\(S_{0} + \frac{A}{B}\)"],
                ["빈칸표시", "\\"],
            ]
        )
        self.assertIn(r"| 증거금 | \(S_{0} + \frac{A}{B}\) |", text)
        self.assertIn(r"| 빈칸표시 | \\ |", text)
        self.assertNotIn(r"\\(S_{0}", text)

    def test_markdown_table_cells_protect_pipe_delimiters_without_double_escaping(self) -> None:
        text = render_markdown_table(
            [
                ["구분", "값"],
                ["일반", "A|B"],
                ["이미 보호됨", r"A\|B"],
            ]
        )
        self.assertIn(r"| 일반 | A\|B |", text)
        self.assertIn(r"| 이미 보호됨 | A\|B |", text)
        self.assertNotIn(r"A\\|B", text)

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

    def test_hwp_table_cells_render_script_tags_inside_html_tables(self) -> None:
        text = render_hwp_table_cells(
            [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 2, "text": "구분"},
                {"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "text": "S<sub>0</sub>"},
                {"row": 1, "col": 1, "rowspan": 1, "colspan": 1, "text": "명칭<sup>주)</sup>"},
            ]
        )
        self.assertIn("<td>S<sub>0</sub></td>", text)
        self.assertIn("<td>명칭<sup>주)</sup></td>", text)
        self.assertNotIn("&lt;sub&gt;", text)
        self.assertNotIn("&lt;sup&gt;", text)

    def test_hwp_html_table_cells_keep_unrelated_html_escaped(self) -> None:
        text = render_hwp_table_cells(
            [
                {
                    "row": 0,
                    "col": 0,
                    "rowspan": 1,
                    "colspan": 2,
                    "text": 'literal <sub> marker <sup class="note">1</sup> <script>x</script>',
                },
            ]
        )
        self.assertIn("&lt;sub&gt; marker", text)
        self.assertIn("&lt;sup class=&quot;note&quot;&gt;1&lt;/sup&gt;", text)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", text)

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

    def test_hwp_layout_table_places_direct_formula_block_before_nested_table(self) -> None:
        equation_chunks = [
            ((0, 8), {"code": 11, "chid": "eqed", "param": b"\x00" * 8}),
            ((8, 9), {"code": 13}),
        ]
        models = [
            {"tagname": "HWPTAG_TABLE", "level": 2, "content": {"rows": 1, "cols": 1, "rowcols": [1]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 2, "content": {"row": 0, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 3, "content": {"chunks": equation_chunks}},
            {"tagname": "HWPTAG_TABLE", "level": 4, "content": {"rows": 1, "cols": 1, "rowcols": [1]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 4, "content": {"row": 0, "col": 0}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 5, "content": {"chunks": equation_chunks}},
        ]

        text, next_index, formula_index, used = render_hwp_table(
            models,
            0,
            ["alpha _{1}", "beta _{2}"],
            0,
        )

        direct_paragraph = r"[수식 1 LaTeX(best-effort): \(\alpha_{1}\)]"
        direct_formula_block = "\n".join(
            [
                "수식 1 원본(HWP EqEdit):",
                "```hwp-equation",
                "alpha _{1}",
                "```",
                "",
                "수식 1 LaTeX(best-effort):",
                "```math",
                r"\alpha_{1}",
                "```",
            ]
        )
        nested_formula = r"[수식 2 LaTeX(best-effort): \(\beta_{2}\)]"
        self.assertEqual(next_index, len(models))
        self.assertEqual(formula_index, 2)
        self.assertEqual(used, 2)
        self.assertEqual(text.count("수식 1 원본(HWP EqEdit):"), 1)
        self.assertEqual(text.count("수식 2 원본(HWP EqEdit):"), 1)
        self.assertIn(f"{direct_paragraph}\n\n{direct_formula_block}", text)
        self.assertLess(
            text.index(direct_formula_block) + len(direct_formula_block),
            text.index(nested_formula),
        )

    def test_hwp_data_table_keeps_direct_formula_blocks_outside_cells(self) -> None:
        equation_chunk = ((1, 9), {"code": 11, "chid": "eqed", "param": b"\x00" * 8})
        models = [
            {"tagname": "HWPTAG_TABLE", "level": 2, "content": {"rows": 1, "cols": 2, "rowcols": [2]}},
            {"tagname": "HWPTAG_LIST_HEADER", "level": 2, "content": {"row": 0, "col": 0}},
            {
                "tagname": "HWPTAG_PARA_TEXT",
                "level": 3,
                "content": {"chunks": [((0, 1), "값 "), equation_chunk]},
            },
            {"tagname": "HWPTAG_LIST_HEADER", "level": 2, "content": {"row": 0, "col": 1}},
            {"tagname": "HWPTAG_PARA_TEXT", "level": 3, "content": {"chunks": [((0, 1), "설명")]}},
        ]

        text, next_index, formula_index, used = render_hwp_table(models, 0, ["alpha _{1}"], 0)

        table_end = "| --- | --- |"
        formula_block = "수식 1 원본(HWP EqEdit):"
        self.assertEqual(next_index, len(models))
        self.assertEqual(formula_index, 1)
        self.assertEqual(used, 1)
        self.assertLess(text.index(table_end) + len(table_end), text.index(formula_block))
        self.assertNotIn("```hwp-equation", text[: text.index(table_end)])

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

    def test_hwp_paragraph_preserves_script_char_shapes(self) -> None:
        char_shapes = [
            {"charshapeflags": 0},
            {"charshapeflags": 0x10000},
            {"charshapeflags": 0x8000},
        ]

        text, next_index, used = render_hwp_paragraph(
            [((0, 2), "S0"), ((2, 3), {"code": 13})],
            [],
            0,
            [(0, 0), (1, 1)],
            char_shapes,
        )
        self.assertEqual(text, "S<sub>0</sub>")
        self.assertEqual(next_index, 0)
        self.assertEqual(used, 0)

        text, _, _ = render_hwp_paragraph(
            [((0, 9), "f(S-15,H)")],
            [],
            0,
            [(0, 0), (3, 1), (6, 0)],
            char_shapes,
        )
        self.assertEqual(text, "f(S<sub>-15</sub>,H)")

        text, _, _ = render_hwp_paragraph(
            [((0, 8), "명칭주1) :")],
            [],
            0,
            [(0, 0), (2, 2), (5, 0)],
            char_shapes,
        )
        self.assertEqual(text, "명칭<sup>주1)</sup> :")

        text, _, _ = render_hwp_paragraph(
            [((0, 6), "사업연도주)")],
            [],
            0,
            [(0, 0), (4, 2), (5, 3)],
            char_shapes + [{"charshapeflags": 0x8000}],
        )
        self.assertEqual(text, "사업연도<sup>주)</sup>")

        text, _, _ = render_hwp_paragraph(
            [((0, 8), "컨설팅 방식3)")],
            [],
            0,
            [(0, 0), (7, 2)],
            char_shapes,
        )
        self.assertEqual(text, "컨설팅 방식<sup>3)</sup>")

    def test_hwp_equation_to_latex_converts_common_eqedit_syntax(self) -> None:
        latex = hwp_equation_to_latex(
            "sum _{i=1} ^{m} 선형화된`증거금 _{i} `/ {dmatrix{sum _{i=1} ^{m} 표준계약수량 _{i}}}"
        )
        self.assertIn(r"\frac", latex)
        self.assertIn(r"\sum_{i = 1}^{m}", latex)
        self.assertIn(r"\text{선형화된 증거금}_{i}", latex)
        self.assertIn(r"\left\lvert \sum_{i = 1}^{m}", latex)
        self.assertIn(r"\right\rvert", latex)
        self.assertNotIn(r"\\left", latex)
        self.assertNotIn(r"\\right", latex)
        self.assertIn(r"\text{표준계약수량}_{i}", latex)

    def test_hwp_equation_to_latex_converts_dmatrix_to_vertical_bars(self) -> None:
        latex = hwp_equation_to_latex("dmatrix{a&b#c&d}")
        self.assertEqual(latex, r"\begin{vmatrix}a & b \\ c & d\end{vmatrix}")

    def test_hwp_equation_to_latex_uses_lvert_for_left_right_pipe(self) -> None:
        latex = hwp_equation_to_latex("LEFT | A _{i} -B _{i} RIGHT |")
        self.assertEqual(latex, r"\left\lvert A_{i} - B_{i} \right\rvert")

    def test_hwp_equation_to_latex_converts_absolute_value_bars(self) -> None:
        latex = hwp_equation_to_latex("C=Min LEFT { sum _{i=1} ^{m} |`k _{i}`| RIGHT }")
        self.assertIn(r"C = \min \left\{", latex)
        self.assertIn(r"\sum_{i = 1}^{m}", latex)
        self.assertIn(r"\lvert k_{i} \rvert", latex)
        self.assertNotIn(r"| k_{i} |", latex)
        self.assertIn(r"\right\}", latex)

    def test_hwp_equation_to_latex_keeps_relation_like_pipes(self) -> None:
        latex = hwp_equation_to_latex("A | B | C")
        self.assertEqual(latex, "A | B | C")

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
            schema_version=1,
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
        self.assertEqual(loaded.schema_version, 2)
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
            raw_path="en/rules/sample-rule/raw/english.txt",
            text_path="en/rules/sample-rule/attachments/english-full-text.md",
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
        bundle = "ko/rules/상장규정"
        source_path = f"{bundle}/raw/source.html"
        request_path = f"{bundle}/raw/request.json"
        source = "<p>official source</p>"
        source_hash = canonical_text_hash(source)
        doc = Document(
            id="rule-1",
            title="상장규정",
            source_url="https://rule.krx.co.kr/out/regulation/regulationViewPop.do",
            document_type="rule",
            collected_at=now_utc(),
            content_hash="hash-rule-1",
            body="상장 심사",
            source_content_hash=source_hash,
            source_content_path=source_path,
            source_request_path=request_path,
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
            source_file = root / source_path
            source_file.write_text(source, encoding="utf-8")
            request_file = root / request_path
            request_file.write_text(
                json.dumps(
                    {
                        "endpoint": "/out/regulation/regulationViewPop.do",
                        "bookid": doc.id,
                        "noformyn": "N",
                        "source_content_hash": source_hash,
                    }
                ),
                encoding="utf-8",
            )
            converted = root / "ko" / "rules" / "상장규정" / "attachments" / "att-1.md"
            converted.write_text("converted", encoding="utf-8")
            write_document(root, doc)
            write_sync_manifest(root, [doc], [], "https://example.test")
            result = clean_unreferenced_attachments(root)
            self.assertTrue(keep.exists())
            self.assertTrue(converted.exists())
            self.assertTrue(source_file.exists())
            self.assertTrue(request_file.exists())
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
            write_sync_manifest(root, [current], [], "https://example.test")
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


class ContractAndSafetyRegressionTests(unittest.TestCase):
    def test_document_searchability_uses_body_without_document_text_path(self) -> None:
        doc = Document(
            id="body-only",
            title="body only",
            source_url="https://example.test/body-only",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="searchable Korean body",
            conversion_status="converted",
        )
        self.assertTrue(effective_searchable(doc))
        self.assertTrue(doc.to_mapping()["searchable"])

    def test_shared_contract_fixture_matches_canonical_hashes(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "corpus_contract_v2.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        source = fixture["documents"][0]
        doc = Document.from_mapping(source, source["body"])
        expected = fixture["expected"]
        asset_bytes = base64.b64decode(fixture["asset_fixture"]["file_base64"])

        self.assertEqual(canonical_text(doc.body), expected["canonical_body"])
        self.assertEqual(canonical_text_hash(doc.body), expected["body_hash"])
        self.assertEqual(hash_text(doc.title + "\n" + doc.body), expected["legacy_content_hash"])
        payload = index_source_payload([doc])
        self.assertEqual(canonical_json_bytes(payload).decode("utf-8"), expected["index_source_canonical_json"])
        self.assertEqual(index_source_hash([doc]), expected["index_source_hash"])
        self.assertEqual(fixture["negative_cases"][0]["expected_error"], "required_source_missing")
        self.assertEqual(doc.assets[0].to_mapping(), source["assets"][0])
        self.assertEqual(len(asset_bytes), source["assets"][0]["size"])
        self.assertEqual(sha256_bytes(asset_bytes), source["assets"][0]["raw_file_hash"])
        image = inspect_image(asset_bytes)
        self.assertEqual((image.mime_type, image.width, image.height), ("image/png", 2, 3))
        self.assertTrue(status_combination_errors(conversion_status="failed", searchable=True))
        self.assertEqual(
            release_hash({"value": "same", "generated_at": "first", "source_response_hash": "a"}),
            release_hash({"value": "same", "generated_at": "second", "source_response_hash": "b"}),
        )

    def test_shared_asset_contract_negative_cases(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "corpus_contract_v2.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        base_mapping = fixture["documents"][0]["assets"][0]
        asset_bytes = base64.b64decode(fixture["asset_fixture"]["file_base64"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "ko/rules/caf-규정"
            asset_path = root / base_mapping["path"]
            asset_path.parent.mkdir(parents=True)
            asset_path.write_bytes(asset_bytes)
            for case in fixture["asset_fixture"]["negative_cases"]:
                with self.subTest(case=case["name"]):
                    mapping = copy.deepcopy(base_mapping)
                    for key in case.get("remove", []):
                        mapping.pop(key, None)
                    mapping.update(case.get("overrides", {}))
                    errors = "\n".join(
                        validate_asset(
                            root,
                            bundle,
                            Asset.from_mapping(mapping),
                            {},
                            {},
                            "fixture asset",
                            "https://example.test/rule-1",
                        )
                    )
                    self.assertIn(case["expected_error"], errors)

            oversized = copy.deepcopy(base_mapping)
            oversized["path"] = "ko/rules/caf-규정/assets/inline/oversized.gif"
            oversized["size"] = MAX_ASSET_BYTES + 1
            oversized_path = root / oversized["path"]
            with oversized_path.open("wb") as file:
                file.truncate(MAX_ASSET_BYTES + 1)
            errors = "\n".join(
                validate_asset(
                    root,
                    bundle,
                    Asset.from_mapping(oversized),
                    {},
                    {},
                    "oversized asset",
                    "https://example.test/rule-1",
                )
            )
            self.assertIn("exceeds", errors)

    def test_inline_asset_migration_is_strict_idempotent_and_prunes_stale_files(self) -> None:
        gif = b"GIF89a\x02\x00\x03\x00"
        url_a = "https://rule.krx.co.kr/dataFile/law/img/a.gif"
        url_b = "https://rule.krx.co.kr/dataFile/law/img/b.gif"

        class InlineClient:
            def __init__(self, _base_url: str) -> None:
                pass

            def download_inline_asset(self, source_url: str) -> tuple[bytes, str]:
                if source_url not in {url_a, url_b}:
                    raise RuntimeError("unexpected inline URL")
                return gif, "image/gif"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = Document(
                id="inline-assets",
                title="inline assets",
                source_url="https://rule.krx.co.kr/out/regulation/regulationViewPop.do",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body=f"[이미지: {url_a}]\n\n[이미지: {url_b}]",
                converter_version=CONVERTER_VERSION,
            )
            write_document(root, doc)
            write_sync_manifest(root, [doc], [], "https://rule.krx.co.kr")
            with mock.patch("krx_rule_markdown.asset_migration.Client", InlineClient):
                first = migrate_assets(root, download_inline=True)
                first_snapshot = snapshot_files(root)
                second = migrate_assets(root, download_inline=True)
            self.assertEqual((first.preserved_assets, first.failed_assets), (2, 0))
            self.assertEqual(second.preserved_assets, 2)
            self.assertEqual(snapshot_files(root), first_snapshot)

            migrated = load_documents(root)[0]
            self.assertNotIn("/assets/", migrated.body)
            self.assertEqual(migrated.body.count("krx-asset:"), 2)
            self.assertTrue(all((root / asset.path).is_file() for asset in migrated.assets))
            stale_path = root / next(asset.path for asset in migrated.assets if asset.source_url == url_b)

            migrated.body = f"[이미지: {url_a}]"
            write_document(root, migrated)
            write_sync_manifest(root, [migrated], [], "https://rule.krx.co.kr")
            with mock.patch("krx_rule_markdown.asset_migration.Client", InlineClient):
                shrunk = migrate_assets(root, download_inline=True)
            self.assertEqual(shrunk.preserved_assets, 1)
            self.assertEqual(shrunk.pruned_assets, 1)
            self.assertFalse(stale_path.exists())

            migrated = load_documents(root)[0]
            failed_url = "https://rule.krx.co.kr/dataFile/law/img/fail.gif"
            migrated.body = f"[이미지: {failed_url}]"
            write_document(root, migrated)
            write_sync_manifest(root, [migrated], [], "https://rule.krx.co.kr")
            before_failure = snapshot_files(root)

            class FailingClient(InlineClient):
                def download_inline_asset(self, source_url: str) -> tuple[bytes, str]:
                    raise RuntimeError("network failed")

            with (
                mock.patch("krx_rule_markdown.asset_migration.Client", FailingClient),
                self.assertRaisesRegex(CorpusMutationError, "inline asset"),
            ):
                migrate_assets(root, download_inline=True)
            self.assertEqual(snapshot_files(root), before_failure)

    def test_actual_hwp_images_and_operation_cache_are_reused(self) -> None:
        project = Path(__file__).resolve().parents[1]
        fixtures = {
            project
            / "data/ko/rules/krx금시장-운영규정-시행세칙/raw/별표-1-거래소-및-품질인증기관의-상징-표식개정-2018-1-15.hwp": [
                ("image/bmp", 812838, 471, 574),
                ("image/jpeg", 37339, 539, 613),
                ("image/jpeg", 615979, 720, 764),
                ("image/jpeg", 135415, 714, 713),
            ],
            project
            / "data/ko/rules/전문가회의-및-기술평가제도-운영지침/raw/서식-2-기술평가신청서.hwp": [
                ("image/jpeg", 415459, 1400, 834)
            ],
        }
        for path, expected in fixtures.items():
            if not path.is_file():
                self.skipTest(f"corpus HWP fixture is missing: {path}")
            streams = read_hwp_image_streams(path)
            actual = [
                (item.image.mime_type, len(item.data), item.image.width, item.image.height)
                for item in streams
            ]
            self.assertEqual(actual, expected)

        cache = SourceInspectionCache()
        source = next(iter(fixtures))
        text, _ = extract_hwp_with_diagnostics(source, cache)
        inspect_attachment_quality(text, source, cache)
        inspect_converted_source(source, text, inspection_cache=cache)
        inspect_converted_source(source, text, inspection_cache=cache)
        self.assertEqual(cache.hwp_model_parse_count, 1)
        self.assertEqual(cache.hwp_ole_parse_count, 1)

    def test_current_named_pdf_comparisons_match_coordinate_goldens(self) -> None:
        project = Path(__file__).resolve().parents[1]
        attachments = {
            att.id: project / "data" / att.raw_path
            for doc in load_documents(project / "data")
            for att in doc.attachments
            if att.id in KNOWN_COMPARISON_PDFS
        }
        expected = {
            "210219879-210219880-pdf": (14, 419),
            "210224393-210224395-pdf": (11, 308),
            "210222057-210222059-pdf": (8, 222),
            "210221769-210221771-pdf": (4, 88),
            "210220231-210220236-pdf": (1, 22),
            "210219622-210219624-pdf": (11, 407),
            "210224396-210224398-pdf": (8, 214),
        }
        # The classification catalog intentionally includes historical notices,
        # while a full sync materializes only the current KRX listing.  Validate
        # every catalog entry that is present without requiring removed notices
        # to remain in the release.
        self.assertTrue(attachments)
        self.assertLessEqual(set(attachments), set(KNOWN_COMPARISON_PDFS))
        classifications = {
            attachment_id: classify_comparison_pdf(attachments[attachment_id], attachment_id)
            for attachment_id in sorted(attachments)
        }
        for attachment_id in sorted(attachments):
            with self.subTest(attachment_id=attachment_id):
                table_page_count, row_count = expected[attachment_id]
                classification = classifications[attachment_id]
                self.assertEqual(classification.status, "restored")
                self.assertEqual(len(classification.table_pages), table_page_count)
                self.assertEqual(classification.row_count, row_count)
                self.assertEqual(classification.confidence, 1.0)

        golden = json.loads(
            (Path(__file__).parent / "fixtures/pdf_comparison_210220231_golden.json").read_text(
                encoding="utf-8"
            )
        )
        golden_attachment_id = golden["attachment_id"]
        if golden_attachment_id in classifications:
            classification = classifications[golden_attachment_id]
            self.assertEqual(classification.table_pages, golden["table_pages"])
            self.assertEqual(classification.pages[0].rows[:3], golden["first_rows"])

        degradation_fixture_id = (
            golden_attachment_id
            if golden_attachment_id in attachments
            else sorted(attachments)[0]
        )

        with (
            mock.patch("pdfminer.high_level.extract_pages", return_value=[mock.Mock()]),
            mock.patch(
                "krx_rule_markdown.converters.pdf_comparison.comparison_boundaries",
                return_value=[],
            ),
        ):
            degraded = classify_comparison_pdf(
                attachments[degradation_fixture_id], degradation_fixture_id
            )
        self.assertEqual(degraded.status, "degraded")

    def test_pdf_comparison_apply_failure_keeps_active_generation(self) -> None:
        ids = ["210220231-210220236-pdf", "210221769-210221771-pdf"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = "ko/notices/atomic-comparison"
            attachments = []
            for index, attachment_id in enumerate(ids, start=1):
                raw_path = f"{bundle}/raw/{index}.pdf"
                text_path = f"{bundle}/attachments/{index}.md"
                (root / raw_path).parent.mkdir(parents=True, exist_ok=True)
                (root / raw_path).write_bytes(b"%PDF-1.4\nfixture")
                (root / text_path).parent.mkdir(parents=True, exist_ok=True)
                (root / text_path).write_text("last known good", encoding="utf-8")
                attachments.append(
                    Attachment(
                        id=attachment_id,
                        title=attachment_id,
                        file_name=f"{index}.pdf",
                        raw_path=raw_path,
                        text_path=text_path,
                        status=ATTACHMENT_CONVERTED,
                        preservation_status="preserved",
                        searchable=True,
                        converter_version=CONVERTER_VERSION,
                    )
                )
            doc = Document(
                id="atomic-comparison",
                title="atomic comparison",
                source_url="https://rule.krx.co.kr/out/pds/pdsViewPop.do",
                document_type="notice",
                collected_at="2026-07-01T00:00:00Z",
                body="notice body",
                attachments=attachments,
            )
            write_document(root, doc)
            write_sync_manifest(root, [doc], [], "https://rule.krx.co.kr")
            before = snapshot_files(root)
            active_inode = root.stat().st_ino

            def classify(_path: Path, attachment_id: str) -> ComparisonClassification:
                return ComparisonClassification(
                    attachment_id,
                    KNOWN_COMPARISON_PDFS[attachment_id],
                    "restored",
                )

            def convert(_raw: Path, text_path: Path, attachment: Attachment) -> Attachment:
                if attachment.id == ids[0]:
                    text_path.write_text("partially migrated", encoding="utf-8")
                    return attachment
                attachment.status = ATTACHMENT_FAILED
                attachment.text_path = ""
                return attachment

            with (
                mock.patch("krx_rule_markdown.pdf_migration.classify_comparison_pdf", side_effect=classify),
                mock.patch("krx_rule_markdown.pdf_migration.convert_attachment", side_effect=convert),
                self.assertRaisesRegex(CorpusMutationError, "PDF comparison migration aborted"),
            ):
                migrate_pdf_comparisons(root, apply=True)
            self.assertEqual(root.stat().st_ino, active_inode)
            self.assertEqual(snapshot_files(root), before)

    def test_reconvert_current_corpus_is_byte_identical_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = "ko/rules/reconvert-noop"
            raw_path = f"{bundle}/raw/source.txt"
            text_path = f"{bundle}/attachments/source.md"
            (root / raw_path).parent.mkdir(parents=True)
            (root / raw_path).write_text("stable source", encoding="utf-8")
            (root / text_path).parent.mkdir(parents=True)
            (root / text_path).write_text("stable source\n", encoding="utf-8")
            doc = Document(
                id="reconvert-noop",
                title="reconvert noop",
                source_url="https://rule.krx.co.kr/out/regulation/regulationViewPop.do",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body="body",
                attachments=[
                    Attachment(
                        id="reconvert-noop-att",
                        title="source",
                        file_name="source.txt",
                        raw_path=raw_path,
                        text_path=text_path,
                        status=ATTACHMENT_CONVERTED,
                        preservation_status="preserved",
                        searchable=True,
                        converter_version=CONVERTER_VERSION,
                    )
                ],
            )
            write_document(root, doc)
            write_sync_manifest(root, [doc], [], "https://rule.krx.co.kr")
            before = snapshot_files(root)
            first = reconvert_data(root)
            middle = snapshot_files(root)
            second = reconvert_data(root)
            self.assertEqual((first.converted, first.skipped), (0, 1))
            self.assertEqual((second.converted, second.skipped), (0, 1))
            self.assertEqual(middle, before)
            self.assertEqual(snapshot_files(root), before)

    def test_public_metadata_and_source_request_are_strict_and_bounded(self) -> None:
        invalid_urls = (
            "file:///etc/passwd",
            "/home/user/private",
            "https://user:password@rule.krx.co.kr/rule",
            "https://rule.krx.co.kr/%0aheader",
        )
        for source_url in invalid_urls:
            with self.subTest(source_url=source_url), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_document(
                    root,
                    Document(
                        id="unsafe-url",
                        title="unsafe url",
                        source_url=source_url,
                        document_type="rule",
                        collected_at="2026-07-01T00:00:00Z",
                        body="body",
                    ),
                )
                self.assertIn("source_url must be absolute HTTP(S)", "\n".join(validate_data(root)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = "ko/rules/source-request"
            source_path = f"{bundle}/raw/source.html"
            request_path = f"{bundle}/raw/request.json"
            source = "<p>official source</p>"
            source_hash = canonical_text_hash(source)
            (root / source_path).parent.mkdir(parents=True)
            (root / source_path).write_text(source, encoding="utf-8")
            doc = Document(
                id="source-request",
                title="source request",
                source_url="https://rule.krx.co.kr/out/regulation/regulationViewPop.do",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body="body",
                source_content_hash=source_hash,
                source_content_path=source_path,
                source_request_path=request_path,
            )
            write_document(root, doc)
            valid_prefix = (
                '{"endpoint":"/out/regulation/regulationViewPop.do",'
                '"bookid":"source-request","noformyn":"N",'
            )
            deeply_nested: object = []
            for _ in range(66):
                deeply_nested = [deeply_nested]
            cases = {
                "duplicate field": (
                    '{"endpoint":"/out/regulation/regulationViewPop.do",'
                    '"endpoint":"/out/regulation/regulationViewPop.do",'
                    '"bookid":"source-request","noformyn":"N",'
                    f'"source_content_hash":"{source_hash}"}}'
                ),
                "unknown field": valid_prefix
                + f'"source_content_hash":"{source_hash}","headers":"secret"}}',
                "does not match document": valid_prefix
                + f'"source_content_hash":"{"0" * 64}"}}',
                "nesting exceeds 64": json.dumps({"endpoint": deeply_nested}),
            }
            for expected, request in cases.items():
                with self.subTest(request_error=expected):
                    (root / request_path).write_text(request, encoding="utf-8")
                    self.assertIn(expected, "\n".join(validate_data(root)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "ko/rules/oversized/index.md"
            index.parent.mkdir(parents=True)
            with index.open("wb") as file:
                file.truncate(64 * 1024 * 1024 + 1)
            self.assertIn("file exceeds", "\n".join(validate_data(root)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(
                root,
                Document(
                    id="oversized-manifest",
                    title="oversized manifest",
                    source_url="https://rule.krx.co.kr/out/regulation/regulationViewPop.do",
                    document_type="rule",
                    collected_at="2026-07-01T00:00:00Z",
                    body="body",
                ),
            )
            with (root / "manifest.json").open("wb") as file:
                file.truncate(64 * 1024 * 1024 + 1)
            self.assertIn("file exceeds", "\n".join(validate_data(root, release_mode=True)))

    def test_html_preserves_inline_boundaries_and_excludes_active_content(self) -> None:
        html = """
        <p>제1조 <span>목적</span> 및 <b>범위</b><br>다음 문장</p>
        <script>SEARCH_POISON</script><style>.hidden { content: 'POISON'; }</style>
        """
        converted = html_to_markdown(html)
        self.assertIn("제1조 목적 및 **범위**\n다음 문장", converted)
        self.assertNotIn("SEARCH_POISON", converted)
        self.assertNotIn("POISON", converted)

        sanitized = sanitize_source_html(
            '<meta name="_csrf" content="secret"><input name="_csrf" value="secret">'
            '<script>secret</script><p>보존 본문</p>'
        )
        self.assertEqual(sanitized, "<p>보존 본문</p>")

    def test_writer_lock_and_atomic_generation_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            with WriterLock(data_dir, "first"):
                with self.assertRaises(WriterLockError):
                    with WriterLock(data_dir, "second"):
                        pass
            lock_path = root / ".data.writer.lock"
            lock_path.write_text(
                json.dumps({"pid": 999999, "host": "terminated-other-host", "operation": "old"}),
                encoding="utf-8",
            )
            with WriterLock(data_dir, "after-crash"):
                pass

            left = root / "left"
            right = root / "right"
            left.mkdir()
            right.mkdir()
            (left / "generation").write_text("old", encoding="utf-8")
            (right / "generation").write_text("new", encoding="utf-8")
            atomic_exchange_paths(left, right)
            self.assertEqual((left / "generation").read_text(encoding="utf-8"), "new")
            self.assertEqual((right / "generation").read_text(encoding="utf-8"), "old")

    def test_staging_rejects_external_file_and_directory_symlinks_before_mutation(self) -> None:
        for link_kind in ("file", "directory"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                data_dir = root / "data"
                data_dir.mkdir()
                sentinel = data_dir / "sentinel"
                sentinel.write_text("active generation", encoding="utf-8")
                if link_kind == "file":
                    external = root / "external-secret.txt"
                    external.write_text("external secret", encoding="utf-8")
                    link = data_dir / "escaped-file"
                else:
                    external = root / "external-directory"
                    external.mkdir()
                    (external / "secret.txt").write_text("external secret", encoding="utf-8")
                    link = data_dir / "escaped-directory"
                link.symlink_to(external, target_is_directory=link_kind == "directory")
                active_inode = data_dir.stat().st_ino
                callback_called = False

                def mutate(staging: Path) -> None:
                    nonlocal callback_called
                    callback_called = True
                    (staging / "sentinel").write_text("mutated", encoding="utf-8")

                with self.assertRaisesRegex(CorpusMutationError, "contains a symlink"):
                    mutate_staged_corpus(data_dir, "symlink-test", mutate)

                self.assertFalse(callback_called)
                self.assertEqual(data_dir.stat().st_ino, active_inode)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "active generation")
                self.assertTrue(link.is_symlink())
                external_secret = external if link_kind == "file" else external / "secret.txt"
                self.assertEqual(external_secret.read_text(encoding="utf-8"), "external secret")

    def test_staged_mutation_validation_failure_keeps_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "sentinel").write_text("old", encoding="utf-8")

            def mutate(staging: Path) -> str:
                (staging / "sentinel").write_text("new", encoding="utf-8")
                return "mutated"

            with mock.patch("krx_rule_markdown.validate.validate_data", return_value=["injected"]):
                with self.assertRaises(CorpusMutationError):
                    mutate_staged_corpus(data_dir, "test", mutate)
            self.assertEqual((data_dir / "sentinel").read_text(encoding="utf-8"), "old")

    def test_conversion_failure_keeps_last_known_good_bytes_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "broken.pdf"
            out_path = root / "converted.md"
            raw_path.write_bytes(b"not a pdf")
            original = b"last known good\n"
            out_path.write_bytes(original)
            attachment = Attachment(
                id="att-lkg",
                title="LKG",
                file_name="broken.pdf",
                text_path=str(out_path),
                status=ATTACHMENT_CONVERTED,
                converter_version=CONVERTER_VERSION,
            )

            result = convert_attachment(raw_path, out_path, attachment)

            self.assertEqual(out_path.read_bytes(), original)
            self.assertEqual(result.status, ATTACHMENT_CONVERTED)
            self.assertTrue(result.last_refresh_error)
            self.assertTrue(result.last_refresh_failed_at)
            self.assertIn("stale_due_to_refresh_failure", result.quality_codes)

    def test_strict_validation_rejects_status_hash_symlink_and_global_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_relative = "ko/rules/strict/raw/source.bin"
            text_relative = "ko/rules/strict/attachments/source.md"
            link_relative = "ko/rules/strict/raw/escape.bin"
            doc = Document(
                id="duplicate-id",
                title="strict",
                source_url="https://example.test/strict",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body="strict body",
                content_hash=hash_text("strict\nstrict body"),
                attachments=[
                    Attachment(
                        id="duplicate-attachment",
                        title="bad hashes",
                        file_name="source.bin",
                        raw_path=raw_relative,
                        text_path=text_relative,
                        raw_file_hash="0" * 64,
                        converted_text_hash="0" * 64,
                        status="unknown-status",
                    ),
                    Attachment(
                        id="escape-attachment",
                        title="symlink",
                        file_name="escape.bin",
                        raw_path=link_relative,
                        raw_file_hash="0" * 64,
                        status=ATTACHMENT_FAILED,
                    ),
                ],
            )
            write_document(root, doc)
            (root / raw_relative).parent.mkdir(parents=True, exist_ok=True)
            (root / raw_relative).write_bytes(b"actual raw")
            (root / text_relative).parent.mkdir(parents=True, exist_ok=True)
            (root / text_relative).write_text("actual converted", encoding="utf-8")
            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            (root / link_relative).symlink_to(outside)

            english = Document(
                id="duplicate-id",
                title="strict english",
                source_url="https://example.test/strict-en",
                document_type="rule",
                language="en",
                collected_at="2026-07-01T00:00:00Z",
                body="strict english body",
                content_hash=hash_text("strict english\nstrict english body"),
                attachments=[Attachment(id="duplicate-attachment", status=ATTACHMENT_FAILED)],
            )
            write_document(root, english)

            errors = "\n".join(validate_data(root))
            self.assertIn("duplicate_document_id", errors)
            self.assertIn("duplicate_attachment_id", errors)
            self.assertIn("invalid conversion_status", errors)
            self.assertIn("raw_file_hash_mismatch", errors)
            self.assertIn("converted_text_hash_mismatch", errors)
            self.assertIn("symlink paths are forbidden", errors)
            self.assertIn("path_outside_data_root", errors)

    def test_strict_validation_rejects_invalid_utf8_converted_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_relative = "ko/rules/utf-8/raw/source.bin"
            text_relative = "ko/rules/utf-8/attachments/source.md"
            doc = Document(
                id="utf-8",
                title="utf 8",
                source_url="https://example.test/utf-8",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body="body",
                content_hash=hash_text("utf 8\nbody"),
                attachments=[
                    Attachment(
                        id="utf-8-att",
                        raw_path=raw_relative,
                        text_path=text_relative,
                        raw_file_hash=hash_text("raw"),
                        converted_text_hash="0" * 64,
                        status=ATTACHMENT_CONVERTED,
                    )
                ],
            )
            write_document(root, doc)
            (root / raw_relative).parent.mkdir(parents=True, exist_ok=True)
            (root / raw_relative).write_bytes(b"raw")
            (root / text_relative).parent.mkdir(parents=True, exist_ok=True)
            (root / text_relative).write_bytes(b"valid prefix\xffinvalid")
            errors = "\n".join(validate_data(root))
        self.assertIn("invalid UTF-8", errors)

    def test_document_and_attachment_ids_share_one_global_namespace(self) -> None:
        doc = Document(
            id="same-id",
            title="global id",
            source_url="https://example.test/global-id",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="body",
            attachments=[Attachment(id="same-id", status=ATTACHMENT_FAILED)],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, doc)
            errors = "\n".join(validate_data(root))
        self.assertIn("duplicate_attachment_id same-id", errors)

    def test_release_quality_gate_requires_explicit_failure_allowlist(self) -> None:
        doc = Document(
            id="rule-failed",
            title="failed attachment rule",
            source_url="https://example.test/failed",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="body",
            content_hash=hash_text("failed attachment rule\nbody"),
            attachments=[
                Attachment(
                    id="known-deletion",
                    status=ATTACHMENT_FAILED,
                    error="deleted form",
                    raw_path="ko/rules/failed-attachment-rule/raw/deleted.bin",
                    preservation_status="preserved",
                    searchable=False,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "ko/rules/failed-attachment-rule/raw/deleted.bin"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"preserved raw")
            write_document(root, doc)
            blocked = audit_data_quality(root, release_gate=True)
            allowed = audit_data_quality(root, release_gate=True, allowed_failure_ids={"known-deletion"})
        self.assertTrue(
            any(item["severity"] == "error" and item["code"] == "required_conversion_failed" for item in blocked["issues"])
        )
        self.assertFalse(any(item["severity"] == "error" for item in allowed["issues"]))

    def test_quality_update_gate_does_not_publish_failed_release(self) -> None:
        doc = Document(
            id="quality-gate",
            title="quality gate",
            source_url="https://example.test/quality-gate",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="body",
            attachments=[
                Attachment(
                    id="failed-new-attachment",
                    status=ATTACHMENT_FAILED,
                    searchable=False,
                    error="conversion failed",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = write_document(root, doc)
            write_sync_manifest(root, [doc], [], "https://example.test")
            original_index = index.read_bytes()
            original_manifest = (root / "manifest.json").read_bytes()
            with self.assertRaises(CorpusMutationError):
                audit_data_quality(
                    root,
                    update_metadata=True,
                    release_gate=True,
                    fail_on="error",
                )
            self.assertEqual(index.read_bytes(), original_index)
            self.assertEqual((root / "manifest.json").read_bytes(), original_manifest)

    def test_hwpx_zip_bomb_ratio_is_rejected(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Contents/section0.xml", "<body>" + ("A" * 1_000_000) + "</body>")
        with self.assertRaisesRegex(ConversionError, "compression ratio"):
            extract_hwpx(buf.getvalue())

    def test_sparse_comparison_and_unresolved_image_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "amendment.pdf"
            pdf_path.write_bytes(b"%PDF-placeholder")
            with mock.patch(
                "krx_rule_markdown.converters.inspection.count_pdf_pages",
                return_value=(7, ""),
            ):
                sparse, searchable = inspect_converted_source(pdf_path, "이미지 PDF")
                comparison, comparison_searchable = inspect_converted_source(
                    pdf_path,
                    ("현행 조문 개정안 조문 " * 100),
                )
            self.assertFalse(searchable)
            self.assertIn("pdf_text_layer_too_sparse", {item.code for item in sparse})
            self.assertTrue(comparison_searchable)
            self.assertIn("pdf_comparison_structure_lost", {item.code for item in comparison})

            hwpx_path = root / "picture.hwpx"
            with zipfile.ZipFile(hwpx_path, "w") as archive:
                archive.writestr("BinData/image.png", b"\x89PNG\r\n\x1a\n")
            image_diagnostics, _ = inspect_converted_source(hwpx_path, "searchable text")
            self.assertIn("hwp_picture_missing", {item.code for item in image_diagnostics})

            html_outcome = convert_bytes_outcome(
                Path("source.html"),
                b'<p>body</p><img src="/dataFile/law/img/important.png">',
            )
            self.assertIn("inline_image_missing", html_outcome.quality_codes)
            self.assertIn("image_content_unindexed", html_outcome.quality_codes)

    def test_hwp_fallback_reason_is_preserved_as_diagnostic(self) -> None:
        with (
            mock.patch.dict("sys.modules", {"hwp5": mock.Mock()}),
            mock.patch(
                "krx_rule_markdown.converters.hwp.extract_hwp_layout",
                side_effect=RuntimeError("layout failed"),
            ),
            mock.patch("krx_rule_markdown.converters.hwp.runpy.run_module", side_effect=SystemExit(0)),
            mock.patch("krx_rule_markdown.converters.hwp.extract_hwp_preview", return_value="preview fallback"),
            mock.patch(
                "krx_rule_markdown.converters.hwp.extract_hwp_equations_with_error",
                return_value=([], ""),
            ),
        ):
            text, diagnostics = extract_hwp_with_diagnostics(Path("fixture.hwp"))
        self.assertEqual(text, "preview fallback")
        self.assertIn("source_inspection_failed", {item.code for item in diagnostics})
        self.assertTrue(any("primary layout extraction failed" in item.message for item in diagnostics))

    def test_formula_source_mismatch_is_integrity_error_and_missing_math_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_relative = "ko/rules/formula/raw/formula.hwp"
            text_relative = "ko/rules/formula/attachments/formula.md"
            raw_path = root / raw_relative
            text_path = root / text_relative
            raw_path.parent.mkdir(parents=True)
            text_path.parent.mkdir(parents=True)
            raw_path.write_bytes(b"fake hwp fixture")
            text_path.write_text("plain converted text", encoding="utf-8")
            doc = Document(
                id="formula",
                title="formula",
                source_url="https://example.test/formula",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body="body",
                attachments=[
                    Attachment(
                        id="formula-att",
                        raw_path=raw_relative,
                        text_path=text_relative,
                        status=ATTACHMENT_CONVERTED,
                    )
                ],
            )
            write_document(root, doc)
            with mock.patch(
                "krx_rule_markdown.quality.hwp_structure_counts",
                return_value=(0, 0, 1, ""),
            ):
                errors = "\n".join(validate_data(root))
                quality = inspect_attachment_quality(
                    "```hwp-equation\nA=B\n```",
                    raw_path,
                )
        self.assertIn("formula_source_count_mismatch", errors)
        self.assertIn("formula_generated_latex_invalid", quality.flags)

    def test_redirect_escape_and_signature_mismatch_are_rejected(self) -> None:
        handler = SameHostRedirectHandler("rule.krx.co.kr", "https")
        request = urlrequest.Request("https://rule.krx.co.kr/source")
        with self.assertRaises(urlerror.HTTPError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://evil.example/escape",
            )
        with self.assertRaisesRegex(RuntimeError, "not a PDF"):
            validate_download(Attachment(id="pdf", file_name="rule.pdf"), b"<not-pdf>")

    def test_sync_run_report_is_failed_when_staged_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "sentinel").write_text("old release", encoding="utf-8")
            with (
                mock.patch.object(SyncRunner, "run", return_value=0),
                mock.patch("krx_rule_markdown.sync.validate_data", return_value=["injected staged error"]),
            ):
                result = sync_rules(
                    data_dir=data_dir,
                    base_url="https://rule.krx.co.kr",
                    limit=0,
                    recent_only=False,
                    rule_id="",
                    download_attachments=False,
                    language="all",
                )
            report = json.loads((root / ".krx-rule-runs" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(report["result"], "failed")
            self.assertEqual((data_dir / "sentinel").read_text(encoding="utf-8"), "old release")

    def test_english_file_not_found_keeps_lkg_and_records_run_failure(self) -> None:
        class MissingEnglishClient:
            def download_rule_file(self, item, filecd, title):
                raise FileNotFoundError("official English file disappeared")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = Document(
                id="rule-1-en",
                title="Previous English",
                source_url="https://example.test/rule-1",
                document_type="rule",
                language="en",
                source_id="rule-1",
                collected_at="2026-06-01T00:00:00Z",
                body="last known good English body",
            )
            write_document(root, previous)
            korean = Document(
                id="rule-1",
                title="한국어 규정",
                source_url="https://example.test/rule-1",
                document_type="rule",
                collected_at="2026-07-01T00:00:00Z",
                body="한국어 본문",
            )
            runner = SyncRunner(
                data_dir=root,
                base_url="https://rule.krx.co.kr",
                limit=0,
                recent_only=False,
                rule_id="",
                download_attachments=False,
                language="all",
            )
            runner.client = MissingEnglishClient()
            runner.existing_docs[("en", "rule", "rule-1-en")] = previous
            runner.write_english_document(
                Item(id="rule-1", book_id="rule-1", title="한국어 규정", document_type="rule"),
                korean,
            )
        self.assertEqual(runner.manifest_docs[0].body, "last known good English body")
        self.assertIn("stale_due_to_refresh_failure", runner.manifest_docs[0].quality_codes)
        self.assertEqual(runner.run_provenance[-1]["outcome"], "stale")
        self.assertTrue(runner.run_provenance[-1]["failed_at"])
        self.assertIn("disappeared", runner.run_provenance[-1]["error"])

    def test_sync_optional_failure_requires_named_id_and_preserved_raw(self) -> None:
        class BrokenAttachmentClient:
            def download_attachment(self, attachment):
                return attachment, b"not a real PDF"

        attachment = Attachment(
            id="optional-pdf",
            title="optional",
            file_name="optional.pdf",
            server_file="optional.pdf",
            source_url="/Download.do",
        )
        document = Document(
            id="rule-optional",
            title="optional rule",
            source_url="https://example.test/optional",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="body",
            attachments=[attachment],
        )
        with tempfile.TemporaryDirectory() as tmp:
            allowed_runner = SyncRunner(
                data_dir=Path(tmp),
                base_url="https://rule.krx.co.kr",
                limit=0,
                recent_only=False,
                rule_id="",
                download_attachments=True,
                language="ko",
                allowed_failure_ids={"optional-pdf"},
            )
            allowed_runner.client = BrokenAttachmentClient()
            converted = allowed_runner.download_and_convert_attachments(document)
            self.assertEqual(allowed_runner.required_failures, [])
            self.assertEqual(converted[0].status, ATTACHMENT_FAILED)
            self.assertEqual(converted[0].preservation_status, "preserved")
            self.assertFalse(converted[0].searchable)
            self.assertTrue((Path(tmp) / converted[0].raw_path).is_file())

        with tempfile.TemporaryDirectory() as tmp:
            strict_runner = SyncRunner(
                data_dir=Path(tmp),
                base_url="https://rule.krx.co.kr",
                limit=0,
                recent_only=False,
                rule_id="",
                download_attachments=True,
                language="ko",
            )
            strict_runner.client = BrokenAttachmentClient()
            strict_runner.download_and_convert_attachments(document)
            self.assertTrue(strict_runner.required_failures)

    def test_release_validation_requires_v2_manifest_and_integrity_hashes(self) -> None:
        doc = Document(
            id="release",
            title="release",
            source_url="https://example.test/release",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="body",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, doc)
            missing = "\n".join(validate_data(root, release_mode=True))
            self.assertIn("release manifest is required", missing)
            write_sync_manifest(root, [doc], [], "https://example.test")
            self.assertEqual(validate_data(root, release_mode=True), [])
            payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            payload.pop("index_source_hash")
            (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
            stale = "\n".join(validate_data(root, release_mode=True))
        self.assertIn("index_source_hash is required for release", stale)
        self.assertIn("release_hash mismatch", stale)

    def test_release_rejects_v1_document_and_failed_required_source(self) -> None:
        doc = Document(
            id="legacy-release",
            title="legacy release",
            source_url="https://example.test/legacy-release",
            document_type="rule",
            collected_at="2026-07-01T00:00:00Z",
            body="body",
            schema_version=1,
            preservation_status="failed",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_document(root, doc)
            write_sync_manifest(root, [doc], [], "https://example.test")
            errors = "\n".join(validate_data(root, release_mode=True))
        self.assertIn("schema_version 2 is required for release document", errors)
        self.assertIn("schema-v2 document entry is required", errors)
        self.assertIn("required_source_missing", errors)


def snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeClient:
    def current_rule_items(self, limit: int) -> list[Item]:
        return [Item(id="rule-1", title="규정", document_type="rule")]

    def recent_items(self) -> list[Item]:
        return [Item(id="notice-1", title="예고", document_type="notice")]


if __name__ == "__main__":
    unittest.main()
