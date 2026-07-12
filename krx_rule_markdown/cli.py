from __future__ import annotations

from pathlib import Path
import argparse
import os
import sys

from .clean import (
    clean_unreferenced_documents,
    clean_unreferenced_attachments,
    drop_past_rule_attachments,
    drop_professional_attachments,
)
from .asset_migration import migrate_assets
from .pdf_migration import migrate_pdf_comparisons
from .collector import DEFAULT_BASE_URL
from .quality import audit_data_quality, write_quality_report
from .manifest import manifest_allowed_failure_ids
from .reconvert import reconvert_data
from .repository import CorpusMutationError, WriterLockError
from .sync import sync_rules
from .validate import validate_data


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (CorpusMutationError, WriterLockError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krx-rule-markdown")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Collect KRX rules and write Markdown corpus data.")
    sync_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    sync_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sync_parser.add_argument("--limit", type=int, default=0)
    sync_parser.add_argument("--rule-id", default="")
    sync_parser.add_argument("--recent-only", action="store_true")
    sync_parser.add_argument("--download-attachments", action="store_true")
    sync_parser.add_argument(
        "--allow-failure-id",
        action="append",
        default=[],
        help="Allow a named optional attachment to degrade only when its raw file is preserved.",
    )
    sync_parser.add_argument(
        "--language",
        choices=("all", "ko", "en"),
        default=os.getenv("KRX_SYNC_LANGUAGE", "all"),
        help="Select corpus language to collect. Default: all.",
    )
    sync_parser.add_argument("--all", action="store_true", help="Collect all current rules and notices, including attachments.")

    validate_parser = subparsers.add_parser("validate", help="Validate Markdown/frontmatter/attachment references.")
    validate_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    validate_parser.add_argument("--quality", action="store_true", help="Also run data-quality checks and fail on quality errors.")
    validate_parser.add_argument("--release", action="store_true", help="Require a complete schema-v2 release manifest and hashes.")
    validate_parser.add_argument("--allow-failure-id", action="append", default=[])

    quality_parser = subparsers.add_parser("quality", help="Audit converted attachment and corpus quality.")
    quality_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    quality_parser.add_argument("--output", default=os.getenv("KRX_QUALITY_REPORT", ""))
    quality_parser.add_argument("--update-metadata", action="store_true")
    quality_parser.add_argument("--fail-on", choices=("none", "error", "warn"), default="none")
    quality_parser.add_argument("--allow-failure-id", action="append", default=[])

    reconvert_parser = subparsers.add_parser("reconvert", help="Rebuild converted attachment Markdown from existing raw files.")
    reconvert_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    reconvert_parser.add_argument("--document-id", default="", help="Only reconvert one document id or source_id.")
    reconvert_parser.add_argument("--dry-run", action="store_true")
    reconvert_parser.add_argument("--force", action="store_true", help="Reconvert even when source hash and converter version match.")

    assets_parser = subparsers.add_parser("assets", help="Preserve bundle-local HTML and HWP image assets.")
    assets_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    assets_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    assets_parser.add_argument("--document-id", default="")
    assets_parser.add_argument("--download-inline", action="store_true")
    assets_parser.add_argument("--dry-run", action="store_true")

    pdf_parser = subparsers.add_parser("pdf-comparisons", help="Classify or restore the named amendment comparison PDF set.")
    pdf_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    pdf_parser.add_argument("--apply", action="store_true")

    clean_parser = subparsers.add_parser("clean", help="Clean generated corpus artifacts.")
    clean_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    clean_parser.add_argument("--drop-professional-attachments", action="store_true")
    clean_parser.add_argument(
        "--drop-past-rule-attachments",
        action="store_true",
        help="Drop current-rule attachments that are past revision history, while keeping future notices.",
    )
    clean_parser.add_argument("--prune-unreferenced-attachments", action="store_true")
    clean_parser.add_argument("--prune-unreferenced-documents", action="store_true")
    clean_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "sync":
        return sync_rules(
            data_dir=Path(args.data_dir),
            base_url=args.base_url,
            limit=args.limit,
            rule_id=args.rule_id,
            recent_only=args.recent_only,
            download_attachments=args.download_attachments or args.all,
            language=args.language,
            allowed_failure_ids=set(args.allow_failure_id),
        )
    if args.command == "validate":
        errors = validate_data(Path(args.data_dir), release_mode=args.release or args.quality)
        if args.quality:
            requested_ids = set(args.allow_failure_id)
            profile_ids = manifest_allowed_failure_ids(Path(args.data_dir))
            if requested_ids and requested_ids != (profile_ids or set()):
                errors.append("--allow-failure-id must exactly match release_profile.allowed_failure_ids")
            report = audit_data_quality(
                Path(args.data_dir),
                allowed_failure_ids=profile_ids,
                release_gate=True,
            )
            errors.extend(quality_failures(report, "error"))
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            print(f"validation failed with {len(errors)} error(s)", file=sys.stderr)
            return 1
        print("validation ok")
        return 0
    if args.command == "quality":
        requested_ids = set(args.allow_failure_id)
        profile_ids = manifest_allowed_failure_ids(Path(args.data_dir))
        if requested_ids and requested_ids != (profile_ids or set()):
            print("error: --allow-failure-id must exactly match release_profile.allowed_failure_ids", file=sys.stderr)
            return 1
        report = audit_data_quality(
            Path(args.data_dir),
            update_metadata=args.update_metadata,
            allowed_failure_ids=profile_ids if profile_ids is not None else (requested_ids or None),
            release_gate=args.fail_on != "none",
            fail_on=args.fail_on,
        )
        output = Path(args.output) if args.output else Path(args.data_dir) / "reports" / "data-quality.json"
        write_quality_report(output, report)
        summary = report["summary"]
        print(
            "quality "
            f"documents={summary['documents']} "
            f"attachments={summary['attachments']} "
            f"status={summary['quality_status']} "
            f"issues={len(report['issues'])} "
            f"report={output}"
        )
        failures = quality_failures(report, args.fail_on)
        if failures:
            for failure in failures:
                print(failure, file=sys.stderr)
            print(f"quality failed with {len(failures)} issue(s)", file=sys.stderr)
            return 1
        return 0
    if args.command == "reconvert":
        result = reconvert_data(
            Path(args.data_dir),
            document_id=args.document_id,
            dry_run=args.dry_run,
            force=args.force,
        )
        action = "would convert" if args.dry_run else "converted"
        print(
            "reconvert "
            f"documents={result.documents} "
            f"attachments={result.attachments} "
            f"{action}={result.converted} "
            f"failed={result.failed} "
            f"skipped={result.skipped}"
        )
        return 1 if result.failed else 0
    if args.command == "assets":
        result = migrate_assets(
            Path(args.data_dir),
            base_url=args.base_url,
            download_inline=args.download_inline,
            document_id=args.document_id,
            dry_run=args.dry_run,
        )
        print(
            "assets "
            f"documents={result.documents} "
            f"inline_candidates={result.inline_candidates} "
            f"hwp_attachments={result.hwp_attachments} "
            f"preserved={result.preserved_assets} "
            f"failed_assets={result.failed_assets} "
            f"missing_assets={result.missing_assets} "
            f"failed_sources={result.failed_sources} "
            f"pruned={result.pruned_assets}"
        )
        return 1 if result.failed_sources or (args.download_inline and result.failed_assets) else 0
    if args.command == "pdf-comparisons":
        result = migrate_pdf_comparisons(Path(args.data_dir), apply=args.apply)
        print(
            "pdf-comparisons "
            f"classified={result.classified} "
            f"restored={result.restored} "
            f"degraded={result.degraded} "
            f"converted={result.converted} "
            f"failed={result.failed}"
        )
        return 1 if result.failed else 0
    if args.command == "clean":
        did_work = False
        if args.drop_professional_attachments:
            did_work = True
            result = drop_professional_attachments(Path(args.data_dir), dry_run=args.dry_run)
            action = "would drop" if args.dry_run else "dropped"
            print(f"clean professional_attachments documents={result.documents} {action}={result.removed}")
        if args.drop_past_rule_attachments:
            did_work = True
            result = drop_past_rule_attachments(Path(args.data_dir), dry_run=args.dry_run)
            action = "would drop" if args.dry_run else "dropped"
            print(f"clean past_rule_attachments documents={result.documents} {action}={result.removed}")
        if args.prune_unreferenced_attachments:
            did_work = True
            result = clean_unreferenced_attachments(Path(args.data_dir), dry_run=args.dry_run)
            action = "would remove" if args.dry_run else "removed"
            print(f"clean unreferenced_attachments scanned={result.scanned} {action}={result.removed}")
        if args.prune_unreferenced_documents:
            did_work = True
            result = clean_unreferenced_documents(Path(args.data_dir), dry_run=args.dry_run)
            action = "would remove" if args.dry_run else "removed"
            print(f"clean unreferenced_documents scanned={result.scanned} {action}={result.removed}")
        if not did_work:
            print(
                "nothing to clean; pass --drop-past-rule-attachments, "
                "--drop-professional-attachments, --prune-unreferenced-attachments, "
                "or --prune-unreferenced-documents",
                file=sys.stderr,
            )
            return 2
        return 0
    return 2


def quality_failures(report: dict, fail_on: str) -> list[str]:
    if fail_on == "none":
        return []
    allowed = {"error"} if fail_on == "error" else {"error", "warn"}
    failures = []
    for item in report.get("issues", []):
        if item.get("severity") in allowed:
            failures.append(
                f"{item.get('severity')}: {item.get('code')} "
                f"{item.get('document_id')}/{item.get('attachment_id')}: {item.get('message')}"
            )
    return failures
