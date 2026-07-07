from __future__ import annotations

from dataclasses import dataclass
import re
from html import escape

from .base import normalize_text

HTML_CELL_WRAP = 900


@dataclass(frozen=True)
class RawHtml:
    html: str


CellValue = str | RawHtml


def render_markdown_table(rows: list[list[CellValue]]) -> str:
    rows = normalize_rows(rows)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = [
        "| " + " | ".join(escape_markdown_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(escape_markdown_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def render_html_table(
    rows: list[list[CellValue]],
    spans: list[list[tuple[int, int]]] | None = None,
    *,
    preserve_empty_rows: bool = False,
) -> str:
    if spans is None:
        rows = normalize_rows(rows, preserve_empty_rows=preserve_empty_rows)
        spans = [[(1, 1) for _ in row] for row in rows]
    else:
        rows, spans = normalize_rows_and_spans(rows, spans, preserve_empty_rows=preserve_empty_rows)
    if not rows:
        return ""
    lines = ["<table>"]
    for row, span_row in zip(rows, spans, strict=False):
        lines.append("  <tr>")
        for cell, (rowspan, colspan) in zip(row, span_row, strict=False):
            attrs = []
            if rowspan > 1:
                attrs.append(f'rowspan="{rowspan}"')
            if colspan > 1:
                attrs.append(f'colspan="{colspan}"')
            attr = " " + " ".join(attrs) if attrs else ""
            cell_lines = wrapped_html_cell_lines(cell)
            if len(cell_lines) == 1:
                lines.append(f"    <td{attr}>{cell_lines[0]}</td>")
            else:
                lines.append(f"    <td{attr}>")
                lines.extend(f"      {line}" for line in cell_lines)
                lines.append("    </td>")
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


def normalize_angle_bracket_tables(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    pending: list[list[str]] = []

    def flush() -> None:
        nonlocal pending
        if pending:
            rendered = render_markdown_table(pending)
            if rendered:
                out.append(rendered)
            pending = []

    for line in lines:
        cells = parse_angle_bracket_table_line(line)
        if cells is not None:
            pending.append(cells)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def parse_angle_bracket_table_line(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped or "<" not in stripped or ">" not in stripped:
        return None
    cells = re.findall(r"<([^<>]*)>", stripped)
    if len(cells) < 2:
        return None
    if "".join(f"<{cell}>" for cell in cells) != stripped:
        return None
    if any(is_html_tag_fragment(cell) for cell in cells):
        return None
    cells = [normalize_text(cell) for cell in cells]
    if not any(cells):
        return None
    return cells


def is_html_tag_fragment(cell: str) -> bool:
    return bool(re.match(r"/?\s*(?:table|tbody|thead|tr|td|th|br)\b", cell.strip(), re.I))


def table_has_spans(spans: list[list[tuple[int, int]]]) -> bool:
    return any(rowspan > 1 or colspan > 1 for row in spans for rowspan, colspan in row)


def table_needs_html(rows: list[list[CellValue]], spans: list[list[tuple[int, int]]]) -> bool:
    return table_has_spans(spans) or any(
        isinstance(cell, RawHtml) or "<br>" in cell_to_text(cell) or len(cell_to_text(cell)) > 180
        for row in rows
        for cell in row
    )


def normalize_rows(rows: list[list[CellValue]], *, preserve_empty_rows: bool = False) -> list[list[CellValue]]:
    normalized = [[normalize_cell(cell) for cell in row] for row in rows]
    if preserve_empty_rows:
        return normalized if any(any(cell_has_text(cell) for cell in row) for row in normalized) else []
    return [row for row in normalized if any(cell_has_text(cell) for cell in row)]


def normalize_rows_and_spans(
    rows: list[list[CellValue]],
    spans: list[list[tuple[int, int]]],
    *,
    preserve_empty_rows: bool = False,
) -> tuple[list[list[CellValue]], list[list[tuple[int, int]]]]:
    normalized_rows: list[list[CellValue]] = []
    normalized_spans: list[list[tuple[int, int]]] = []
    any_text = False
    for index, row in enumerate(rows):
        normalized = [normalize_cell(cell) for cell in row]
        has_text = any(cell_has_text(cell) for cell in normalized)
        any_text = any_text or has_text
        if not preserve_empty_rows and not has_text:
            continue
        span_row = list(spans[index]) if index < len(spans) else []
        if len(span_row) < len(normalized):
            span_row.extend((1, 1) for _ in range(len(normalized) - len(span_row)))
        normalized_rows.append(normalized)
        normalized_spans.append(span_row[: len(normalized)])
    if preserve_empty_rows and not any_text:
        return [], []
    return normalized_rows, normalized_spans


def normalize_cell(cell: CellValue) -> CellValue:
    if isinstance(cell, RawHtml):
        return RawHtml(cell.html.strip())
    return normalize_text(str(cell))


def cell_to_text(cell: CellValue) -> str:
    return cell.html if isinstance(cell, RawHtml) else str(cell)


def cell_has_text(cell: CellValue) -> bool:
    return bool(cell_to_text(cell).strip())


def escape_markdown_cell(cell: CellValue) -> str:
    text = cell_to_text(cell)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def escape_html_cell(cell: str) -> str:
    return escape(cell).replace("&lt;br&gt;", "<br>")


def wrapped_html_cell_lines(cell: CellValue) -> list[str]:
    if isinstance(cell, RawHtml):
        lines = cell.html.splitlines()
        return lines or [""]
    escaped = escape_html_cell(cell)
    if len(escaped) <= HTML_CELL_WRAP:
        return [escaped]
    lines: list[str] = []
    logical_lines = escaped.split("<br>")
    for index, logical in enumerate(logical_lines):
        wrapped = wrap_text(logical, HTML_CELL_WRAP)
        if index < len(logical_lines) - 1:
            wrapped[-1] += "<br>"
        lines.extend(wrapped)
    return lines


def wrap_text(text: str, width: int) -> list[str]:
    if len(text) <= width:
        return [text]
    lines: list[str] = []
    remaining = text
    while len(remaining) > width:
        split_at = remaining.rfind(" ", 0, width)
        if split_at < width // 2:
            split_at = width
        lines.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        lines.append(remaining)
    return lines or [""]
