from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from itertools import combinations
import re


KNOWN_COMPARISON_PDFS = {
    "210219879-210219880-pdf": "three_column",
    "210224393-210224395-pdf": "three_column",
    "210222057-210222059-pdf": "three_column",
    "210219622-210219624-pdf": "three_column",
    "210221769-210221771-pdf": "two_column",
    "210220231-210220236-pdf": "two_column",
    "210224396-210224398-pdf": "two_column",
}


@dataclass(frozen=True)
class PositionedText:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str


@dataclass
class ComparisonPage:
    page_number: int
    headers: list[str]
    rows: list[list[str]]


@dataclass
class ComparisonClassification:
    attachment_id: str
    expected_template: str
    status: str
    reason: str = ""
    confidence: float = 0.0
    page_count: int = 0
    table_pages: list[int] = field(default_factory=list)
    row_count: int = 0
    pages: list[ComparisonPage] = field(default_factory=list, repr=False)

    def to_mapping(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "expected_template": self.expected_template,
            "status": self.status,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "page_count": self.page_count,
            "table_pages": self.table_pages,
            "row_count": self.row_count,
        }


def classify_comparison_pdf(path: Path, attachment_id: str) -> ComparisonClassification:
    expected = KNOWN_COMPARISON_PDFS.get(attachment_id, "")
    result = ComparisonClassification(attachment_id, expected, "not_in_classification_set")
    if not expected:
        result.reason = "attachment is not in the named comparison classification set"
        return result
    try:
        from pdfminer.high_level import extract_pages
    except ImportError:
        result.status = "degraded"
        result.reason = "pdfminer is unavailable"
        return result

    started = False
    try:
        for page_number, layout in enumerate(extract_pages(str(path)), start=1):
            result.page_count = page_number
            boundaries = comparison_boundaries(layout, expected)
            if not boundaries:
                continue
            positioned = positioned_lines(layout, boundaries)
            headers = header_text(positioned, boundaries, expected)
            if not started:
                normalized = [normalize_header(value) for value in headers]
                if len(normalized) < 2 or "현행" not in normalized[0] or "개정안" not in normalized[1]:
                    continue
                started = True
            rows = visual_rows(positioned, boundaries)
            if rows:
                result.pages.append(ComparisonPage(page_number, comparison_headers(expected), rows))
    except Exception as exc:  # noqa: BLE001 - untrusted PDF stays degraded.
        result.status = "degraded"
        result.reason = f"coordinate parser failed ({type(exc).__name__})"
        return result

    result.table_pages = [page.page_number for page in result.pages]
    result.row_count = sum(len(page.rows) for page in result.pages)
    if not result.pages:
        result.status = "degraded"
        result.reason = "named template header and coordinate grid were not both found"
        return result
    populated = sum(
        1
        for page in result.pages
        for row in page.rows
        if len(row) >= 2 and row[0].strip() and row[1].strip()
    )
    pair_ratio = populated / max(1, result.row_count)
    result.confidence = min(1.0, 0.75 + min(0.25, pair_ratio / 2))
    if result.row_count < 5 or pair_ratio < 0.15:
        result.status = "degraded"
        result.reason = f"coordinate rows have insufficient paired content ({pair_ratio:.2f})"
        return result
    result.status = "restored"
    result.reason = "named coordinate grid matched"
    return result


def restore_comparison_pages(
    raw_text: str,
    classification: ComparisonClassification,
) -> str:
    if classification.status != "restored":
        return raw_text
    raw_pages = raw_text.split("\x0c")
    for page in classification.pages:
        if 1 <= page.page_number <= len(raw_pages):
            raw_pages[page.page_number - 1] = render_comparison_page(page)
    return "\n\n".join(raw_pages)


def render_comparison_page(page: ComparisonPage) -> str:
    lines = [f"### 신·구조문대비표 — PDF {page.page_number}쪽", "", "<table>", "<thead><tr>"]
    lines.extend(f"<th>{escape(header)}</th>" for header in page.headers)
    lines.extend(["</tr></thead>", "<tbody>"])
    for row in page.rows:
        lines.append("<tr>")
        lines.extend(f"<td>{escape(cell)}</td>" for cell in row)
        lines.append("</tr>")
    lines.extend(["</tbody>", "</table>"])
    return "\n".join(lines)


def comparison_boundaries(layout, expected: str) -> list[float]:
    from pdfminer.layout import LTLine

    candidates: list[tuple[float, float, float]] = []
    for item in walk_layout(layout):
        if not isinstance(item, LTLine):
            continue
        x0, y0, x1, y1 = item.bbox
        if abs(x1 - x0) <= 1.0 and y1 - y0 >= layout.height * 0.30:
            candidates.append(((x0 + x1) / 2, y0, y1))
    xs = cluster_values([item[0] for item in candidates], tolerance=2.5)
    required = 4 if expected == "three_column" else 3
    if len(xs) < required:
        return []
    matches: list[tuple[float, list[float]]] = []
    target = [0.10, 0.448, 0.775, 0.89] if expected == "three_column" else [0.12, 0.50, 0.88]
    for candidate in combinations(xs, required):
        ratios = [value / layout.width for value in candidate]
        if expected == "three_column":
            valid = (
                0.08 <= ratios[0] <= 0.14
                and 0.40 <= ratios[1] <= 0.47
                and 0.74 <= ratios[2] <= 0.80
                and 0.86 <= ratios[3] <= 0.93
            )
        else:
            valid = (
                0.10 <= ratios[0] <= 0.15
                and 0.48 <= ratios[1] <= 0.53
                and 0.86 <= ratios[2] <= 0.91
            )
        if valid:
            matches.append((sum((actual - wanted) ** 2 for actual, wanted in zip(ratios, target)), list(candidate)))
    return min(matches, default=(0.0, []), key=lambda item: item[0])[1]


def positioned_lines(layout, boundaries: list[float]) -> list[PositionedText]:
    from pdfminer.layout import LTTextLine

    vertical_lines = []
    for item in walk_layout(layout):
        if item.__class__.__name__ == "LTLine":
            x0, y0, x1, y1 = item.bbox
            if abs(x1 - x0) <= 1.0 and any(abs(x0 - value) <= 3 for value in boundaries):
                vertical_lines.append((y0, y1))
    bottom = min((item[0] for item in vertical_lines), default=0)
    top = max((item[1] for item in vertical_lines), default=layout.height)
    out: list[PositionedText] = []
    for item in walk_layout(layout):
        if not isinstance(item, LTTextLine):
            continue
        text = " ".join(item.get_text().split())
        if not text:
            continue
        x0, y0, x1, y1 = item.bbox
        center = (x0 + x1) / 2
        if boundaries[0] <= center <= boundaries[-1] and bottom <= (y0 + y1) / 2 <= top:
            out.append(PositionedText(x0, x1, y0, y1, text))
    return out


def header_text(lines: list[PositionedText], boundaries: list[float], expected: str) -> list[str]:
    for band in banded_lines(lines):
        row = band_columns(band, boundaries)
        normalized = [normalize_header(value) for value in row]
        if len(normalized) >= 2 and "현행" in normalized[0] and "개정안" in normalized[1]:
            return row
    return []


def visual_rows(lines: list[PositionedText], boundaries: list[float]) -> list[list[str]]:
    rows: list[list[str]] = []
    for band in banded_lines(lines):
        row = band_columns(band, boundaries)
        normalized = [normalize_header(value) for value in row]
        if len(normalized) >= 2 and "현행" in normalized[0] and "개정안" in normalized[1]:
            continue
        if "신구조문대비표" in "".join(normalized):
            continue
        if any(cell.strip() for cell in row):
            rows.append(row)
    return rows


def banded_lines(lines: list[PositionedText]) -> list[list[PositionedText]]:
    bands: list[list[PositionedText]] = []
    for line in sorted(lines, key=lambda item: (-(item.y0 + item.y1) / 2, item.x0)):
        center = (line.y0 + line.y1) / 2
        if not bands:
            bands.append([line])
            continue
        previous_center = sum((item.y0 + item.y1) / 2 for item in bands[-1]) / len(bands[-1])
        if abs(center - previous_center) <= 3.2:
            bands[-1].append(line)
        else:
            bands.append([line])
    return bands


def band_columns(band: list[PositionedText], boundaries: list[float]) -> list[str]:
    columns: list[list[PositionedText]] = [[] for _ in range(len(boundaries) - 1)]
    for line in band:
        index = column_index((line.x0 + line.x1) / 2, boundaries)
        if index is not None:
            columns[index].append(line)
    return [" ".join(item.text for item in sorted(column, key=lambda value: value.x0)) for column in columns]


def comparison_headers(expected: str) -> list[str]:
    return ["현행", "개정안", "비고"] if expected == "three_column" else ["현행", "개정안"]


def column_index(center: float, boundaries: list[float]) -> int | None:
    for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
        if left <= center <= right:
            return index
    return None


def cluster_values(values: list[float], *, tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and abs(value - sum(groups[-1]) / len(groups[-1])) <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def normalize_header(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value)


def walk_layout(item):
    yield item
    if hasattr(item, "__iter__"):
        for child in item:
            yield from walk_layout(child)
