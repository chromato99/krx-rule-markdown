from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib import parse
import re
import struct
import zlib

from .contracts import add_quality_code, canonical_text_hash, parse_quality_codes, sha256_bytes
from .markdown import document_bundle_dir
from .models import Asset, Attachment, Document, slug
from .repository import atomic_write_bytes, atomic_write_text


MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_ASSET_PIXELS = 40_000_000
MAX_ASSET_DIMENSION = 20_000
MAX_HWP_FILE_BYTES = 64 * 1024 * 1024
MAX_HWP_STREAMS = 2048
MAX_HWP_IMAGE_STREAMS = 256
MAX_HWP_ASSET_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DEFLATE_RATIO = 400

INLINE_IMAGE_RE = re.compile(r"\[이미지:\s*(https?://[^\]\s]+)\s*\]")
ASSET_BLOCK_RE = re.compile(
    r"\n*<!-- krx-assets:start -->.*?<!-- krx-assets:end -->\n*",
    re.S,
)


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class HWPImageStream:
    source_anchor: str
    stream_name: str
    data: bytes
    image: ImageInfo


class AssetError(RuntimeError):
    pass


def inspect_image(data: bytes) -> ImageInfo:
    if not data or len(data) > MAX_ASSET_BYTES:
        raise AssetError(f"image byte size must be 1..{MAX_ASSET_BYTES}")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack_from("<HH", data, 6)
        return checked_image("image/gif", ".gif", width, height)
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", data, 16)
        return checked_image("image/png", ".png", width, height)
    if data.startswith(b"BM") and len(data) >= 26:
        dib_size = struct.unpack_from("<I", data, 14)[0]
        if dib_size == 12:
            width, height = struct.unpack_from("<HH", data, 18)
        elif dib_size >= 40:
            width, signed_height = struct.unpack_from("<ii", data, 18)
            height = abs(signed_height)
        else:
            raise AssetError(f"unsupported BMP DIB header size {dib_size}")
        return checked_image("image/bmp", ".bmp", width, height)
    if data.startswith(b"\xff\xd8"):
        width, height = jpeg_dimensions(data)
        return checked_image("image/jpeg", ".jpg", width, height)
    raise AssetError("unsupported or mismatched image signature")


def checked_image(mime_type: str, extension: str, width: int, height: int) -> ImageInfo:
    if width <= 0 or height <= 0:
        raise AssetError("image dimensions must be positive")
    if width > MAX_ASSET_DIMENSION or height > MAX_ASSET_DIMENSION:
        raise AssetError(f"image dimensions exceed {MAX_ASSET_DIMENSION}px")
    if width * height > MAX_ASSET_PIXELS:
        raise AssetError(f"image pixel count exceeds {MAX_ASSET_PIXELS}")
    return ImageInfo(mime_type, extension, width, height)


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if offset + 2 > len(data):
            break
        segment_length = struct.unpack_from(">H", data, offset)[0]
        if segment_length < 2 or offset + segment_length > len(data):
            raise AssetError("malformed JPEG segment length")
        if marker in sof_markers:
            if segment_length < 7:
                raise AssetError("malformed JPEG SOF segment")
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return width, height
        offset += segment_length
    raise AssetError("JPEG has no supported SOF dimensions")


def read_hwp_image_streams(path: Path) -> list[HWPImageStream]:
    path = Path(path)
    if path.stat().st_size > MAX_HWP_FILE_BYTES:
        raise AssetError(f"HWP exceeds {MAX_HWP_FILE_BYTES} bytes")
    try:
        import olefile
    except ImportError as exc:
        raise AssetError("olefile is unavailable") from exc
    try:
        ole = olefile.OleFileIO(str(path))
    except Exception as exc:  # noqa: BLE001 - normalized as an asset error.
        raise AssetError(f"HWP OLE container could not be opened: {exc}") from exc
    try:
        all_streams = ole.listdir(streams=True, storages=False)
        if len(all_streams) > MAX_HWP_STREAMS:
            raise AssetError(f"HWP has too many OLE streams ({len(all_streams)}/{MAX_HWP_STREAMS})")
        bindata = [parts for parts in all_streams if parts and parts[0].lower() == "bindata"]
        if len(bindata) > MAX_HWP_IMAGE_STREAMS:
            raise AssetError(
                f"HWP has too many BinData streams ({len(bindata)}/{MAX_HWP_IMAGE_STREAMS})"
            )
        images: list[HWPImageStream] = []
        total = 0
        for parts in sorted(bindata, key=lambda value: "/".join(value).lower()):
            stream_size = ole.get_size(parts)
            if stream_size <= 0 or stream_size > MAX_ASSET_BYTES:
                continue
            raw = ole.openstream(parts).read(MAX_ASSET_BYTES + 1)
            if len(raw) > MAX_ASSET_BYTES:
                raise AssetError(f"HWP BinData stream exceeds {MAX_ASSET_BYTES} bytes")
            try:
                decoded, image = decode_hwp_image_stream(raw)
            except AssetError:
                continue
            total += len(decoded)
            if total > MAX_HWP_ASSET_TOTAL_BYTES:
                raise AssetError(f"HWP decoded image bytes exceed {MAX_HWP_ASSET_TOTAL_BYTES}")
            anchor = "hwp:" + "/".join(parts)
            images.append(HWPImageStream(anchor, Path(parts[-1]).name, decoded, image))
        return images
    finally:
        ole.close()


def decode_hwp_image_stream(data: bytes) -> tuple[bytes, ImageInfo]:
    try:
        return data, inspect_image(data)
    except AssetError:
        pass
    try:
        inflater = zlib.decompressobj(-15)
        decoded = inflater.decompress(data, MAX_ASSET_BYTES + 1)
        if len(decoded) <= MAX_ASSET_BYTES:
            decoded += inflater.flush(MAX_ASSET_BYTES + 1 - len(decoded))
    except zlib.error as exc:
        raise AssetError("BinData is neither a supported image nor raw-deflate image data") from exc
    if len(decoded) > MAX_ASSET_BYTES or inflater.unconsumed_tail:
        raise AssetError("deflated HWP image exceeds the decoded byte limit")
    if not inflater.eof:
        raise AssetError("truncated deflated HWP image stream")
    if len(decoded) > max(1, len(data)) * MAX_DEFLATE_RATIO:
        raise AssetError("deflated HWP image compression ratio is unsafe")
    return decoded, inspect_image(decoded)


def preserve_hwp_attachment_assets(
    data_dir: Path,
    doc: Document,
    att: Attachment,
    *,
    streams: list[HWPImageStream] | None = None,
) -> list[Asset]:
    if not att.raw_path:
        return []
    raw_path = Path(data_dir) / att.raw_path
    if raw_path.suffix.lower() != ".hwp" or not raw_path.is_file():
        return []
    streams = streams if streams is not None else read_hwp_image_streams(raw_path)
    bundle = document_bundle(data_dir, doc)
    output_dir = bundle / "assets" / slug(att.id)
    assets: list[Asset] = []
    used_names: set[str] = set()
    for stream in streams:
        stem = slug(Path(stream.stream_name).stem)
        name = unique_name(f"{stem}{stream.image.extension}", used_names)
        output_path = output_dir / name
        write_bytes_if_changed(output_path, stream.data)
        relative = output_path.relative_to(data_dir).as_posix()
        assets.append(
            Asset(
                id=slug(f"asset-{att.id}-{stream.source_anchor}"),
                source_kind="hwp_bindata",
                source_anchor=stream.source_anchor,
                path=relative,
                mime_type=stream.image.mime_type,
                raw_file_hash=sha256_bytes(stream.data),
                size=len(stream.data),
                width=stream.image.width,
                height=stream.image.height,
                preservation_status="preserved",
                searchable=False,
                quality_codes=["image_content_unindexed"],
            )
        )
    att.assets = assets
    att.asset_inspection_version = "1"
    if streams and len(assets) == len(streams):
        att.quality_codes = [code for code in parse_quality_codes(att.quality_codes) if code != "hwp_picture_missing"]
        att.diagnostics = [item for item in att.diagnostics if item.get("code") != "hwp_picture_missing"]
        att.quality_flags = ",".join(att.quality_codes)
    if att.text_path and (Path(data_dir) / att.text_path).is_file():
        text_path = Path(data_dir) / att.text_path
        original = text_path.read_text(encoding="utf-8", errors="strict")
        rendered = render_attachment_asset_block(original, text_path, data_dir, assets)
        write_text_if_changed(text_path, rendered.rstrip() + "\n")
        att.converted_text_hash = canonical_text_hash(rendered)
    return assets


def render_attachment_asset_block(text: str, text_path: Path, data_dir: Path, assets: list[Asset]) -> str:
    base = ASSET_BLOCK_RE.sub("\n", text).rstrip()
    if not assets:
        return base
    lines = ["<!-- krx-assets:start -->", "### 보존된 HWP 원본 이미지", ""]
    for asset in assets:
        lines.append(f"- `{asset.source_anchor}` ({asset.width}×{asset.height})")
        lines.append(f"  ![{asset.source_anchor}](krx-asset:{asset.id})")
    lines.append("<!-- krx-assets:end -->")
    return base + "\n\n" + "\n".join(lines)


InlineDownloader = Callable[[str], tuple[bytes, str]]


def preserve_inline_document_assets(
    data_dir: Path,
    doc: Document,
    downloader: InlineDownloader | None,
) -> list[Asset]:
    already_inspected = doc.asset_inspection_version == "1"
    urls = list(dict.fromkeys(INLINE_IMAGE_RE.findall(doc.body)))
    if not urls and already_inspected:
        urls = list(
            dict.fromkeys(
                asset.source_url
                for asset in doc.assets
                if asset.source_kind == "html_inline" and asset.source_url
            )
        )
    doc.asset_inspection_version = "1"
    if not urls:
        doc.assets = [asset for asset in doc.assets if asset.source_kind != "html_inline"]
        return doc.assets
    existing = {asset.source_url: asset for asset in doc.assets if asset.source_kind == "html_inline"}
    bundle = document_bundle(data_dir, doc)
    assets: list[Asset] = [asset for asset in doc.assets if asset.source_kind != "html_inline"]
    body = doc.body
    used_names: set[str] = set()
    for url in urls:
        previous = existing.get(url)
        try:
            if previous is not None and previous.path and (Path(data_dir) / previous.path).is_file():
                existing_path = Path(data_dir) / previous.path
                if existing_path.stat().st_size > MAX_ASSET_BYTES:
                    raise AssetError(f"existing asset exceeds {MAX_ASSET_BYTES} bytes")
                data = existing_path.read_bytes()
                image = inspect_image(data)
            elif downloader is None:
                raise AssetError("inline asset download was not requested")
            else:
                data, declared_mime = downloader(url)
                image = inspect_image(data)
                if declared_mime and declared_mime not in {
                    image.mime_type,
                    "application/octet-stream",
                    "binary/octet-stream",
                }:
                    raise AssetError(
                        f"HTTP Content-Type {declared_mime!r} conflicts with {image.mime_type!r} signature"
                    )
            source_name = Path(parse.urlparse(url).path).stem or "inline-image"
            name = unique_name(f"{slug(source_name)}{image.extension}", used_names)
            output_path = bundle / "assets" / "inline" / name
            write_bytes_if_changed(output_path, data)
            relative = output_path.relative_to(data_dir).as_posix()
            asset = Asset(
                id=slug(f"asset-{doc.id}-html-{url}"),
                source_kind="html_inline",
                source_anchor=f"html-img:{url}",
                source_url=url,
                path=relative,
                mime_type=image.mime_type,
                raw_file_hash=sha256_bytes(data),
                size=len(data),
                width=image.width,
                height=image.height,
                preservation_status="preserved",
                searchable=False,
                quality_codes=["image_content_unindexed"],
            )
            body = body.replace(f"[이미지: {url}]", f"![KRX 규정 이미지](krx-asset:{asset.id})")
        except Exception as exc:  # noqa: BLE001 - optional asset failure is explicit metadata.
            asset = Asset(
                id=slug(f"asset-{doc.id}-html-{url}"),
                source_kind="html_inline",
                source_anchor=f"html-img:{url}",
                source_url=url,
                preservation_status="failed",
                searchable=False,
                quality_codes=["inline_image_missing", "image_content_unindexed"],
                error=str(exc),
            )
        assets.append(asset)
    doc.assets = assets
    doc.body = body
    doc.body_hash = canonical_text_hash(body)
    if INLINE_IMAGE_RE.search(body):
        doc.quality_codes = add_quality_code(doc.quality_codes, "inline_image_missing")
        doc.quality_codes = add_quality_code(doc.quality_codes, "image_content_unindexed")
    else:
        doc.quality_codes = [code for code in parse_quality_codes(doc.quality_codes) if code != "inline_image_missing"]
    if any(asset.source_kind == "html_inline" for asset in assets):
        doc.quality_codes = add_quality_code(doc.quality_codes, "image_content_unindexed")
        if doc.quality_status in {"", "ok"}:
            doc.quality_status = "warn"
    return assets


def document_bundle(data_dir: Path, doc: Document) -> Path:
    if doc.path:
        path = Path(doc.path)
        if not path.is_absolute():
            path = Path(data_dir) / path
        return path.parent
    return document_bundle_dir(Path(data_dir), doc)


def unique_name(candidate: str, used: set[str]) -> str:
    path = Path(candidate)
    name = candidate
    index = 2
    while name.lower() in used:
        name = f"{path.stem}-{index}{path.suffix}"
        index += 1
    used.add(name.lower())
    return name


def write_bytes_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.stat().st_size <= MAX_ASSET_BYTES and path.read_bytes() == data:
        return False
    atomic_write_bytes(path, data)
    return True


def write_text_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8", errors="strict") == text:
        return False
    atomic_write_text(path, text)
    return True
