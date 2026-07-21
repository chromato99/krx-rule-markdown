from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
from html import unescape
import re


class ConversionError(Exception):
    pass


@dataclass(frozen=True)
class ConversionDiagnostic:
    code: str
    message: str
    severity: str = "warn"


@dataclass
class ConversionOutcome:
    text: str
    raw_file_hash: str = ""
    diagnostics: list[ConversionDiagnostic] = field(default_factory=list)
    assets: list[dict[str, object]] = field(default_factory=list)
    searchable: bool = True

    @property
    def quality_codes(self) -> list[str]:
        return list(dict.fromkeys(item.code for item in self.diagnostics if item.code))


def infer_extension(path: Path, data: bytes) -> str:
    ext = path.suffix.lower()
    if ext:
        return ext
    parent = path.parent.name.lower()
    for candidate in (".hwpx", ".hwp", ".pdf", ".html", ".htm", ".txt"):
        if parent.endswith(candidate.replace(".", "-")):
            return candidate
    if data.startswith(b"%PDF-"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".hwpx"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".hwp"
    return ""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def normalize_converted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def dedupe_adjacent(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return out
