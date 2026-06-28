# Data Format

Documents are stored as Markdown with YAML frontmatter.

```yaml
---
id: "210207961"
title: "코스닥시장 상장규정"
category: "업무규정 / 코스닥시장규정"
source_url: "https://rule.krx.co.kr/out/regulation/regulationViewPop.do"
effective_date: "2026-07-01"
published_date: "2026-05-13"
collected_at: "2026-06-16T13:00:00Z"
content_hash: "sha256..."
document_type: "rule"
language: "ko"
attachments:
  - id: "210203562-210032775-hwp"
    title: "[별표 1] 시가기준가종목의 최초의 가격을 결정하기 위한 최저호가가격 및 최고호가가격 산정기준"
    file_name: "유가증권시장 업무규정 시행세칙_172차_시가기준가종목의최초의가격을결정하기위한최저호가가격및최고호가가격산정기준.hwp"
    source_url: "/Download.do"
    raw_path: "ko/rules/코스닥시장-상장규정/raw/별표-1-시가기준가종목의-최초의-가격을-결정하기-위한-최저호가가격-및-최고호가가격-산정기준.hwp"
    text_path: "ko/rules/코스닥시장-상장규정/attachments/별표-1-시가기준가종목의-최초의-가격을-결정하기-위한-최저호가가격-및-최고호가가격-산정기준.md"
    content_hash: "sha256..."
    status: "converted"
    quality_status: "ok"
    quality_score: 100
    converted_text_chars: 18354
    table_row_count: 12
    formula_hint_count: 1
---
```

Required document fields:

- `id`
- `title`
- `source_url`
- `collected_at`
- `content_hash`
- `document_type`
- `language`: `ko` or `en`

The `id` field is the stable KRX document id used by MCP resource URIs and search metadata. Korean documents use the KRX id. English full-text documents use `{source_id}-en` and keep the Korean document id in `source_id`. Generated directory names are title-based for readability.

Language-specific corpus directories:

- `ko/rules/<title>/index.md`, `ko/notices/<title>/index.md`: Korean source pages.
- `en/rules/<title>/index.md`: English full-text rule documents when available.
- `<document>/raw`: downloaded original files for that rule or notice.
- `<document>/attachments`: converted Markdown attachments for that rule or notice.

Legacy `rules`, `notices`, and `attachments` directories may still be read by downstream tools as Korean corpus, but new sync output uses language-specific directories.

Attachment statuses are `pending`, `converted`, or `failed`.

Current-rule history attachments such as `전문(JUN)`, `개정이유`, `개정문`, and `신구조문` are intentionally skipped. They either duplicate the main rule body or describe past revisions. Direct `별표 및 서식` downloads are collected as normal attachments because they frequently carry tables, formulas, and templates needed for RAG answers. Future amendment notice attachments are kept with the notice document.

Attachment path fields are relative to the data root:

- `raw_path`: downloaded original file, when available. Raw paths point into the parent document bundle's `raw/` directory and preserve the original extension.
- `text_path`: converted Markdown text, only present for successfully converted attachments. Converted Markdown paths point into the parent document bundle's `attachments/` directory, so generated server ids do not leak into filenames.
- `content_hash`: hash of the original attachment bytes when downloaded
- `error`: failure reason for failed downloads or conversions

If conversion fails, the manifest keeps the original file path and failure reason but omits `text_path`.

## HWP Formula Blocks

Converted HWP attachments may include a dedicated `## HWP 수식` section appended after the converted body text. This section is designed for RAG use: it keeps the original HWP EqEdit script and adds a Markdown `math` block with a best-effort LaTeX conversion.

Example:

````markdown
## HWP 수식

이 섹션은 HWP EqEdit 원본 수식과 Markdown/RAG 참조용 LaTeX 자동 변환을 함께 제공합니다. `hwp-equation` 블록이 원본이며, 이어지는 `math` 블록은 best-effort 변환 결과입니다. 수식을 인용하거나 검증할 때는 원본 HWP 수식과 LaTeX 변환을 함께 참조하세요.

수식 1 원본(HWP EqEdit):
```hwp-equation
{의무호가`제시시간`} over {의무발생시간} & GEQ 일중의무이행률
```

수식 1 LaTeX(best-effort):
```math
\begin{aligned}\frac{\text{의무호가 제시시간}}{\text{의무발생시간}} & \ge \text{일중의무이행률}\end{aligned}
```
````

Important semantics:

- The `hwp-equation` block is the preserved source expression from the HWP EqEdit object.
- The `math` block is generated automatically for AI/RAG readability and Markdown math rendering.
- The LaTeX block is best-effort. It handles the KRX corpus patterns covered by the converter, but it is not a legal or mathematical guarantee that the original rendered HWP formula is identical.
- RAG clients should use the LaTeX block for retrieval and synthesis, but should keep the adjacent original block available for verification and citation-sensitive answers.

Converted attachment quality fields are optional but recommended:

- `quality_status`: `ok`, `warn`, or `fail`
- `quality_score`: simple 0-100 conversion quality score
- `quality_flags`: comma-separated warning flags such as `very_short_text`, `very_long_lines`, `replacement_characters`, `raw_table_hints_without_table_text`
- `converted_text_chars`, `converted_non_space_chars`: converted text size indicators
- `table_row_count`: table-like rows detected in the converted Markdown text
- `formula_hint_count`: formula-like expressions detected in converted text
- `replacement_char_count`: Unicode replacement characters found in converted text

`data/reports/data-quality.json` stores the full data-quality audit, including issue severity, document id, attachment id, filename, and message. This is intended to catch RAG-risky data issues such as empty conversion output, suspiciously short converted text, broken characters, or HWPX table/formula hints that did not survive conversion.

Search indexes are not generated by this project. Pass the generated `data/` directory to `krx-rule-mcp` and run `krx-rule-index` there when you need BM25 or vector snapshots.
