from __future__ import annotations

from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib import parse as urlparse
import copy
import json
import re

from .contracts import (
    CONVERSION_STATUSES,
    CORPUS_SCHEMA_VERSION,
    MAX_CONVERTED_TEXT_BYTES,
    MAX_METADATA_FILE_BYTES,
    MAX_SOURCE_BYTES,
    PRESERVATION_STATUSES,
    QUALITY_CODES,
    canonical_json_hash,
    canonical_text_hash,
    effective_searchable,
    index_source_hash,
    parse_quality_codes,
    release_hash,
    read_utf8_file_bounded,
    sha256_bytes,
    sha256_file,
    status_combination_errors,
)
from .markdown import document_paths, document_roots, parse_frontmatter, parse_markdown
from .models import Asset, Document, hash_text
from .assets import (
    MAX_ASSET_BYTES,
    MAX_ASSET_DIMENSION,
    MAX_ASSET_PIXELS,
    AssetError,
    inspect_image,
)


SHA256_RE = re.compile(r"[0-9a-f]{64}")
HWP_SOURCE_BLOCK_RE = re.compile(r"^```hwp-equation\s*$", re.MULTILINE)
MAX_SOURCE_REQUEST_BYTES = 64 * 1024
SOURCE_REQUEST_KEYS = {
    "endpoint",
    "bookid",
    "noformyn",
    "statehistoryid",
    "BBSID",
    "Menuid",
    "source_content_hash",
}
SOURCE_REQUEST_SECRET_KEYS = {
    "cookie",
    "cookies",
    "csrf",
    "_csrf",
    "x-csrf-token",
    "authorization",
}


def validate_data(data_dir: Path, *, release_mode: bool = False) -> list[str]:
    data_dir = Path(data_dir)
    docs, errors = load_documents_for_validation(data_dir)
    errors.extend(validate_manifest_document_paths(data_dir, docs, release_mode=release_mode))

    seen_ids: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for doc in docs:
        location = doc.path or doc.id
        if release_mode and doc.schema_version < CORPUS_SCHEMA_VERSION:
            errors.append(
                f"{location}: schema_version {CORPUS_SCHEMA_VERSION} is required for release document"
            )
        previous = seen_ids.get(doc.id)
        if previous:
            errors.append(f"{location}: duplicate_document_id {doc.id} (already in {previous})")
        else:
            seen_ids[doc.id] = location
        errors.extend(validate_document(data_dir, doc, seen_ids, seen_paths))
    return errors


def load_documents_for_validation(data_dir: Path) -> tuple[list[Document], list[str]]:
    docs: list[Document] = []
    errors: list[str] = []
    seen_paths: set[Path] = set()
    for directory_language, base in document_roots(data_dir):
        if not base.exists():
            continue
        for path in document_paths(base):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                doc = parse_markdown(
                    read_utf8_file_bounded(path, max_bytes=MAX_METADATA_FILE_BYTES)
                )
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                errors.append(f"{path}: invalid document: {exc}")
                continue
            doc.path = str(path)
            doc.directory_language = directory_language
            if not doc.language:
                doc.language = directory_language
            docs.append(doc)
    return docs, errors


def validate_document(
    data_dir: Path,
    doc: Document,
    seen_ids: dict[str, str],
    seen_paths: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    location = doc.path or doc.id
    if not doc.id:
        errors.append(f"{location}: id is required")
    if not doc.title:
        errors.append(f"{location}: title is required")
    if doc.document_type not in {"rule", "notice"}:
        errors.append(f"{location}: document_type must be rule or notice")
    if not doc.source_url:
        errors.append(f"{location}: source_url is required")
    elif not valid_absolute_http_url(doc.source_url):
        errors.append(
            f"{location}: source_url must be absolute HTTP(S) without credentials or control characters"
        )
    if not portable_file_name(doc.file_name):
        errors.append(f"{location}: file_name must be a portable basename")
    if not doc.collected_at:
        errors.append(f"{location}: collected_at is required")
    elif not valid_datetime(doc.collected_at):
        errors.append(f"{location}: collected_at must be an RFC3339 timestamp with timezone")
    for field_name in ("effective_date", "published_date"):
        value = getattr(doc, field_name)
        if value and not valid_date(value):
            errors.append(f"{location}: {field_name} must be YYYY-MM-DD")
    if not doc.declared_language:
        errors.append(f"{location}: language is required")
    if doc.language not in {"ko", "en"}:
        errors.append(f"{location}: language must be ko or en")
    if doc.directory_language and doc.language != doc.directory_language:
        errors.append(
            f"{location}: language {doc.language!r} does not match directory {doc.directory_language!r}"
        )
    if doc.schema_version not in {1, CORPUS_SCHEMA_VERSION}:
        errors.append(f"{location}: unsupported schema_version {doc.schema_version}")
    errors.extend(validate_document_directory_type(doc))
    errors.extend(
        f"{location}: invalid_status_combination: {message}"
        for message in status_combination_errors(
            conversion_status=doc.conversion_status,
            preservation_status=doc.preservation_status,
            searchable=doc.searchable,
            quality_status=doc.quality_status,
        )
    )
    errors.extend(validate_quality_codes(location, doc.quality_codes))
    if doc.preservation_status in {"missing", "failed"}:
        errors.append(f"{location}: required_source_missing")

    expected_body_hash = canonical_text_hash(doc.body)
    if doc.body_hash:
        errors.extend(validate_hash(location, "body_hash", doc.body_hash))
        if doc.body_hash != expected_body_hash:
            errors.append(f"{location}: body_hash_mismatch")
    elif doc.schema_version >= CORPUS_SCHEMA_VERSION:
        errors.append(f"{location}: body_hash is required for schema v{CORPUS_SCHEMA_VERSION}")
    if doc.content_hash:
        errors.extend(validate_hash(location, "content_hash", doc.content_hash))
        if doc.content_hash != hash_text(doc.title + "\n" + doc.body):
            errors.append(f"{location}: legacy content_hash mismatch")
    elif not doc.body_hash:
        errors.append(f"{location}: body_hash or content_hash is required")

    bundle = Path(doc.path).parent if doc.path else data_dir
    provenance_values = (
        doc.source_content_hash,
        doc.source_content_path,
        doc.source_request_path,
    )
    if any(provenance_values) and not all(provenance_values):
        errors.append(
            f"{location}: source_content_hash, source_content_path, and source_request_path "
            "must be provided together"
        )
    for field_name, hash_name in (
        ("source_content_path", "source_content_hash"),
        ("source_request_path", ""),
        ("raw_path", "raw_file_hash"),
        ("text_path", ""),
    ):
        relative = getattr(doc, field_name)
        if not relative:
            continue
        resolved, path_errors = validate_corpus_path(data_dir, bundle, relative, f"{location}: {field_name}")
        errors.extend(path_errors)
        if resolved is None or not resolved.exists():
            errors.append(f"{location}: missing {field_name} {relative}")
            continue
        register_path(errors, seen_paths, resolved, f"{location}:{field_name}")
        if field_name == "source_request_path":
            errors.extend(validate_request_descriptor(resolved, location, doc))
            continue
        declared_hash = getattr(doc, hash_name) if hash_name else ""
        if field_name == "raw_path" and not declared_hash:
            declared_hash = doc.file_content_hash
        if declared_hash:
            errors.extend(validate_hash(location, hash_name, declared_hash))
            if field_name == "source_content_path":
                source_text, decode_errors = read_utf8_strict(resolved, f"{location}: {field_name}")
                errors.extend(decode_errors)
                if source_text is None:
                    continue
                actual = canonical_text_hash(source_text)
            else:
                try:
                    actual = sha256_file(resolved, max_bytes=MAX_SOURCE_BYTES)
                except ValueError as exc:
                    errors.append(f"{location}: invalid {field_name}: {exc}")
                    continue
            if declared_hash != actual:
                errors.append(f"{location}: {hash_name}_mismatch")
        elif field_name in {"source_content_path", "raw_path"}:
            errors.append(f"{location}: {hash_name} is required when {field_name} is present")

    for asset in doc.assets:
        errors.extend(
            validate_asset(
                data_dir,
                bundle,
                asset,
                seen_ids,
                seen_paths,
                f"{location}: asset",
                doc.source_url,
            )
        )
    for att in doc.attachments:
        errors.extend(validate_attachment(data_dir, bundle, doc, att, seen_ids, seen_paths))
    return errors


def validate_attachment(data_dir, bundle, doc, att, seen_ids, seen_paths) -> list[str]:
    errors: list[str] = []
    location = f"{doc.path}: attachment {att.id or '(missing id)'}"
    if not att.id:
        errors.append(f"{location}: id is required")
    elif att.id in seen_ids:
        errors.append(
            f"{location}: duplicate_attachment_id {att.id} (already in {seen_ids[att.id]})"
        )
    else:
        seen_ids[att.id] = location
    if not safe_attachment_source_url(att.source_url):
        errors.append(
            f"{location}: source_url must be a safe HTTP(S) URL or supported KRX endpoint"
        )
    if not portable_file_name(att.file_name):
        errors.append(f"{location}: file_name must be a portable basename")
    if att.status not in CONVERSION_STATUSES:
        errors.append(f"{location}: invalid conversion_status {att.status!r}")
    if att.preservation_status and att.preservation_status not in PRESERVATION_STATUSES:
        errors.append(f"{location}: invalid preservation_status {att.preservation_status!r}")
    errors.extend(
        f"{location}: invalid_status_combination: {message}"
        for message in status_combination_errors(
            conversion_status=att.status,
            preservation_status=att.preservation_status,
            searchable=att.searchable,
            quality_status=att.quality_status,
        )
    )
    errors.extend(validate_quality_codes(location, att.quality_codes or att.quality_flags))
    if att.status == "converted" and not att.text_path:
        errors.append(f"{location}: converted attachment has no text_path")
    if att.status == "failed" and att.text_path:
        errors.append(f"{location}: failed attachment must not expose text_path")
    if effective_searchable(att) and att.status != "converted":
        errors.append(f"{location}: only converted attachments may be searchable")

    raw_path: Path | None = None
    text_path: Path | None = None
    converted_text: str | None = None
    for field_name in ("raw_path", "text_path"):
        relative = getattr(att, field_name)
        if not relative:
            continue
        resolved, path_errors = validate_corpus_path(data_dir, bundle, relative, f"{location}: {field_name}")
        errors.extend(path_errors)
        if resolved is None or not resolved.exists():
            errors.append(f"{location}: missing {field_name} {relative}")
            continue
        register_path(errors, seen_paths, resolved, f"{location}:{field_name}")
        if field_name == "raw_path":
            raw_path = resolved
        else:
            text_path = resolved
    declared_raw_hash = att.raw_file_hash or att.content_hash
    if declared_raw_hash:
        errors.extend(validate_hash(location, "raw_file_hash", declared_raw_hash))
        if raw_path is not None:
            try:
                actual_raw_hash = sha256_file(raw_path, max_bytes=MAX_SOURCE_BYTES)
            except ValueError as exc:
                errors.append(f"{location}: invalid raw_path: {exc}")
            else:
                if declared_raw_hash != actual_raw_hash:
                    errors.append(f"{location}: raw_file_hash_mismatch")
    elif raw_path is not None:
        errors.append(f"{location}: raw_file_hash is required when raw_path is present")
    if att.raw_file_hash and att.content_hash and att.raw_file_hash != att.content_hash:
        errors.append(f"{location}: raw_file_hash and legacy content_hash differ")
    if att.converted_text_hash:
        errors.extend(validate_hash(location, "converted_text_hash", att.converted_text_hash))
        if text_path is not None:
            converted_text, decode_errors = read_utf8_strict(text_path, f"{location}: text_path")
            errors.extend(decode_errors)
            if converted_text is not None:
                actual = canonical_text_hash(converted_text)
                if att.converted_text_hash != actual:
                    errors.append(f"{location}: converted_text_hash_mismatch")
    elif doc.schema_version >= CORPUS_SCHEMA_VERSION and att.status == "converted" and att.text_path:
        errors.append(f"{location}: converted_text_hash is required for schema v{CORPUS_SCHEMA_VERSION}")
    if raw_path is not None and text_path is not None and raw_path.suffix.lower() == ".hwp":
        if converted_text is None:
            converted_text, decode_errors = read_utf8_strict(text_path, f"{location}: text_path")
            errors.extend(decode_errors)
        if converted_text is not None:
            from .quality import hwp_structure_counts

            _, _, raw_equation_count, inspection_error = hwp_structure_counts(raw_path)
            source_count = len(HWP_SOURCE_BLOCK_RE.findall(converted_text))
            if not inspection_error and raw_equation_count != source_count:
                errors.append(
                    f"{location}: formula_source_count_mismatch "
                    f"raw_eqedit={raw_equation_count} converted_source={source_count}"
                )
    for asset in att.assets:
        errors.extend(
            validate_asset(
                data_dir,
                bundle,
                asset,
                seen_ids,
                seen_paths,
                f"{location}: asset",
                doc.source_url,
            )
        )
    return errors


def validate_asset(
    data_dir: Path,
    bundle: Path,
    asset: Asset,
    seen_ids: dict[str, str],
    seen_paths: dict[str, str],
    owner: str,
    document_source_url: str,
) -> list[str]:
    location = f"{owner} {asset.id or '(missing id)'}"
    errors: list[str] = []
    if not asset.id or asset.id != asset.id.strip():
        errors.append(f"{location}: id is required")
    elif asset.id in seen_ids:
        errors.append(f"{location}: duplicate_asset_id {asset.id} (already in {seen_ids[asset.id]})")
    else:
        seen_ids[asset.id] = location
    if asset.source_kind not in {"html_inline", "hwp_bindata"}:
        errors.append(f"{location}: invalid asset source_kind {asset.source_kind!r}")
    anchor = asset.source_anchor
    if (
        not anchor
        or anchor != anchor.strip()
        or len(anchor) > 4096
        or any(char in anchor for char in "\x00\r\n")
    ):
        errors.append(f"{location}: source_anchor is required as a bounded single-line value")
    if asset.source_kind == "html_inline":
        source_url = asset.source_url
        if (
            not source_url
            or source_url != source_url.strip()
            or len(source_url) > 4096
            or not valid_absolute_http_url(source_url)
        ):
            errors.append(f"{location}: html_inline source_url must be absolute HTTP(S)")
        else:
            parsed_source = urlparse.urlparse(source_url)
            parsed_owner = urlparse.urlparse(document_source_url)
            if (
                parsed_source.scheme.lower() != parsed_owner.scheme.lower()
                or parsed_source.netloc.lower() != parsed_owner.netloc.lower()
            ):
                errors.append(f"{location}: html_inline source_url must use document source origin")
            if (
                not parsed_source.path.startswith("/dataFile/law/img/")
                or bool(parsed_source.fragment)
            ):
                errors.append(
                    f"{location}: html_inline source_url must use /dataFile/law/img/ without fragment"
                )
        if anchor != f"html-img:{asset.source_url}":
            errors.append(
                f"{location}: html_inline source_anchor must equal html-img:<source_url>"
            )
    elif asset.source_kind == "hwp_bindata":
        if asset.source_url:
            errors.append(f"{location}: hwp_bindata source_url is forbidden")
        prefix = "hwp:BinData/"
        has_prefix = anchor.lower().startswith(prefix.lower())
        stream = anchor[len(prefix) :] if has_prefix else ""
        stream_path = PurePosixPath(stream)
        if (
            not stream
            or not has_prefix
            or stream.startswith("/")
            or "\\" in stream
            or stream_path.as_posix() != stream
            or ".." in stream_path.parts
            or any(not part or part == "." for part in stream_path.parts)
        ):
            errors.append(f"{location}: invalid hwp_bindata source_anchor")
    if asset.searchable is not False:
        errors.append(f"{location}: binary assets require searchable=false")
    errors.extend(validate_quality_codes(location, asset.quality_codes))
    if "image_content_unindexed" not in parse_quality_codes(asset.quality_codes):
        errors.append(f"{location}: image_content_unindexed quality code is required")
    if asset.preservation_status not in PRESERVATION_STATUSES:
        errors.append(f"{location}: invalid preservation_status {asset.preservation_status!r}")
        return errors
    if asset.preservation_status != "preserved":
        if any(
            (
                asset.path,
                asset.mime_type,
                asset.raw_file_hash,
                asset.size,
                asset.width,
                asset.height,
            )
        ):
            errors.append(f"{location}: non-preserved asset must not expose file metadata")
        required_code = (
            "inline_image_missing" if asset.source_kind == "html_inline" else "hwp_picture_missing"
        )
        if required_code not in parse_quality_codes(asset.quality_codes):
            errors.append(f"{location}: {required_code} quality code is required")
        if asset.preservation_status == "failed" and not asset.error.strip():
            errors.append(f"{location}: failed asset requires error")
        return errors
    if asset.error.strip():
        errors.append(f"{location}: preserved asset must not contain error")
    for field_name, value in (
        ("path", asset.path),
        ("mime_type", asset.mime_type),
        ("raw_file_hash", asset.raw_file_hash),
    ):
        if not value or value != value.strip():
            errors.append(f"{location}: {field_name} is required for preserved asset")
    if asset.size <= 0 or asset.width <= 0 or asset.height <= 0:
        errors.append(f"{location}: positive size, width, and height are required for preserved asset")
    if asset.size > MAX_ASSET_BYTES:
        errors.append(f"{location}: asset size exceeds {MAX_ASSET_BYTES} bytes")
    if (
        asset.width > MAX_ASSET_DIMENSION
        or asset.height > MAX_ASSET_DIMENSION
        or asset.width * asset.height > MAX_ASSET_PIXELS
    ):
        errors.append(f"{location}: asset dimensions exceed contract bounds")
    if not asset.path:
        return errors
    resolved, path_errors = validate_corpus_path(data_dir, bundle, asset.path, f"{location}: path")
    errors.extend(path_errors)
    if resolved is None or not resolved.is_file():
        errors.append(f"{location}: missing asset path {asset.path}")
        return errors
    register_path(errors, seen_paths, resolved, f"{location}:path")
    if not resolved.is_file():
        errors.append(f"{location}: asset path is not a regular file")
        return errors
    stat = resolved.stat()
    if stat.st_size > MAX_ASSET_BYTES:
        errors.append(f"{location}: asset exceeds {MAX_ASSET_BYTES} bytes")
        return errors
    data = resolved.read_bytes()
    if asset.raw_file_hash:
        errors.extend(validate_hash(location, "raw_file_hash", asset.raw_file_hash))
        if asset.raw_file_hash != sha256_bytes(data):
            errors.append(f"{location}: raw_file_hash_mismatch")
    if asset.size and asset.size != len(data):
        errors.append(f"{location}: asset size mismatch")
    try:
        image = inspect_image(data)
    except AssetError as exc:
        errors.append(f"{location}: invalid image asset: {exc}")
        return errors
    if asset.mime_type and asset.mime_type != image.mime_type:
        errors.append(f"{location}: asset MIME/signature mismatch")
    if asset.width and asset.width != image.width or asset.height and asset.height != image.height:
        errors.append(f"{location}: asset dimensions mismatch")
    return errors


def validate_corpus_path(
    data_dir: Path,
    bundle: Path,
    relative: str,
    location: str,
) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    path = Path(relative)
    if path.is_absolute():
        return None, [f"{location}: path_outside_data_root: absolute paths are forbidden"]
    if ".." in path.parts:
        return None, [f"{location}: path_outside_data_root: parent traversal is forbidden"]
    root = data_dir.resolve()
    candidate = data_dir / path
    if path_contains_symlink(data_dir, path):
        return None, [f"{location}: path_outside_data_root: symlink paths are forbidden"]
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, [*errors, f"{location}: path_outside_data_root"]
    try:
        resolved.relative_to(bundle.resolve())
    except ValueError:
        return None, [*errors, f"{location}: cross-document bundle path is forbidden"]
    return resolved, errors


def read_utf8_strict(path: Path, location: str) -> tuple[str | None, list[str]]:
    try:
        if path.stat().st_size > MAX_CONVERTED_TEXT_BYTES:
            return None, [f"{location}: text exceeds {MAX_CONVERTED_TEXT_BYTES} bytes"]
        return path.read_text(encoding="utf-8", errors="strict"), []
    except UnicodeDecodeError as exc:
        return None, [f"{location}: invalid UTF-8: {exc}"]
    except OSError as exc:
        return None, [f"{location}: could not read file: {exc}"]


def path_contains_symlink(data_dir: Path, relative: Path) -> bool:
    current = data_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def register_path(errors: list[str], seen: dict[str, str], path: Path, owner: str) -> None:
    key = str(path)
    if key in seen and seen[key] != owner:
        errors.append(f"{owner}: path is also referenced by {seen[key]}")
    else:
        seen[key] = owner


def validate_request_descriptor(path: Path, location: str, doc: Document) -> list[str]:
    try:
        if path.stat().st_size > MAX_SOURCE_REQUEST_BYTES:
            return [
                f"{location}: source request descriptor exceeds {MAX_SOURCE_REQUEST_BYTES} bytes"
            ]
        encoded = path.read_bytes()
        text = encoded.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=unique_json_object)
    except UnicodeDecodeError as exc:
        return [f"{location}: invalid source request descriptor UTF-8: {exc}"]
    except (OSError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        return [f"{location}: invalid source request descriptor: {exc}"]
    if not isinstance(payload, dict):
        return [f"{location}: source request descriptor must be an object"]
    if json_nesting_exceeds(payload, 64):
        return [f"{location}: invalid source request descriptor: JSON nesting exceeds 64 levels"]
    errors: list[str] = []
    forbidden = recursive_secret_keys(payload)
    if forbidden:
        errors.append(
            f"{location}: source request descriptor contains secret field(s): "
            f"{', '.join(sorted(forbidden))}"
        )
    unknown = set(payload) - SOURCE_REQUEST_KEYS
    if unknown:
        errors.append(
            f"{location}: source request descriptor contains unknown field(s): "
            f"{', '.join(sorted(unknown))}"
        )
    values: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str):
            errors.append(f"{location}: source request field {key!r} must be a string")
            continue
        normalized = value.strip()
        if len(normalized) > 4096 or any(char in normalized for char in "\x00\r\n"):
            errors.append(f"{location}: source request field {key!r} has an invalid value")
            continue
        values[key] = normalized
    descriptor_hash = values.get("source_content_hash", "")
    if not descriptor_hash:
        errors.append(f"{location}: source request source_content_hash is required")
    elif descriptor_hash != doc.source_content_hash:
        errors.append(f"{location}: request source_content_hash does not match document")
    endpoint = values.get("endpoint", "")
    if endpoint == "/out/regulation/regulationViewPop.do":
        if (
            doc.document_type != "rule"
            or not values.get("bookid")
            or not values.get("noformyn")
            or values.get("BBSID")
            or values.get("Menuid")
        ):
            errors.append(
                f"{location}: rule source request requires bookid, noformyn, and rule document type"
            )
    elif endpoint == "/out/pds/pdsViewPop.do":
        if (
            doc.document_type != "notice"
            or not values.get("BBSID")
            or not values.get("Menuid")
            or values.get("bookid")
            or values.get("noformyn")
            or values.get("statehistoryid")
        ):
            errors.append(
                f"{location}: notice source request requires BBSID, Menuid, and notice document type"
            )
    else:
        errors.append(f"{location}: unsupported source request endpoint {endpoint!r}")
    return errors


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate field {key!r}")
        out[key] = value
    return out


def recursive_secret_keys(value: object) -> set[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = str(key).strip().lower()
                if normalized in SOURCE_REQUEST_SECRET_KEYS:
                    found.add(normalized)
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    return found


def json_nesting_exceeds(value: object, limit: int) -> bool:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > limit:
            return True
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    return False


def validate_document_directory_type(doc: Document) -> list[str]:
    if not doc.path:
        return []
    folder = Path(doc.path).parent.parent.name
    expected = "notices" if doc.document_type == "notice" else "rules"
    if folder != expected:
        return [f"{doc.path}: document_type {doc.document_type!r} does not match directory {folder!r}"]
    return []


def validate_hash(location: str, field_name: str, value: str) -> list[str]:
    if not SHA256_RE.fullmatch(value or ""):
        return [f"{location}: {field_name} must be a lowercase SHA-256 hex digest"]
    return []


def validate_quality_codes(location: str, value) -> list[str]:
    return [
        f"{location}: unknown quality code {code!r}"
        for code in parse_quality_codes(value)
        if code not in QUALITY_CODES
    ]


def valid_absolute_http_url(value: str) -> bool:
    lowered = value.lower()
    if (
        not value
        or value != value.strip()
        or len(value) > 4096
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or any(encoded in lowered for encoded in ("%00", "%0a", "%0d"))
    ):
        return False
    try:
        parsed = urlparse.urlparse(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def safe_attachment_source_url(value: str) -> bool:
    if not value:
        return True
    if valid_absolute_http_url(value):
        return True
    lowered = value.lower()
    if (
        value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(encoded in lowered for encoded in ("%00", "%0a", "%0d"))
    ):
        return False
    try:
        parsed = urlparse.urlparse(value)
    except ValueError:
        return False
    return (
        not parsed.scheme
        and not parsed.netloc
        and not parsed.fragment
        and parsed.path in {"/Download.do", "/out/pds/pdsViewPop.do"}
    )


def portable_file_name(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return not (
        value != value.strip()
        or value in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(encoded in lowered for encoded in ("%00", "%0a", "%0d"))
        or any(char in value for char in '/\\<>:"|?*')
    )


def valid_date(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def valid_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_manifest_document_paths(
    data_dir: Path,
    docs: list[Document],
    *,
    release_mode: bool = False,
) -> list[str]:
    manifest = data_dir / "manifest.json"
    if not manifest.exists():
        return [f"{manifest}: release manifest is required"] if release_mode else []
    try:
        payload = json.loads(
            read_utf8_file_bounded(manifest, max_bytes=MAX_METADATA_FILE_BYTES)
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return [f"{manifest}: invalid manifest: {exc}"]
    items = payload.get("documents", [])
    if not isinstance(items, list):
        return [f"{manifest}: documents must be a list"]
    disk_by_path: dict[str, Document] = {}
    for doc in docs:
        if doc.path:
            try:
                disk_by_path[Path(doc.path).relative_to(data_dir).as_posix()] = doc
            except ValueError:
                disk_by_path[doc.path] = doc
    manifest_by_path: dict[str, dict] = {}
    errors: list[str] = []
    try:
        manifest_schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        manifest_schema_version = 0
    if release_mode and manifest_schema_version < CORPUS_SCHEMA_VERSION:
        errors.append(f"{manifest}: schema_version {CORPUS_SCHEMA_VERSION} is required for release")
    release_profile = payload.get("release_profile")
    try:
        release_profile_version = int(release_profile.get("version") or 0) if isinstance(release_profile, dict) else 0
    except (TypeError, ValueError):
        release_profile_version = 0
    if release_mode and (
        not isinstance(release_profile, dict)
        or release_profile_version != 1
        or release_profile.get("default") != "strict"
        or not isinstance(release_profile.get("allowed_failure_ids"), list)
    ):
        errors.append(f"{manifest}: valid release_profile version 1 is required for release")
    elif release_mode and isinstance(release_profile, dict):
        allowed_ids = release_profile.get("allowed_failure_ids", [])
        if (
            not all(isinstance(value, str) and value.strip() for value in allowed_ids)
            or len(set(allowed_ids)) != len(allowed_ids)
            or allowed_ids != sorted(allowed_ids)
        ):
            errors.append(f"{manifest}: release_profile.allowed_failure_ids must be sorted unique strings")
    for item in items:
        if not isinstance(item, dict):
            errors.append(f"{manifest}: document entries must be mappings")
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            errors.append(f"{manifest}: document entry has no path")
            continue
        normalized = Path(path).as_posix()
        if normalized in manifest_by_path:
            errors.append(f"{manifest}: duplicate document path {normalized}")
        manifest_by_path[normalized] = item
    if len(manifest_by_path) != len(disk_by_path):
        errors.append(
            f"{manifest}: manifest document count {len(manifest_by_path)} does not match disk document count {len(disk_by_path)}"
        )
    for path in sorted(disk_by_path.keys() - manifest_by_path.keys()):
        errors.append(f"{manifest}: missing document path in manifest: {path}")
    for path in sorted(manifest_by_path.keys() - disk_by_path.keys()):
        errors.append(f"{manifest}: manifest references missing document path: {path}")
    for path in sorted(disk_by_path.keys() & manifest_by_path.keys()):
        item = manifest_by_path[path]
        doc = disk_by_path[path]
        try:
            item_schema_version = int(item.get("schema_version") or 1)
        except (TypeError, ValueError):
            item_schema_version = 0
        if item_schema_version < CORPUS_SCHEMA_VERSION:
            if release_mode:
                errors.append(f"{manifest}: schema-v2 document entry is required for {path}")
            continue
        try:
            disk_mapping = read_frontmatter_mapping(data_dir / path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{manifest}: invalid document metadata for {path}: {exc}")
            continue
        manifest_mapping = {key: value for key, value in item.items() if key != "path"}
        if canonical_json_hash(disk_mapping) != canonical_json_hash(manifest_mapping):
            errors.append(f"{manifest}: manifest_metadata_mismatch for {path}")
    declared_index_hash = str(payload.get("index_source_hash") or "")
    if not declared_index_hash and release_mode:
        errors.append(f"{manifest}: index_source_hash is required for release")
    elif declared_index_hash:
        errors.extend(validate_hash(str(manifest), "index_source_hash", declared_index_hash))
        if declared_index_hash != index_source_hash_with_files(data_dir, docs):
            errors.append(f"{manifest}: index_source_hash mismatch")
    declared_release_hash = str(payload.get("release_hash") or "")
    if not declared_release_hash and release_mode:
        errors.append(f"{manifest}: release_hash is required for release")
    elif declared_release_hash:
        errors.extend(validate_hash(str(manifest), "release_hash", declared_release_hash))
        if declared_release_hash != release_hash(payload):
            errors.append(f"{manifest}: release_hash mismatch")
    return errors


def index_source_hash_with_files(data_dir: Path, docs: list[Document]) -> str:
    hydrated = copy.deepcopy(docs)
    for doc in hydrated:
        for att in doc.attachments:
            if att.converted_text_hash or not att.text_path:
                continue
            path = data_dir / att.text_path
            if not path.is_file():
                continue
            text, _ = read_utf8_strict(path, f"{doc.id}/{att.id}")
            if text is not None:
                att.converted_text_hash = canonical_text_hash(text)
    return index_source_hash(hydrated)


def read_frontmatter_mapping(path: Path) -> dict:
    data = read_utf8_file_bounded(path, max_bytes=MAX_METADATA_FILE_BYTES)
    if not data.startswith("---\n"):
        return {}
    end = data.find("\n---", 4)
    if end < 0:
        return {}
    return parse_frontmatter(data[4:end])
