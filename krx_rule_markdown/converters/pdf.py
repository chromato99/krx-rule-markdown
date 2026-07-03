from __future__ import annotations

from pathlib import Path
import re

from .base import ConversionError


def extract_pdf(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:
        raise ConversionError("pdfminer.six is not installed") from exc
    return postprocess_pdf_text(extract_text(str(path)))


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
