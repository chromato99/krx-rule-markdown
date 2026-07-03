from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from html import unescape

from .base import ConversionError, dedupe_adjacent, normalize_text
from .tables import render_html_table, render_markdown_table, table_needs_html


def extract_hwpx(data: bytes) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if not lower.endswith(".xml"):
                continue
            if "section" not in lower and "bodytext" not in lower:
                continue
            xml = zf.read(name).decode("utf-8", errors="replace")
            text = extract_hwpx_xml(xml)
            if text:
                chunks.append(text)
    if not chunks:
        raise ConversionError("no readable HWPX body XML found")
    return "\n\n".join(chunks)


def extract_hwpx_xml(xml: str) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return fallback_xml_text(xml)

    lines: list[str] = []
    table_descendants: set[int] = set()
    for tbl in root.iter():
        if local_name(tbl.tag) != "tbl":
            continue
        for node in tbl.iter():
            table_descendants.add(id(node))
        rows, spans = extract_hwpx_table(tbl)
        if rows:
            if table_needs_html(rows, spans):
                lines.append(render_html_table(rows, spans))
            else:
                lines.append(render_markdown_table(rows))

    for elem in root.iter():
        if id(elem) in table_descendants:
            continue
        lname = local_name(elem.tag)
        if lname in {"p", "equation", "formula", "eq"}:
            text = normalize_text(" ".join(iter_element_text(elem)))
            if lname in {"equation", "formula", "eq"}:
                attr_text = normalize_text(" ".join(str(value) for value in elem.attrib.values()))
                text = normalize_text(f"{text} {attr_text}")
                if text:
                    text = "수식: " + text
            if text:
                lines.append(text)
    if not lines:
        return fallback_xml_text(xml)
    return "\n".join(dedupe_adjacent(lines))


def local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def extract_hwpx_table(tbl: ET.Element) -> tuple[list[list[str]], list[list[tuple[int, int]]]]:
    rows: list[list[str]] = []
    spans: list[list[tuple[int, int]]] = []
    for tr in tbl.iter():
        if local_name(tr.tag) != "tr":
            continue
        row: list[str] = []
        span_row: list[tuple[int, int]] = []
        for tc in direct_or_descendant_table_cells(tr):
            row.append(normalize_text(" ".join(iter_element_text(tc))))
            span_row.append(cell_span(tc))
        if any(cell.strip() for cell in row):
            rows.append(row)
            spans.append(span_row)
    return rows, spans


def direct_or_descendant_table_cells(row: ET.Element) -> list[ET.Element]:
    cells = [child for child in row if local_name(child.tag) == "tc"]
    if cells:
        return cells
    return [elem for elem in row.iter() if local_name(elem.tag) == "tc"]


def cell_span(cell: ET.Element) -> tuple[int, int]:
    rowspan = 1
    colspan = 1
    for elem in [cell, *list(cell.iter())]:
        for key, value in elem.attrib.items():
            lname = local_name(key).lower()
            if lname == "rowspan":
                rowspan = max(rowspan, positive_int(value))
            elif lname == "colspan":
                colspan = max(colspan, positive_int(value))
    return rowspan, colspan


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def iter_element_text(elem: ET.Element) -> list[str]:
    parts: list[str] = []
    if elem.text and elem.text.strip():
        parts.append(unescape(elem.text.strip()))
    for child in elem:
        parts.extend(iter_element_text(child))
        if child.tail and child.tail.strip():
            parts.append(unescape(child.tail.strip()))
    return parts


def fallback_xml_text(xml: str) -> str:
    text = re.sub(r"<[^>]+>", " ", xml)
    return normalize_text(text)
