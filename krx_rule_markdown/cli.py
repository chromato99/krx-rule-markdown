from __future__ import annotations

from pathlib import Path
import argparse
import os
import sys

from .clean import (
    clean_unreferenced_attachments,
    drop_past_rule_attachments,
    drop_professional_attachments,
)
from .collector import DEFAULT_BASE_URL
from .quality import audit_data_quality, write_quality_report
from .sync import sync_rules
from .validate import validate_data


def main(argv: list[str] | None = None) -> int:
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
        "--language",
        choices=("all", "ko", "en"),
        default=os.getenv("KRX_SYNC_LANGUAGE", "all"),
        help="Select corpus language to collect. Default: all.",
    )
    sync_parser.add_argument("--all", action="store_true", help="Collect all current rules and notices, including attachments.")

    validate_parser = subparsers.add_parser("validate", help="Validate Markdown/frontmatter/attachment references.")
    validate_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    validate_parser.add_argument("--quality", action="store_true", help="Also run data-quality checks and fail on quality errors.")

    quality_parser = subparsers.add_parser("quality", help="Audit converted attachment and corpus quality.")
    quality_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    quality_parser.add_argument("--output", default=os.getenv("KRX_QUALITY_REPORT", ""))
    quality_parser.add_argument("--update-metadata", action="store_true")
    quality_parser.add_argument("--fail-on", choices=("none", "error", "warn"), default="none")

    clean_parser = subparsers.add_parser("clean", help="Clean generated corpus artifacts.")
    clean_parser.add_argument("--data-dir", default=os.getenv("KRX_DATA_DIR", "data"))
    clean_parser.add_argument("--drop-professional-attachments", action="store_true")
    clean_parser.add_argument(
        "--drop-past-rule-attachments",
        action="store_true",
        help="Drop current-rule attachments that are past revision history, while keeping future notices.",
    )
    clean_parser.add_argument("--prune-unreferenced-attachments", action="store_true")
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
        )
    if args.command == "validate":
        errors = validate_data(Path(args.data_dir))
        if args.quality:
            report = audit_data_quality(Path(args.data_dir))
            errors.extend(quality_failures(report, "error"))
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            print(f"validation failed with {len(errors)} error(s)", file=sys.stderr)
            return 1
        print("validation ok")
        return 0
    if args.command == "quality":
        report = audit_data_quality(Path(args.data_dir), update_metadata=args.update_metadata)
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
        if not did_work:
            print(
                "nothing to clean; pass --drop-past-rule-attachments, "
                "--drop-professional-attachments, or --prune-unreferenced-attachments",
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
