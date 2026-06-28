from __future__ import annotations

from pathlib import Path
import contextlib
import io
import re
import runpy
import sys

from .base import ConversionError
from .equation_latex import append_hwp_equations, clean_eqedit_script


def extract_hwp(path: Path) -> str:
    pyhwp_error: Exception | None = None
    try:
        import hwp5  # noqa: F401
    except ImportError as exc:
        pyhwp_error = exc
    else:
        old_argv = sys.argv[:]
        stdout = io.StringIO()
        try:
            sys.argv = ["hwp5txt", str(path)]
            with contextlib.redirect_stdout(stdout):
                try:
                    runpy.run_module("hwp5.hwp5txt", run_name="__main__")
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise ConversionError(f"pyhwp hwp5txt exited with {exc.code}") from exc
        finally:
            sys.argv = old_argv
        text = stdout.getvalue()
        formulas = extract_hwp_equations(path)
        if text.strip():
            return append_hwp_equations(text, formulas)
    preview = extract_hwp_preview(path)
    if preview.strip():
        return append_hwp_equations(preview, extract_hwp_equations(path))
    if pyhwp_error is not None:
        raise ConversionError("pyhwp is not installed") from pyhwp_error
    raise ConversionError("pyhwp produced empty text and no PrvText fallback was available")


def extract_hwp_equations(path: Path) -> list[str]:
    try:
        from hwp5.binmodel import EqEdit
        from hwp5.proc.find import hwp5file_models
    except ImportError:
        return []

    formulas: list[str] = []
    try:
        models = hwp5file_models(str(path))
        for model in models:
            if model.get("type") is not EqEdit:
                continue
            formula = parse_eqedit_payload(model.get("payload", b""))
            if formula:
                formulas.append(formula)
    except Exception:
        return []
    return formulas


def parse_eqedit_payload(payload: bytes) -> str:
    if len(payload) < 6:
        return ""
    script_len = int.from_bytes(payload[4:6], "little")
    script_end = 6 + script_len * 2
    if 0 < script_len and script_end <= len(payload):
        script = payload[6:script_end].decode("utf-16le", errors="replace")
        script = clean_eqedit_script(script)
        if script:
            return script
    return fallback_eqedit_payload(payload)


def fallback_eqedit_payload(payload: bytes) -> str:
    text = payload.decode("utf-16le", errors="ignore")
    text = re.sub(r"Equation Version \d+", " ", text)
    text = text.replace("HYhwpEQ", " ")
    return clean_eqedit_script(text)


def extract_hwp_preview(path: Path) -> str:
    try:
        import olefile
    except ImportError as exc:
        raise ConversionError("olefile is not installed") from exc
    try:
        ole = olefile.OleFileIO(str(path))
    except OSError as exc:
        raise ConversionError("HWP OLE container could not be opened") from exc
    try:
        if not ole.exists("PrvText"):
            return ""
        data = ole.openstream("PrvText").read()
    finally:
        ole.close()
    for encoding in ("utf-16le", "utf-16", "cp949", "utf-8"):
        text = data.decode(encoding, errors="replace")
        if readable_score(text) > 0.5:
            return text
    return data.decode("utf-16le", errors="replace")


def readable_score(text: str) -> float:
    if not text:
        return 0.0
    useful = sum(1 for ch in text if ch.isalnum() or "가" <= ch <= "힣")
    bad = text.count("\ufffd") + text.count("\x00")
    return useful / max(1, useful + bad)
