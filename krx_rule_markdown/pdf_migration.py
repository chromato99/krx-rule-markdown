from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .convert import convert_attachment
from .converters.pdf_comparison import KNOWN_COMPARISON_PDFS, classify_comparison_pdf
from .markdown import load_documents, write_document
from .models import ATTACHMENT_CONVERTED, now_utc
from .quality import _audit_data_quality, write_manifest, write_quality_report
from .repository import CorpusMutationError, atomic_write_json, mutate_staged_corpus


@dataclass
class PDFComparisonMigrationResult:
    classified: int = 0
    restored: int = 0
    degraded: int = 0
    converted: int = 0
    failed: int = 0


def migrate_pdf_comparisons(data_dir: Path, *, apply: bool = False) -> PDFComparisonMigrationResult:
    if not apply:
        return classify_pdf_comparisons_in_place(Path(data_dir), apply=False)
    return mutate_staged_corpus(
        data_dir,
        "pdf-comparisons",
        lambda staging: classify_pdf_comparisons_in_place(staging, apply=True),
    )


def classify_pdf_comparisons_in_place(data_dir: Path, *, apply: bool) -> PDFComparisonMigrationResult:
    docs = load_documents(data_dir)
    result = PDFComparisonMigrationResult()
    classifications = []
    changed_docs = []
    for doc in docs:
        changed = False
        for att in doc.attachments:
            if att.id not in KNOWN_COMPARISON_PDFS or not att.raw_path:
                continue
            classification = classify_comparison_pdf(Path(data_dir) / att.raw_path, att.id)
            classifications.append(classification.to_mapping())
            result.classified += 1
            if classification.status == "restored":
                result.restored += 1
            else:
                result.degraded += 1
            if not apply or classification.status != "restored":
                continue
            if not att.text_path:
                result.failed += 1
                continue
            raw_path = Path(data_dir) / att.raw_path
            text_path = Path(data_dir) / att.text_path
            previous_refresh_failed_at = att.last_refresh_failed_at
            att = convert_attachment(raw_path, text_path, att)
            att.raw_path = raw_path.relative_to(data_dir).as_posix()
            if (
                att.status == ATTACHMENT_CONVERTED
                and text_path.is_file()
                and not (
                    att.last_refresh_error
                    and att.last_refresh_failed_at != previous_refresh_failed_at
                )
            ):
                att.text_path = text_path.relative_to(data_dir).as_posix()
                result.converted += 1
                changed = True
            else:
                result.failed += 1
        if changed:
            write_document(data_dir, doc)
            changed_docs.append(doc)
    if apply and result.failed:
        raise CorpusMutationError(
            f"PDF comparison migration aborted after {result.failed} conversion failure(s)"
        )
    if changed_docs:
        write_manifest(data_dir, docs)
        quality_report = _audit_data_quality(data_dir, update_metadata=True, release_gate=False)
        write_quality_report(Path(data_dir) / "reports" / "data-quality.json", quality_report)
    report = {
        "version": "1",
        "generated_at": now_utc(),
        "classification_set": sorted(KNOWN_COMPARISON_PDFS),
        "summary": {
            "classified": result.classified,
            "restored": result.restored,
            "degraded": result.degraded,
            "converted": result.converted,
            "failed": result.failed,
        },
        "documents": classifications,
    }
    if apply:
        atomic_write_json(Path(data_dir) / "reports" / "pdf-comparison-classification.json", report)
    return result
