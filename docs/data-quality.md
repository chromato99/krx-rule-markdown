# Data Quality

`krx-rule-markdown quality` audits the current Markdown corpus and converted attachments.

```bash
krx-rule-markdown quality --data-dir data --update-metadata
```

The default report path is `data/reports/data-quality.json`.

## What It Checks

- Attachment conversion status: `converted`, `failed`, or `pending`
- Converted text length and non-space character count
- Unicode replacement characters, which usually indicate broken decoding
- Very long lines, which can mean table or paragraph boundaries were lost
- Table-like rows in converted Markdown text
- Formula-like expressions in converted Markdown text
- HWPX raw XML table/formula hints that did not appear in converted text
- HWP EqEdit formula blocks preserved as `hwp-equation` plus generated `math` blocks

The audit is intentionally conservative. It does not prove legal or semantic correctness. It highlights places where a human or a stronger converter should inspect the source.

## Formula Quality

HWP EqEdit formulas are treated as high-value RAG content. The converter preserves the original EqEdit script and emits a neighboring LaTeX `math` block. The quality pass records `formula_hint_count` so formula-heavy attachments can be reviewed more easily.

When validating formula conversion, check for structural LaTeX risks such as:

- raw HWP commands left in output, for example `over`, `GEQ`, `LEFT`, or `RIGHT`
- empty or repeated scripts such as `x_{}` or `x_{i}_{j}`
- spaces before scripts such as `x _{i}`
- unwrapped alignment markers such as `&` outside `aligned`, `cases`, or `matrix`
- suspicious command escaping such as `\\left`
- unbalanced LaTeX grouping braces

Passing these checks means the generated LaTeX is structurally suitable for RAG and Markdown math rendering. It still does not replace the preserved `hwp-equation` source. For legal, trading, or model-validation use cases, inspect the original HWP formula and official KRX document when the exact formula matters.

## Status

- `ok`: no obvious conversion-quality issue was detected
- `warn`: usable text exists, but the attachment may be incomplete, too short, structurally weak, or converted with a known failure elsewhere
- `fail`: converted text is missing or empty

By default the command exits successfully after writing the report. Use stricter modes in CI:

```bash
krx-rule-markdown quality --data-dir data --fail-on error
krx-rule-markdown quality --data-dir data --fail-on warn
```

## RAG Use

For production RAG, review the report after each full sync. High `conversion_failed` counts reduce attachment coverage. `very_short_text` can appear for empty or boilerplate future-notice attachments, but it should still be reviewed. `raw_table_hints_without_table_text` and `raw_formula_hints_without_formula_text` are stronger signs that structural content may have been lost.
