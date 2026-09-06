from __future__ import annotations

from pathlib import Path
import re

from .base import ConversionError
from .pdf_comparison import classify_comparison_pdf, restore_comparison_pages


MAX_PDF_PAGES = 2000


def extract_pdf(path: Path) -> str:
    text, _ = extract_pdf_details(path)
    return text


def extract_pdf_details(path: Path, *, comparison_id: str = "") -> tuple[str, int]:
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.layout import LAParams
    except ImportError as exc:
        raise ConversionError("pdfminer.six is not installed") from exc
    # Hierarchical box grouping can move a centred chapter heading below its
    # left-aligned article title. Preserve the geometric reading order; known
    # amendment comparison tables are still restored from their coordinate grid.
    raw_text = extract_text(str(path), maxpages=MAX_PDF_PAGES, laparams=LAParams(boxes_flow=None))
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
    return bool(re.search(r"(Korea Exchange|KRX|Regulation|Rules|Enforcement)", line, re.I))
