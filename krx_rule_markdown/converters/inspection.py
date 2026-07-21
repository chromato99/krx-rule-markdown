from __future__ import annotations

from pathlib import Path
import zipfile

from ..models import Asset
from .base import ConversionDiagnostic
from .cache import SourceInspectionCache
from .pdf import has_structured_table, looks_like_amendment_comparison


PDF_SPARSE_NON_SPACE_PER_PAGE = 80
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".wmf", ".emf", ".tif", ".tiff"}


def inspect_converted_source(
    path: Path,
    text: str,
    *,
    pdf_pages: int | None = None,
    inspection_cache: SourceInspectionCache | None = None,
    assets: list[Asset] | None = None,
) -> tuple[list[ConversionDiagnostic], bool]:
    suffix = path.suffix.lower()
    diagnostics: list[ConversionDiagnostic] = []
    searchable = bool(text.strip())
    if suffix == ".pdf":
        page_count, error = (pdf_pages, "") if pdf_pages else count_pdf_pages(path)
        if error:
            diagnostics.append(ConversionDiagnostic("source_inspection_failed", error))
        page_count = max(1, int(page_count or 1))
        non_space = sum(1 for char in text if not char.isspace())
        searchable = non_space / page_count >= PDF_SPARSE_NON_SPACE_PER_PAGE
        if not searchable:
            diagnostics.extend(
                [
                    ConversionDiagnostic(
                        "pdf_text_layer_too_sparse",
                        f"PDF has {non_space} non-space text characters across {page_count} page(s)",
                    ),
                    ConversionDiagnostic("image_content_unindexed", "PDF is preserved but image content is not searchable"),
                ]
            )
        if looks_like_amendment_comparison(text) and not has_structured_table(text):
            diagnostics.append(
                ConversionDiagnostic(
                    "pdf_comparison_structure_lost",
                    "amendment comparison columns were extracted without a structured table",
                )
            )
    elif suffix == ".hwp":
        picture_count, anchors, error = hwp_picture_count(path, inspection_cache)
        preserved_anchors = {
            asset.source_anchor
            for asset in (assets or [])
            if asset.preservation_status == "preserved" and asset.path
        }
        missing_count = sum(1 for anchor in anchors if anchor not in preserved_anchors)
        if missing_count:
            diagnostics.extend(
                [
                    ConversionDiagnostic("hwp_picture_missing", f"HWP contains {missing_count} unexported picture asset(s)"),
                    ConversionDiagnostic("image_content_unindexed", "HWP picture content is not text searchable"),
                ]
            )
        elif picture_count:
            diagnostics.append(
                ConversionDiagnostic("image_content_unindexed", "preserved HWP picture content is not text searchable")
            )
        if error:
            diagnostics.append(ConversionDiagnostic("source_inspection_failed", error))
    elif suffix == ".hwpx":
        picture_count, error = hwpx_picture_count(path)
        if picture_count:
            diagnostics.extend(
                [
                    ConversionDiagnostic("hwp_picture_missing", f"HWPX contains {picture_count} unexported picture asset(s)"),
                    ConversionDiagnostic("image_content_unindexed", "HWPX picture content is not text searchable"),
                ]
            )
        if error:
            diagnostics.append(ConversionDiagnostic("source_inspection_failed", error))
    return diagnostics, searchable


def count_pdf_pages(path: Path) -> tuple[int, str]:
    try:
        from pdfminer.pdfpage import PDFPage
    except ImportError:
        return 0, "pdfminer is unavailable for PDF page inspection"
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in PDFPage.get_pages(fh, check_extractable=False)), ""
    except Exception as exc:  # noqa: BLE001 - source inspection must be observable.
        return 0, f"PDF page inspection failed: {exc}"


def hwp_picture_count(
    path: Path,
    inspection_cache: SourceInspectionCache | None = None,
) -> tuple[int, list[str], str]:
    cache = inspection_cache or SourceInspectionCache()
    streams, error = cache.hwp_images(path)
    anchors = [stream.source_anchor for stream in streams]
    return len(streams), anchors, error


def hwpx_picture_count(path: Path) -> tuple[int, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(1 for info in archive.infolist() if Path(info.filename.lower()).suffix in IMAGE_SUFFIXES), ""
    except (OSError, zipfile.BadZipFile) as exc:
        return 0, f"HWPX picture inspection failed: {exc}"


def is_image_header(header: bytes) -> bool:
    if header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM", b"\xd7\xcd\xc6\x9a")):
        return True
    return len(header) >= 44 and header[:4] == b"\x01\x00\x00\x00" and header[40:44] == b" EMF"
