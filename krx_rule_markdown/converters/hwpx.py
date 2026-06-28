from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from html import unescape

from .base import ConversionError, dedupe_adjacent, normalize_text


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
        for tr in tbl.iter():
            if local_name(tr.tag) != "tr":
                continue
            cells = [
                normalize_text(" ".join(iter_element_text(tc)))
                for tc in tr.iter()
                if local_name(tc.tag) == "tc"
            ]
            cells = [cell for cell in cells if cell]
            if len(cells) >= 2:
                lines.append("| " + " | ".join(cells) + " |")

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
