# Data format

A release is a `data/` directory containing a schema-v2 manifest and one bundle per document. Use document and attachment IDs for references. The directory names come from titles and can differ from source IDs. [Example document and attachments](../data/ko/rules/주식주가지수-파생상품-시장조성-운영지침/index.md)

## Bundle layout

| Path under `data/` | Contents |
| --- | --- |
| `ko/rules/<title>/index.md`, `ko/notices/<title>/index.md` | Korean rule or amendment notice with YAML frontmatter |
| `en/rules/<title>/index.md` | Available English full text. Its ID is `{source_id}-en`, where `source_id` identifies the Korean rule. |
| `<bundle>/raw/` | Downloaded originals. For Korean source pages, `source.html` and `request.json` record the sanitized HTML and the public request fields. |
| `<bundle>/attachments/`, `<bundle>/assets/` | Converted attachment Markdown and preserved image files |

Frontmatter identifies each document by `id`, `title`, `document_type` and `language`. It records the official `source_url`, collection time (`collected_at`), body hash and searchability. When source HTML is preserved, `source_content_path`, `source_content_hash` and `source_request_path` travel together. The request descriptor contains the endpoint, permitted document identifiers and source hash. Session credentials are excluded.

Each attachment belongs to its parent rule or notice. Its `raw_path` and `text_path` are relative to the data root and stay inside that bundle. Documents, attachments and assets share one ID namespace. Current rules retain annexes and forms. The collector skips duplicate full-text attachments (`전문/JUN`) and past revision material such as `개정이유` and `신구조문`. Amendment notices retain their own attachments.

## Release manifest

Before building a search index, validate the [manifest](../data/manifest.json) against every on-disk document and its frontmatter. Schema v2 distinguishes these SHA-256 values.

| Field | Input |
| --- | --- |
| `body_hash`, `converted_text_hash` | Canonical Markdown of the document body and converted attachment, respectively |
| `source_content_hash`, `raw_file_hash` | Canonical sanitized source HTML and exact original file bytes, respectively |
| `index_source_hash` | Ordered document and attachment metadata used for search, including text hashes, quality and searchability |
| `release_hash` | Reproducible manifest content. Generation/check timestamps, response observation hashes and transient refresh errors are excluded. |

Canonical text uses UTF-8, LF and Unicode NFC, with whitespace trimmed at the outer boundaries. Canonical JSON uses normalized strings, sorted keys and compact UTF-8 encoding. See the [hash implementation](../krx_rule_markdown/contracts.py) and [shared contract fixture](../tests/fixtures/corpus_contract_v2.json).

## What survives conversion

- **Tables and notices.** HWP/HWPX tables use Markdown for regular cells and HTML for merged cells (`rowspan`, `colspan`). A notice keeps the full `내용` cell, including nested tables. HWPX support is experimental. The reader limits ZIP expansion and rejects encrypted entries. Complex drawing layout and source order have limited coverage.
- **HWP formulas.** Layout extraction places EqEdit source (`hwp-equation`) and generated LaTeX (`math`, when available) near the paragraph or table. Unplaced equations go under `위치 미확정 HWP 수식`. Text or preview fallback appends recovered equations under `HWP 수식`. Verify exact signs, grouping and subscripts against EqEdit or the original HWP.
- **PDFs.** Text is ordered using page coordinates. For known comparison attachments, restoration requires matching headers, a two- or three-column grid and enough paired row content. Uncertain layouts remain `degraded`. [Classification report](../data/reports/pdf-comparison-classification.json)
- **Images.** Preserved document images (`html_inline`) and HWP images (`hwp_bindata`) are stored in the bundle and referenced as `krx-asset:<id>` in Markdown. Their metadata records the source anchor, MIME type, byte size, dimensions and raw hash, with `searchable: false`. During HTML reconversion, relative image URLs are resolved against the source origin and reconnected to saved assets offline.

Read `conversion_status` (`pending`, `converted`, `failed`), `preservation_status` (`preserved`, `missing`, `failed`), `quality_status` and `searchable` separately. An image-only PDF, for example, can retain its original bytes while its text is excluded from search. [Quality checks](data-quality.md)
