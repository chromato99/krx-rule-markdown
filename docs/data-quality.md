# Data quality

Use `quality` to find conversion issues by document and attachment ID. Before distributing a corpus, run `validate --release --quality` to check its files, manifest, hashes and release-quality errors together.

## Audit or update

| Command | Effect |
| --- | --- |
| `krx-rule-markdown quality --data-dir data` | Writes `data/reports/data-quality.json`. The default `--fail-on none` reports issues without failing on their severity. Use `--output` for another destination. |
| `krx-rule-markdown quality --data-dir data --fail-on error` | Writes the audit report and exits nonzero for an error. Use `--fail-on warn` to fail on warnings as well. |
| `krx-rule-markdown quality --data-dir data --update-metadata` | Updates frontmatter and the manifest in staging. The default is `--fail-on error`. A rejected update leaves the active release intact. Explicit `--fail-on none` skips the quality threshold and emits a warning. |
| `krx-rule-markdown validate --data-dir data --release --quality` | Checks schema-v2 frontmatter parity, paths, hashes, image metadata and the strict release profile, then runs the quality audit. |

Pending conversions block the release quality gate. A failed conversion also blocks it unless its ID was passed to `sync --allow-failure-id`, its original file is preserved (`preservation_status: preserved`), and `searchable` is `false`. An allowed failure still produces a warning, so `--fail-on warn` rejects it. Subsequent audits read `release_profile.allowed_failure_ids` from the manifest. Any IDs supplied explicitly to `quality` or `validate --quality` must match that set exactly.

## Read the report

Start with `summary`. Each issue has a `document_id`, `severity` and `code`. Attachment and image issues also identify their source with `attachment_id` or `asset_id`. An attachment's `quality_status` can be `ok`, `warn` or `fail`. Short converted text can trigger a warning. Empty converted text or a mismatch in the HWP equation count fails the check.

| Finding | What to inspect |
| --- | --- |
| Missing or sparse text | Check the original for a failed conversion, an image-only PDF or an empty body. A searchable converted attachment can provide the text for an empty document body. |
| Lost table structure | Compare row and cell counts with the HWP/HWPX source. Long lines or `raw_table_hints_without_table_text` can indicate flattened rows. For PDF comparison tables, check the `restored` or `degraded` classification and its reason. |
| Formula warnings | `formula_hint_count` counts formula-like text. Compare the preserved `hwp-equation` count with the raw EqEdit count and inspect the generated `math` for lost operators, grouping or alignment. |
| Missing image or decoding damage | Check unresolved image references, HWP picture assets and Unicode replacement characters against the original. |

## When an update fails

Each change to corpus documents or their manifest is staged beside the active directory. A writer lock admits one writer at a time. Once validation passes, Linux `renameat2(RENAME_EXCHANGE)` swaps an existing release with the staged directory. A staging or validation failure leaves the last published release active. [Collection sequence](../README.md#corpus-생성)

During `sync`, a failed refresh can retain the last usable content with `stale_due_to_refresh_failure`. During `reconvert`, a new blocking failure discards that operation's staged changes. Its `blocking_failed` count covers required, stale, inspection and quality failures. For either command, look in `.krx-rule-runs/latest.json` next to the data directory for the outcome and error details. Dry runs of `reconvert` leave that record untouched.
