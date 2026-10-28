from __future__ import annotations

from pathlib import Path
from io import StringIO
import re

from .base import ConversionError
from .pdf_comparison import classify_comparison_pdf, restore_comparison_pages, walk_layout


MAX_PDF_PAGES = 2000


def extract_pdf(path: Path) -> str:
    text, _ = extract_pdf_details(path)
    return text


def extract_pdf_details(path: Path, *, comparison_id: str = "") -> tuple[str, int]:
    try:
        from pdfminer.converter import TextConverter
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams, LTChar
        from pdfminer.pdfinterp import PDFResourceManager
    except ImportError as exc:
        raise ConversionError("pdfminer.six is not installed") from exc
    # Hierarchical box grouping can move a centred chapter heading below its
    # left-aligned article title. Preserve the geometric reading order; known
    # amendment comparison tables are still restored from their coordinate grid.
    with StringIO() as output:
        converter = TextConverter(PDFResourceManager(), output)
        try:
            layouts = extract_pages(str(path), maxpages=MAX_PDF_PAGES, laparams=LAParams(boxes_flow=None))
            for page_index, layout in enumerate(layouts):
                # Enabling vertical heuristics on ordinary pages can collect
                # article symbols at the left margin into a false vertical line.
                # Use them only where the PDF actually contains rotated glyphs.
                rotated = any(
                    isinstance(item, LTChar)
                    and abs(item.matrix[1]) > abs(item.matrix[0])
                    and abs(item.matrix[2]) > abs(item.matrix[3])
                    and item.get_text().strip()
                    for item in walk_layout(layout)
                )
                if rotated:
                    layout = next(extract_pages(
                        str(path),
                        page_numbers=[page_index],
                        laparams=LAParams(boxes_flow=None, detect_vertical=True),
                    ))
                converter.receive_layout(layout)
            raw_text = output.getvalue()
        finally:
            converter.close()
    page_count = max(1, raw_text.count("\x0c"))
    if comparison_id:
        classification = classify_comparison_pdf(path, comparison_id)
        raw_text = restore_comparison_pages(raw_text, classification)
    return postprocess_pdf_text(raw_text), page_count


def looks_like_amendment_comparison(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    return "현행" in normalized and ("개정안" in normalized or "개정(안)" in normalized)


def has_structured_table(text: str) -> bool:
    return bool(re.search(r"(?:^\s*\|.+\|\s*$|<table\b)", text or "", re.I | re.M))


def postprocess_pdf_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = [line.rstrip() for line in text.split("\n")]
    repeated = repeated_pdf_lines(raw_lines)
    lines: list[str] = []
    blank_count = 0
    for line in raw_lines:
        stripped = line.strip()
        if stripped and stripped in repeated:
            continue
        if stripped and re.search(r"\.{4,}\s*\d+\s*$", stripped):
            continue
        if stripped:
            lines.append(stripped)
            blank_count = 0
            continue
        blank_count += 1
        if blank_count <= 2:
            lines.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def repeated_pdf_lines(lines: list[str]) -> set[str]:
    counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        counts[stripped] = counts.get(stripped, 0) + 1
    return {line for line, count in counts.items() if count >= 3 and looks_like_pdf_header_or_footer(line)}


def looks_like_pdf_header_or_footer(line: str) -> bool:
    if re.fullmatch(r"-?\s*\d+\s*-?", line):
        return True
    if re.match(r"(?:§\s*\d|Article\s+\d|CHAPTER\s+(?:\d|[IVXLCDM]+\b))", line, re.I):
        return False
    return bool(re.search(r"(Korea Exchange|KRX|Regulation|Rules|Enforcement)", line, re.I))
