from __future__ import annotations

from pathlib import Path
import contextlib
import io
import re
import runpy
import sys

from .base import ConversionError, dedupe_adjacent, normalize_text
from .equation_latex import append_hwp_equations, clean_eqedit_script, hwp_equation_to_latex
from .tables import normalize_angle_bracket_tables, render_html_table, render_markdown_table, table_needs_html


def extract_hwp(path: Path) -> str:
    pyhwp_error: Exception | None = None
    try:
        import hwp5  # noqa: F401
    except ImportError as exc:
        pyhwp_error = exc
    else:
        try:
            layout_text = extract_hwp_layout(path)
        except Exception:
            layout_text = ""
        if layout_text.strip():
            return layout_text
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
        text = normalize_angle_bracket_tables(stdout.getvalue())
        formulas = extract_hwp_equations(path)
        if text.strip():
            return append_hwp_equations(text, formulas)
    preview = normalize_angle_bracket_tables(extract_hwp_preview(path))
    if preview.strip():
        return append_hwp_equations(preview, extract_hwp_equations(path))
    if pyhwp_error is not None:
        raise ConversionError("pyhwp is not installed") from pyhwp_error
    raise ConversionError("pyhwp produced empty text and no PrvText fallback was available")


def extract_hwp_layout(path: Path) -> str:
    try:
        from hwp5.binmodel import EqEdit
        from hwp5.proc.find import hwp5file_models
    except ImportError as exc:
        raise ConversionError("pyhwp is not installed") from exc

    models = list(hwp5file_models(str(path)))
    formulas = [
        parse_eqedit_payload(model.get("payload") or model.get("unparsed", b""))
        for model in models
        if model.get("type") is EqEdit
    ]
    formula_index = 0
    used_formula_count = 0
    paragraphs: list[str] = []
    model_index = 0
    while model_index < len(models):
        model = models[model_index]
        if model.get("tagname") == "HWPTAG_TABLE":
            rendered, model_index, formula_index, used = render_hwp_table(models, model_index, formulas, formula_index)
            used_formula_count += used
            if rendered.strip():
                paragraphs.append(rendered)
            continue
        if model.get("tagname") != "HWPTAG_PARA_TEXT":
            model_index += 1
            continue
        chunks = model.get("content", {}).get("chunks", [])
        rendered, formula_index, used = render_hwp_paragraph(chunks, formulas, formula_index)
        used_formula_count += used
        if rendered.strip():
            paragraphs.append(rendered)
        model_index += 1

    if not paragraphs:
        return ""
    remaining = [formula for formula in formulas[formula_index:] if formula.strip()]
    if remaining:
        blocks = []
        for offset, formula in enumerate(remaining, start=formula_index + 1):
            blocks.append(format_equation_block(offset, formula))
        paragraphs.extend(["## 위치 미확정 HWP 수식", *blocks])
    text = "\n\n".join(dedupe_adjacent(paragraphs))
    if used_formula_count or remaining:
        text = "\n\n".join([hwp_formula_notice(), text])
    return normalize_angle_bracket_tables(text).rstrip() + "\n"


def render_hwp_paragraph(chunks: list, formulas: list[str], formula_index: int) -> tuple[str, int, int]:
    rendered, notes, formula_index, used, has_text = render_hwp_chunks(chunks, formulas, formula_index)
    if not rendered:
        return "", formula_index, used
    if not has_text and notes:
        return "\n\n".join(notes), formula_index, used
    if notes:
        rendered = "\n\n".join([rendered, *notes])
    return rendered, formula_index, used


def render_hwp_chunks(chunks: list, formulas: list[str], formula_index: int) -> tuple[str, list[str], int, int, bool]:
    parts: list[str] = []
    notes: list[str] = []
    has_text = False
    used = 0
    for _, chunk in chunks:
        if isinstance(chunk, str):
            parts.append(chunk)
            if chunk.strip():
                has_text = True
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("chid") == "eqed":
            formula_index += 1
            used += 1
            formula = formulas[formula_index - 1] if formula_index <= len(formulas) else ""
            if not formula:
                continue
            parts.append(format_inline_equation(formula_index, formula))
            notes.append(format_equation_block(formula_index, formula))
        elif chunk.get("code") == 13:
            continue
    rendered = normalize_text("".join(parts))
    return rendered, notes, formula_index, used, has_text


def render_hwp_table(models: list, table_index: int, formulas: list[str], formula_index: int) -> tuple[str, int, int, int]:
    table = models[table_index]
    table_level = table.get("level", 0)
    table_content = table.get("content", {})
    expected_cells = sum(table_content.get("rowcols", [])) or table_content.get("rows", 0) * table_content.get("cols", 0)
    cells: list[dict] = []
    notes: list[str] = []
    used = 0
    i = table_index + 1
    while i < len(models) and len(cells) < expected_cells:
        model = models[i]
        if model.get("tagname") != "HWPTAG_LIST_HEADER" or model.get("level") != table_level:
            if model.get("level", 0) < table_level:
                break
            i += 1
            continue
        header = model.get("content", {})
        i += 1
        paragraphs: list[str] = []
        while i < len(models):
            next_model = models[i]
            if next_model.get("tagname") == "HWPTAG_LIST_HEADER" and next_model.get("level") == table_level:
                break
            if next_model.get("level", 0) < table_level:
                break
            if next_model.get("tagname") == "HWPTAG_PARA_TEXT":
                text, para_notes, formula_index, para_used, _ = render_hwp_chunks(
                    next_model.get("content", {}).get("chunks", []),
                    formulas,
                    formula_index,
                )
                used += para_used
                if text:
                    paragraphs.append(text)
                notes.extend(para_notes)
            i += 1
        cells.append(
            {
                "row": int(header.get("row", 0) or 0),
                "col": int(header.get("col", 0) or 0),
                "rowspan": int(header.get("rowspan", 1) or 1),
                "colspan": int(header.get("colspan", 1) or 1),
                "text": "<br>".join(paragraphs),
            }
        )
    if not cells:
        return "", table_index + 1, formula_index, used
    rendered = render_hwp_table_cells(cells)
    if notes:
        rendered = "\n\n".join([rendered, *notes])
    return rendered, i, formula_index, used


def render_hwp_table_cells(cells: list[dict]) -> str:
    max_row = max(cell["row"] for cell in cells)
    rows: list[list[str]] = [[] for _ in range(max_row + 1)]
    spans: list[list[tuple[int, int]]] = [[] for _ in range(max_row + 1)]
    for cell in sorted(cells, key=lambda value: (value["row"], value["col"])):
        rows[cell["row"]].append(cell["text"])
        spans[cell["row"]].append((max(1, cell["rowspan"]), max(1, cell["colspan"])))
    if table_needs_html(rows, spans):
        return render_html_table(rows, spans)
    return render_markdown_table(rows)


def format_inline_equation(index: int, formula: str) -> str:
    latex = hwp_equation_to_latex(formula)
    if latex:
        return f" [수식 {index} LaTeX(best-effort): \\({latex}\\)] "
    return f" [수식 {index} HWP EqEdit: {normalize_text(formula)}] "


def format_equation_block(index: int, formula: str) -> str:
    latex = hwp_equation_to_latex(formula)
    lines = [f"수식 {index} 원본(HWP EqEdit):", "```hwp-equation", formula, "```"]
    if latex:
        lines.extend(["", f"수식 {index} LaTeX(best-effort):", "```math", latex, "```"])
    return "\n".join(lines)


def hwp_formula_notice() -> str:
    return (
        "> HWP EqEdit 수식은 원문 문단의 수식 위치에 가능한 한 가깝게 배치합니다. "
        "LaTeX는 RAG 참조용 best-effort 변환이므로 정확한 인용이 필요하면 바로 뒤의 "
        "HWP EqEdit 원본 블록 또는 원본 HWP 첨부를 함께 확인하세요."
    )


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
