# Data Format

Documents are stored as Markdown with YAML frontmatter.

```yaml
---
schema_version: 2
id: "210207961"
title: "코스닥시장 상장규정"
category: "업무규정 / 코스닥시장규정"
source_url: "https://rule.krx.co.kr/out/regulation/regulationViewPop.do"
effective_date: "2026-07-01"
published_date: "2026-05-13"
collected_at: "2026-06-16T13:00:00Z"
body_hash: "sha256-of-canonical-markdown-body"
document_type: "rule"
language: "ko"
conversion_status: "converted"
preservation_status: "preserved"
searchable: true
quality_status: "ok"
source_content_path: "ko/rules/코스닥시장-상장규정/raw/source.html"
source_content_hash: "sha256-of-canonical-source-html"
source_request_path: "ko/rules/코스닥시장-상장규정/raw/request.json"
converter_version: "2"
attachments:
  - id: "210203562-210032775-hwp"
    title: "[별표 1] 시가기준가종목의 최초의 가격을 결정하기 위한 최저호가가격 및 최고호가가격 산정기준"
    file_name: "유가증권시장 업무규정 시행세칙_172차_시가기준가종목의최초의가격을결정하기위한최저호가가격및최고호가가격산정기준.hwp"
    source_url: "/Download.do"
    raw_path: "ko/rules/코스닥시장-상장규정/raw/별표-1-시가기준가종목의-최초의-가격을-결정하기-위한-최저호가가격-및-최고호가가격-산정기준.hwp"
    text_path: "ko/rules/코스닥시장-상장규정/attachments/별표-1-시가기준가종목의-최초의-가격을-결정하기-위한-최저호가가격-및-최고호가가격-산정기준.md"
    raw_file_hash: "sha256-of-original-bytes"
    converted_text_hash: "sha256-of-canonical-converted-markdown"
    conversion_status: "converted"
    preservation_status: "preserved"
    searchable: true
    assets:
      - id: "asset-210203562-210032775-hwp-hwp-bindata-bin0001-jpg"
        source_kind: "hwp_bindata"
        source_anchor: "hwp:BinData/BIN0001.jpg"
        path: "ko/rules/코스닥시장-상장규정/assets/210203562-210032775-hwp/bin0001.jpg"
        mime_type: "image/jpeg"
        raw_file_hash: "sha256-of-image-bytes"
        size: 12345
        width: 640
        height: 480
        preservation_status: "preserved"
        searchable: false
        quality_codes: ["image_content_unindexed"]
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
- `body_hash`
- `document_type`
- `language`: `ko` or `en`

Schema v2 uses separate hashes with one meaning each:

- `body_hash`: canonical Markdown document body
- `source_content_hash`: sanitized collection-time source HTML
- `raw_file_hash`: exact downloaded raw bytes
- `converted_text_hash`: canonical converted attachment Markdown
- `index_source_hash`: canonical projection of all index-affecting documents and attachments, stored in `manifest.json`
- `release_hash`: reproducible manifest contents, excluding operational timestamps and response-observation fields

Canonical text is UTF-8, LF line endings, Unicode NFC, with whitespace trimmed only at the complete-value boundary. Canonical JSON uses NFC strings, sorted keys, UTF-8, and no insignificant whitespace. `content_hash` and attachment `status` remain readable during the v1 migration, but new output writes `body_hash`/`raw_file_hash` and `conversion_status` explicitly.

The `id` field is the stable KRX document id used by MCP resource URIs and search metadata. Korean documents use the KRX id. English full-text documents use `{source_id}-en` and keep the Korean document id in `source_id`. Generated directory names are title-based for readability.

Language-specific corpus directories:

- `ko/rules/<title>/index.md`, `ko/notices/<title>/index.md`: Korean source pages.
- `en/rules/<title>/index.md`: English full-text rule documents when available.
- `<document>/raw`: downloaded original files for that rule or notice.
- `<document>/attachments`: converted Markdown attachments for that rule or notice.

Legacy `rules`, `notices`, and `attachments` directories may still be read by downstream tools as Korean corpus, but new sync output uses language-specific directories.

Attachment statuses are `pending`, `converted`, or `failed`.

Conversion and preservation are independent axes. `conversion_status` says whether searchable text was produced; `preservation_status` says whether the source bytes/content were retained (`preserved`, `missing`, or `failed`). `quality_status` is `ok`, `warn`, or `fail`. `searchable` is false for failed conversion/quality and for known image-only or structurally unreliable content even when the original is preserved. `quality_codes` contains canonical machine-readable reasons such as `pdf_text_layer_too_sparse`, `pdf_comparison_structure_lost`, `image_content_unindexed`, or `stale_due_to_refresh_failure`.

Current-rule history attachments such as `전문(JUN)`, `개정이유`, `개정문`, and `신구조문` are intentionally skipped. They either duplicate the main rule body or describe past revisions. Direct `별표 및 서식` downloads are collected as normal attachments because they frequently carry tables, formulas, and templates needed for RAG answers. Future amendment notice attachments are kept with the notice document.

Attachment path fields are relative to the data root:

- `raw_path`: downloaded original file, when available. Raw paths point into the parent document bundle's `raw/` directory and preserve the original extension.
- `text_path`: converted Markdown text, only present for successfully converted attachments. Converted Markdown paths point into the parent document bundle's `attachments/` directory, so generated server ids do not leak into filenames.
- `content_hash`: hash of the original attachment bytes when downloaded
- `error`: failure reason for failed downloads or conversions

If conversion fails, the manifest keeps the original file path and failure reason but omits `text_path`.

For Korean pages, `source_content_path` points to sanitized source HTML captured at collection time and `source_request_path` points to a JSON descriptor containing only the public endpoint and stable document identifiers needed to reproduce the request. Cookies, CSRF values, authorization headers, and other session secrets are never committed. Volatile response observations and refresh failures are stored outside the release under the sibling `.krx-rule-runs/` directory.

All paths are data-root-relative, must remain inside the owning document bundle, and may not contain parent traversal or symlink escapes. Document and attachment IDs share one global namespace.

Materialized image assets may occur on a document (`html_inline`) or attachment (`hwp_bindata`). Their IDs share the same global namespace as documents and attachments. A preserved asset requires a bundle-contained regular file, exact `raw_file_hash` and byte size, supported MIME signature, bounded positive dimensions, `preservation_status: preserved`, and explicit `searchable: false`. `source_anchor` is `html-img:<source_url>` or `hwp:BinData/<stream>`. Markdown refers to an asset by the opaque `krx-asset:<id>` identifier; filesystem paths remain metadata-only and are not a public serving URL.

## Release Manifest

`manifest.json` is the producer/consumer handoff. A release validation requires schema version 2, an entry for every on-disk document, exact frontmatter parity, `index_source_hash`, and `release_hash`. A consumer must validate the manifest before building or loading a production index; directory names and host filesystem paths are not public identifiers.

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

## HWPX Support Level

HWPX parsing is experimental because the checked-in corpus currently has no representative HWPX raw fixture. The reader enforces ZIP entry, decompressed-size, compression-ratio, and encrypted-entry limits and converts the structures it can verify. Source-order reconstruction and complex drawing/layout fidelity are not stable guarantees until real corpus fixtures and golden outputs are added.

Search indexes are not generated by this project. Pass the generated `data/` directory to `krx-rule-mcp` and run `krx-rule-index` there when you need BM25 or vector snapshots.
