from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .assets import INLINE_IMAGE_RE, preserve_hwp_attachment_assets, preserve_inline_document_assets
from .collector import Client, DEFAULT_BASE_URL
from .contracts import add_quality_code
from .converters.cache import SourceInspectionCache
from .markdown import load_documents, write_document
from .quality import _audit_data_quality, write_manifest, write_quality_report
from .repository import CorpusMutationError, mutate_staged_corpus


@dataclass
class AssetMigrationResult:
    documents: int = 0
    inline_candidates: int = 0
    hwp_attachments: int = 0
    preserved_assets: int = 0
    failed_assets: int = 0
    missing_assets: int = 0
    failed_sources: int = 0
    pruned_assets: int = 0


def migrate_assets(
    data_dir: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    download_inline: bool = False,
    document_id: str = "",
    dry_run: bool = False,
) -> AssetMigrationResult:
    if dry_run:
        return inspect_asset_candidates(Path(data_dir), document_id=document_id)
    return mutate_staged_corpus(
        data_dir,
        "assets",
        lambda staging: migrate_assets_in_place(
            staging,
            base_url=base_url,
            download_inline=download_inline,
            document_id=document_id,
        ),
    )


def inspect_asset_candidates(data_dir: Path, *, document_id: str = "") -> AssetMigrationResult:
    result = AssetMigrationResult()
    for doc in load_documents(data_dir):
        if document_id and doc.id != document_id and doc.source_id != document_id:
            continue
        result.documents += 1
        result.inline_candidates += len(INLINE_IMAGE_RE.findall(doc.body))
        result.hwp_attachments += sum(
            1 for att in doc.attachments if Path(att.raw_path or att.file_name).suffix.lower() == ".hwp"
        )
        count_asset_statuses(result, doc)
    return result


def migrate_assets_in_place(
    data_dir: Path,
    *,
    base_url: str,
    download_inline: bool,
    document_id: str,
) -> AssetMigrationResult:
    docs = load_documents(data_dir)
    client = Client(base_url) if download_inline else None
    result = AssetMigrationResult()
    changed = False
    inspection_cache = SourceInspectionCache()
    for doc in docs:
        if document_id and doc.id != document_id and doc.source_id != document_id:
            continue
        result.documents += 1
        inline_count = len(INLINE_IMAGE_RE.findall(doc.body))
        result.inline_candidates += inline_count
        before = doc.to_mapping()
        if download_inline and (inline_count or doc.assets):
            preserve_inline_document_assets(
                data_dir,
                doc,
                client.download_inline_asset if client is not None else None,
            )
        for att in doc.attachments:
            raw_path = Path(data_dir) / att.raw_path if att.raw_path else None
            if raw_path is None or raw_path.suffix.lower() != ".hwp" or not raw_path.is_file():
                continue
            result.hwp_attachments += 1
            streams, error = inspection_cache.hwp_images(raw_path)
            if error:
                result.failed_sources += 1
                att.quality_codes = add_quality_code(att.quality_codes, "source_inspection_failed")
                diagnostic = {"code": "source_inspection_failed", "message": error, "severity": "warn"}
                if diagnostic not in att.diagnostics:
                    att.diagnostics.append(diagnostic)
                continue
            preserve_hwp_attachment_assets(data_dir, doc, att, streams=streams)
        result.pruned_assets += prune_unreferenced_bundle_assets(data_dir, doc)
        count_asset_statuses(result, doc)
        if doc.to_mapping() != before:
            write_document(data_dir, doc)
            changed = True
    if result.failed_sources:
        raise CorpusMutationError(
            f"asset migration aborted after {result.failed_sources} HWP source inspection failure(s)"
        )
    if download_inline and result.failed_assets:
        raise CorpusMutationError(
            f"asset migration aborted after {result.failed_assets} inline asset download/inspection failure(s)"
        )
    if changed:
        write_manifest(data_dir, docs)
    report = _audit_data_quality(data_dir, update_metadata=True, release_gate=False)
    write_quality_report(Path(data_dir) / "reports" / "data-quality.json", report)
    return result


def count_asset_statuses(result: AssetMigrationResult, doc) -> None:
    assets = [*doc.assets, *(asset for att in doc.attachments for asset in att.assets)]
    result.preserved_assets += sum(asset.preservation_status == "preserved" for asset in assets)
    result.failed_assets += sum(asset.preservation_status == "failed" for asset in assets)
    result.missing_assets += sum(asset.preservation_status == "missing" for asset in assets)


def prune_unreferenced_bundle_assets(data_dir: Path, doc) -> int:
    """Remove stale generated assets inside one owning bundle in staging."""

    if not doc.path:
        return 0
    index_path = Path(doc.path)
    if not index_path.is_absolute():
        index_path = Path(data_dir) / index_path
    assets_root = index_path.parent / "assets"
    if not assets_root.exists():
        return 0
    referenced = {
        asset.path
        for asset in [*doc.assets, *(asset for att in doc.attachments for asset in att.assets)]
        if asset.preservation_status == "preserved" and asset.path
    }
    removed = 0
    for path in sorted(assets_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise CorpusMutationError(f"asset bundle contains a symlink: {path}")
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
            continue
        relative = path.relative_to(data_dir).as_posix()
        if relative not in referenced:
            path.unlink()
            removed += 1
    if assets_root.exists() and not any(assets_root.iterdir()):
        assets_root.rmdir()
    return removed
