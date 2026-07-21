from __future__ import annotations

from pathlib import Path
from typing import Any


class SourceInspectionCache:
    """Per-operation cache for expensive parser and OLE inspection results."""

    def __init__(self) -> None:
        self._hwp_models: dict[tuple[str, int, int], tuple[list[Any], str]] = {}
        self._hwp_images: dict[tuple[str, int, int], tuple[list[Any], str]] = {}
        self.hwp_model_parse_count = 0
        self.hwp_ole_parse_count = 0

    def hwp_models(self, path: Path) -> tuple[list[Any], str]:
        key = source_key(path)
        if key not in self._hwp_models:
            self.hwp_model_parse_count += 1
            try:
                from hwp5.proc.find import hwp5file_models

                models = list(hwp5file_models(str(path)))
                self._hwp_models[key] = (models, "")
            except Exception as exc:  # noqa: BLE001 - caller records the inspection reason.
                self._hwp_models[key] = ([], f"HWP model inspection failed ({type(exc).__name__}): {exc}")
        return self._hwp_models[key]

    def hwp_images(self, path: Path) -> tuple[list[Any], str]:
        key = source_key(path)
        if key not in self._hwp_images:
            self.hwp_ole_parse_count += 1
            try:
                from ..assets import read_hwp_image_streams

                self._hwp_images[key] = (read_hwp_image_streams(path), "")
            except Exception as exc:  # noqa: BLE001 - caller records the inspection reason.
                self._hwp_images[key] = ([], f"HWP picture inspection failed ({type(exc).__name__}): {exc}")
        return self._hwp_images[key]


def source_key(path: Path) -> tuple[str, int, int]:
    stat = Path(path).stat()
    return (str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns)
