from __future__ import annotations

from pathlib import Path
import ast
import io
import tempfile
import unittest
import zipfile

from krx_rule_markdown.convert import (
    append_hwp_equations,
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
    parse_rule_attachments,
    safe_base,
)
from krx_rule_markdown.clean import clean_unreferenced_attachments, drop_past_rule_attachments
from krx_rule_markdown.markdown import load_documents, parse_markdown, write_document
from krx_rule_markdown.models import ATTACHMENT_CONVERTED, Attachment, Document, Item, now_utc
from krx_rule_markdown.paths import converted_attachment_path, raw_attachment_path
from krx_rule_markdown.quality import audit_data_quality, inspect_attachment_quality
from krx_rule_markdown.sync import collect_items, includes_english, includes_korean, normalize_sync_language


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

    def test_load_documents_keeps_legacy_root_rules_as_korean(self) -> None:
        raw = """---
id: "legacy-rule"
title: Legacy Rule
source_url: https://example.test/rule
collected_at: 2026-06-16T14:33:12Z
content_hash: hash
document_type: rule
---

body
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rules").mkdir()
            (root / "rules" / "legacy.md").write_text(raw, encoding="utf-8")
            loaded = load_documents(root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].language, "ko")

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
        path = converted_attachment_path(Path("data"), doc, att)
        self.assertEqual(
            path,
            Path("data/ko/rules/유가증권시장-업무규정-시행세칙/attachments/별표-1-이론가격-산출기준.md"),
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
        path = raw_attachment_path(Path("data"), doc, att)
        self.assertEqual(
            path,
            Path("data/ko/rules/유가증권시장-업무규정-시행세칙/raw/별표-1-이론가격-산출기준.hwp"),
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
        self.assertIn("수식: A=B+1", text)
        self.assertGreaterEqual(quality.table_row_count, 1)
        self.assertGreaterEqual(quality.formula_hint_count, 1)
        self.assertNotIn("raw_table_hints_without_table_text", quality.flags)
        self.assertNotIn("raw_formula_hints_without_formula_text", quality.flags)

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
        self.assertGreaterEqual(quality.formula_hint_count, 2)

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
        self.assertIn(r"D_{i} = \frac{\operatorname{Div}_{i}}{MC}", latex)
        self.assertIn(r"\times 100 \times", latex)
        self.assertIn(r"\frac{f_{i} \times {t_{i}}}{365}", latex)

    def test_hwp_equation_to_latex_balances_malformed_hwp_groups(self) -> None:
        latex = hwp_equation_to_latex("KOFR_{T-1D")
        self.assertEqual(latex, "KOFR_{T - 1D}")

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
        self.assertEqual(report["summary"]["quality_status"]["ok"], 1)
        self.assertEqual(loaded.attachments[0].quality_status, "ok")
        self.assertGreaterEqual(loaded.attachments[0].table_row_count, 1)

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


class FakeClient:
    def current_rule_items(self, limit: int) -> list[Item]:
        return [Item(id="rule-1", title="규정", document_type="rule")]

    def recent_items(self) -> list[Item]:
        return [Item(id="notice-1", title="예고", document_type="notice")]


if __name__ == "__main__":
    unittest.main()
