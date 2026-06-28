from __future__ import annotations

from pathlib import Path
import contextlib
import io
import runpy
import sys
import zipfile
import re
import xml.etree.ElementTree as ET
from html import unescape

from .html import html_to_markdown
from .models import Attachment, ATTACHMENT_CONVERTED, ATTACHMENT_FAILED, hash_bytes
from .quality import apply_quality, inspect_attachment_quality, mark_quality_failure


class ConversionError(Exception):
    pass


def convert_attachment(raw_path: Path, out_path: Path, att: Attachment) -> Attachment:
    data = raw_path.read_bytes()
    att.raw_path = str(raw_path)
    att.text_path = str(out_path)
    att.size = len(data)
    att.content_hash = hash_bytes(data)
    try:
        text = convert_bytes(raw_path, data)
        if not text.strip():
            raise ConversionError("conversion produced empty text")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.strip() + "\n", encoding="utf-8")
        apply_quality(att, inspect_attachment_quality(text, raw_path))
        att.status = ATTACHMENT_CONVERTED
        att.error = ""
    except Exception as exc:  # noqa: BLE001 - failure reason is part of the manifest.
        att.status = ATTACHMENT_FAILED
        att.error = str(exc)
        att.text_path = ""
        mark_quality_failure(att, "conversion_failed")
        with contextlib.suppress(FileNotFoundError):
            out_path.unlink()
    return att


def convert_bytes(path: Path, data: bytes) -> str:
    ext = infer_extension(path, data)
    if ext in {".md", ".txt"}:
        return data.decode("utf-8", errors="replace")
    if ext in {".html", ".htm"}:
        return html_to_markdown(data.decode("utf-8", errors="replace"))
    if ext == ".hwpx":
        return extract_hwpx(data)
    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".hwp":
        return extract_hwp(path)
    raise ConversionError(f"unsupported attachment extension {ext!r}")


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


def extract_hwpx(data: bytes) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if not lower.endswith(".xml"):
                continue
            if "section" not in lower and "bodytext" not in lower:
                continue
            xml = zf.read(name).decode("utf-8", errors="replace")
            text = extract_hwpx_xml(xml)
            if text:
                chunks.append(text)
    if not chunks:
        raise ConversionError("no readable HWPX body XML found")
    return "\n\n".join(chunks)


def extract_hwpx_xml(xml: str) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return fallback_xml_text(xml)

    lines: list[str] = []
    table_descendants: set[int] = set()
    for tbl in root.iter():
        if local_name(tbl.tag) != "tbl":
            continue
        for node in tbl.iter():
            table_descendants.add(id(node))
        for tr in tbl.iter():
            if local_name(tr.tag) != "tr":
                continue
            cells = [
                normalize_text(" ".join(iter_element_text(tc)))
                for tc in tr.iter()
                if local_name(tc.tag) == "tc"
            ]
            cells = [cell for cell in cells if cell]
            if len(cells) >= 2:
                lines.append("| " + " | ".join(cells) + " |")

    for elem in root.iter():
        if id(elem) in table_descendants:
            continue
        lname = local_name(elem.tag)
        if lname in {"p", "equation", "formula", "eq"}:
            text = normalize_text(" ".join(iter_element_text(elem)))
            if lname in {"equation", "formula", "eq"}:
                attr_text = normalize_text(" ".join(str(value) for value in elem.attrib.values()))
                text = normalize_text(f"{text} {attr_text}")
                if text:
                    text = "수식: " + text
            if text:
                lines.append(text)
    if not lines:
        return fallback_xml_text(xml)
    return "\n".join(dedupe_adjacent(lines))


def local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def iter_element_text(elem: ET.Element) -> list[str]:
    parts: list[str] = []
    if elem.text and elem.text.strip():
        parts.append(unescape(elem.text.strip()))
    for child in elem:
        parts.extend(iter_element_text(child))
        if child.tail and child.tail.strip():
            parts.append(unescape(child.tail.strip()))
    return parts


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def fallback_xml_text(xml: str) -> str:
    text = re.sub(r"<[^>]+>", " ", xml)
    return normalize_text(text)


def dedupe_adjacent(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return out


def extract_pdf(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:
        raise ConversionError("pdfminer.six is not installed") from exc
    return extract_text(str(path))


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


def append_hwp_equations(text: str, formulas: list[str]) -> str:
    formulas = [formula for formula in formulas if formula.strip()]
    if not formulas:
        return text
    lines = [
        text.rstrip(),
        "",
        "## HWP 수식",
        "",
        "이 섹션은 HWP EqEdit 원본 수식과 Markdown/RAG 참조용 LaTeX 자동 변환을 함께 제공합니다. "
        "`hwp-equation` 블록이 원본이며, 이어지는 `math` 블록은 best-effort 변환 결과입니다. "
        "수식을 인용하거나 검증할 때는 원본 HWP 수식과 LaTeX 변환을 함께 참조하세요.",
        "",
    ]
    for i, formula in enumerate(formulas, start=1):
        latex = hwp_equation_to_latex(formula)
        lines.extend([f"수식 {i} 원본(HWP EqEdit):", "```hwp-equation", formula, "```", ""])
        if latex:
            lines.extend([f"수식 {i} LaTeX(best-effort):", "```math", latex, "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def hwp_equation_to_latex(script: str) -> str:
    script = clean_eqedit_script(script)
    if not script:
        return ""
    expr = normalize_hwp_equation_script(script)
    return convert_hwp_expression(expr)


def normalize_hwp_equation_script(script: str) -> str:
    script = replace_quoted_hwp_literals(script)
    script = script.replace("`", " ")
    script = script.replace("~", " ")
    script = script.replace("≤", r"\le ")
    script = script.replace("≥", r"\ge ")
    script = script.replace("≠", r"\ne ")
    script = script.replace("×", r"\times ")
    script = script.replace("÷", r"\div ")
    script = re.sub(r"\bbarr(?=_|\b)", r"bar{r}", script)
    script = re.sub(r"\btimes(?=[A-Z(])", "times ", script)
    return normalize_text(script)


def replace_quoted_hwp_literals(script: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = match.group(1).replace("`", " ").strip()
        if not value:
            return " "
        if value == "{":
            return r"\{"
        if value == "}":
            return r"\}"
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]*", value):
            return r"\operatorname{" + normalize_text(value) + "}"
        return r"\text{" + normalize_text(value) + "}"

    return re.sub(r'"([^"]*)"', repl, script)


def convert_hwp_expression(expr: str) -> str:
    expr = strip_outer_group(expr.strip())
    if not expr:
        return ""

    expr = replace_braced_command(expr, "hat", lambda arg: rf"\hat{{{convert_hwp_expression(arg)}}}")
    expr = replace_braced_command(expr, "sqrt", lambda arg: rf"\sqrt{{{convert_hwp_expression(arg)}}}")
    expr = replace_braced_command(expr, "root", lambda arg: rf"\sqrt{{{convert_hwp_expression(arg)}}}")
    expr = replace_braced_command(expr, "bar", lambda arg: rf"\bar{{{convert_hwp_expression(arg)}}}")
    expr = replace_braced_command(expr, "dmatrix", lambda arg: rf"\displaystyle {convert_hwp_expression(arg)}")
    expr = replace_braced_command(expr, "matrix", lambda arg: matrix_to_latex(arg))
    expr = replace_braced_command(expr, "eqalign", eqalign_to_latex)
    expr = replace_braced_command(expr, "cases", cases_to_latex)
    expr = replace_left_right(expr)
    expr = replace_over_operators(expr)
    div = find_top_level_division(expr)
    if div >= 0:
        left = expr[:div].strip()
        right = expr[div + 1 :].strip()
        if left and right:
            return rf"\frac{{{convert_hwp_expression(left)}}}{{{convert_hwp_expression(right)}}}"
    expr = convert_subscripts(expr)
    expr = convert_symbols(expr)
    expr = wrap_korean_text(expr)
    expr = replace_hash_linebreaks(expr)
    expr = normalize_latex_spacing(expr)
    expr = cleanup_latex(expr)
    return expr


def strip_outer_group(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("{") and expr.endswith("}") and matching_brace_index(expr, 0) == len(expr) - 1:
        expr = expr[1:-1].strip()
    return expr


def find_top_level_division(expr: str) -> int:
    depth = 0
    for i, ch in enumerate(expr):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "/" and depth == 0:
            return i
    return -1


def replace_over_operators(expr: str) -> str:
    i = 0
    while True:
        match = re.search(r"(?<!\\)\bover\b", expr[i:], flags=re.I)
        if not match:
            return expr
        start = i + match.start()
        end = i + match.end()
        left_start = left_fraction_operand_start(expr, start)
        right_end = right_fraction_operand_end(expr, end)
        left = expr[left_start:start].strip()
        right = expr[end:right_end].strip()
        prefix = ""
        prefixed_left = re.match(r"(?i)^(LEQ|GEQ|NEQ|leq|geq|neq)\s+(.+)$", left)
        if prefixed_left:
            prefix = prefixed_left.group(1) + " "
            left = prefixed_left.group(2).strip()
        if not left or not right:
            i = end
            continue
        replacement = prefix + rf"\frac{{{convert_hwp_expression(left)}}}{{{convert_hwp_expression(right)}}}"
        expr = expr[:left_start] + replacement + expr[right_end:]
        i = left_start + len(replacement)


def left_fraction_operand_start(expr: str, over_start: int) -> int:
    i = over_start - 1
    while i >= 0 and expr[i].isspace():
        i -= 1
    depth_curly = depth_round = depth_square = 0
    while i >= 0:
        ch = expr[i]
        if ch == "}":
            depth_curly += 1
        elif ch == "{":
            if depth_curly == 0:
                return i + 1
            depth_curly -= 1
        elif ch == ")":
            depth_round += 1
        elif ch == "(":
            if depth_round == 0 and depth_curly == 0 and depth_square == 0:
                return i + 1
            depth_round -= 1
        elif ch == "]":
            depth_square += 1
        elif ch == "[":
            if depth_square == 0 and depth_curly == 0 and depth_round == 0:
                return i + 1
            depth_square -= 1
        elif depth_curly == 0 and depth_round == 0 and depth_square == 0 and ch in "=,+;&":
            return i + 1
        i -= 1
    return 0


def right_fraction_operand_end(expr: str, over_end: int) -> int:
    i = over_end
    while i < len(expr) and expr[i].isspace():
        i += 1
    if i >= len(expr):
        return i
    if expr[i] == "{":
        end = matching_brace_index(expr, i)
        if end >= 0:
            return end + 1
    if expr[i] == "(":
        end = matching_pair_index(expr, i, "(", ")")
        if end >= 0:
            j = end + 1
            while j < len(expr) and expr[j] in " !":
                j += 1
            while j < len(expr) and re.match(r"[A-Za-z0-9_{}!]", expr[j]):
                j += 1
            return j
    depth_curly = depth_round = depth_square = 0
    j = i
    while j < len(expr):
        if depth_curly == 0 and depth_round == 0 and depth_square == 0:
            rest = expr[j:]
            if re.match(r"\s+(?:TIMES|times|\+|-|=|,|;|&|#)", rest):
                break
            if expr[j] in ")]}":
                break
        ch = expr[j]
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            if depth_curly == 0:
                break
            depth_curly -= 1
        elif ch == "(":
            depth_round += 1
        elif ch == ")":
            if depth_round == 0:
                break
            depth_round -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            if depth_square == 0:
                break
            depth_square -= 1
        j += 1
    return j


def replace_braced_command(expr: str, command: str, replacer) -> str:
    out: list[str] = []
    i = 0
    needle = command.lower()
    while i < len(expr):
        if expr[i : i + len(command)].lower() == needle and command_boundary(expr, i, len(command)):
            j = i + len(command)
            while j < len(expr) and expr[j].isspace():
                j += 1
            if j < len(expr) and expr[j] == "{":
                end = matching_brace_index(expr, j)
                if end >= 0:
                    out.append(replacer(expr[j + 1 : end]))
                    i = end + 1
                    continue
        out.append(expr[i])
        i += 1
    return "".join(out)


def command_boundary(expr: str, start: int, length: int) -> bool:
    before = expr[start - 1] if start > 0 else ""
    after = expr[start + length] if start + length < len(expr) else ""
    return before != "\\" and not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def matching_brace_index(expr: str, start: int) -> int:
    if start >= len(expr) or expr[start] != "{":
        return -1
    depth = 0
    for i in range(start, len(expr)):
        if expr[i] == "{":
            depth += 1
        elif expr[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def matching_pair_index(expr: str, start: int, open_ch: str, close_ch: str) -> int:
    if start >= len(expr) or expr[start] != open_ch:
        return -1
    depth = 0
    for i in range(start, len(expr)):
        if expr[i] == open_ch:
            depth += 1
        elif expr[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def matrix_to_latex(arg: str) -> str:
    rows = [row.strip() for row in re.split(r"\s*#\s*", arg) if row.strip()]
    converted_rows = []
    for row in rows or [arg]:
        cells = [cell.strip() for cell in re.split(r"\s*&\s*", row) if cell.strip()]
        converted_rows.append(" & ".join(convert_hwp_expression(cell) for cell in (cells or [row])))
    return r"\begin{matrix}" + r" \\ ".join(converted_rows) + r"\end{matrix}"


def eqalign_to_latex(arg: str) -> str:
    rows = [row.strip() for row in re.split(r"\s*#\s*", arg) if row.strip()]
    if len(rows) <= 1:
        return convert_hwp_expression(rows[0] if rows else arg.replace("#", " "))
    return r"\begin{aligned}" + r" \\ ".join(convert_hwp_expression(row) for row in rows) + r"\end{aligned}"


def cases_to_latex(arg: str) -> str:
    rows = [row.strip() for row in re.split(r"\s*#\s*", arg) if row.strip()]
    converted = []
    for row in rows or [arg]:
        row = normalize_case_condition(row)
        parts = [part.strip() for part in row.split("&", 1)]
        if len(parts) == 2:
            converted.append(convert_hwp_expression(parts[0]) + " & " + convert_hwp_expression(parts[1]))
        else:
            converted.append(convert_hwp_expression(row))
    return r"\begin{cases}" + r" \\ ".join(converted) + r"\end{cases}"


def normalize_case_condition(row: str) -> str:
    row = re.sub(r"\bif\b", r"\\text{if}", row)
    row = re.sub(r"\bwhere\b", r"\\text{where}", row)
    return row


def replace_left_right(expr: str) -> str:
    pairs = {
        "{": r"\{",
        "}": r"\}",
        "(": "(",
        ")": ")",
        "[": "[",
        "]": "]",
        "|": "|",
    }

    def repl(match: re.Match[str]) -> str:
        side = "left" if match.group(1).upper() == "LEFT" else "right"
        symbol = pairs.get(match.group(2), match.group(2))
        return rf"\{side}{symbol}"

    expr = re.sub(r"\b(LEFT|RIGHT)\s*([{}()\[\]|])", repl, expr, flags=re.I)
    expr = re.sub(r"}\s*(?<!\\)\bright\b", r" \\right\\}", expr, flags=re.I)
    expr = re.sub(r"(?<!\\)\bleft\b(?!\s*[{}()\[\]|])", r"\\left.", expr, flags=re.I)
    expr = re.sub(r"(?<!\\)\bright\b(?!\s*[{}()\[\]|])", r"\\right.", expr, flags=re.I)
    return expr


def convert_subscripts(expr: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(expr):
        if expr[i] not in "_^" or is_escaped(expr, i):
            out.append(expr[i])
            i += 1
            continue
        op = expr[i]
        j = i + 1
        while j < len(expr) and expr[j].isspace():
            j += 1
        if j < len(expr) and expr[j] == "{":
            end = matching_brace_index(expr, j)
            if end >= 0:
                content = convert_hwp_expression(expr[j + 1 : end])
                out.append(op + "{" + content + "}")
                i = end + 1
                continue
        end = script_token_end(expr, j)
        if end > j:
            content = convert_hwp_expression(expr[j:end])
            out.append(op + "{" + content + "}")
            i = end
            continue
        out.append(op)
        i += 1
    return "".join(out)


def script_token_end(expr: str, start: int) -> int:
    i = start
    saw = False
    while i < len(expr):
        ch = expr[i]
        if ch.isalnum() or "가" <= ch <= "힣":
            saw = True
            i += 1
            continue
        if ch == "," and saw:
            i += 1
            while i < len(expr) and expr[i].isspace():
                i += 1
            continue
        break
    return i if saw else start


def convert_symbols(expr: str) -> str:
    replacements = {
        "Isum": r"\sum",
        "sum": r"\sum",
        "prod": r"\prod",
        "int": r"\int",
        "TIMES": r"\times",
        "times": r"\times",
        "LEQ": r"\le",
        "GEQ": r"\ge",
        "NEQ": r"\ne",
        "leq": r"\le",
        "geq": r"\ge",
        "neq": r"\ne",
        "MIN": r"\min",
        "MAX": r"\max",
        "Min": r"\min",
        "Max": r"\max",
        "min": r"\min",
        "max": r"\max",
        "ln": r"\ln",
        "log": r"\log",
        "exp": r"\exp",
        "vert": r"\mid",
        "alpha": r"\alpha",
        "beta": r"\beta",
        "gamma": r"\gamma",
        "delta": r"\delta",
        "DELTA": r"\Delta",
        "epsilon": r"\epsilon",
        "varepsilon": r"\varepsilon",
        "theta": r"\theta",
        "lambda": r"\lambda",
        "mu": r"\mu",
        "rho": r"\rho",
        "sigma": r"\sigma",
        "SIGMA": r"\Sigma",
        "phi": r"\phi",
        "omega": r"\omega",
        "prime": r"\prime",
    }
    for word, latex in replacements.items():
        expr = re.sub(
            rf"(?<!\\)(?<![A-Za-z0-9_]){re.escape(word)}(?=(_|\b))",
            lambda _m, value=latex: value,
            expr,
        )
    return expr


def wrap_korean_text(expr: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(expr):
        if expr.startswith(r"\text{", i):
            end = matching_brace_index(expr, i + len(r"\text"))
            if end >= 0:
                out.append(expr[i : end + 1])
                i = end + 1
                continue
        if is_korean_text_char(expr[i]):
            start = i
            i += 1
            while i < len(expr) and (is_korean_text_char(expr[i]) or expr[i].isspace()):
                i += 1
            text = expr[start:i].strip()
            if text:
                out.append(r"\text{" + text + "}")
            continue
        out.append(expr[i])
        i += 1
    return "".join(out)


def is_korean_text_char(ch: str) -> bool:
    return "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㅎ" or "ㅏ" <= ch <= "ㅣ" or ch in "ㆍ․·"


def normalize_latex_spacing(expr: str) -> str:
    expr = re.sub(r"\s+", " ", expr).strip()
    expr = re.sub(r"\\(sum|prod|int)\s*_", lambda m: "\\" + m.group(1) + "_", expr)
    expr = re.sub(r"\\(min|max)\s+", lambda m: "\\" + m.group(1) + " ", expr)
    expr = re.sub(r"\s*([=,+\-])\s*", r" \1 ", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr


def replace_hash_linebreaks(expr: str) -> str:
    return re.sub(r"\s*#\s*", r" \\\\ ", expr)


def cleanup_latex(expr: str) -> str:
    expr = collapse_double_latex_command_slashes(expr)
    expr = re.sub(r"([_^])\{\s*\}", "", expr)
    expr = re.sub(r"\s+([_^]\{)", r"\1", expr)
    expr = collapse_repeated_linebreaks(expr)
    expr = re.sub(r"\^\(([^()]*)\)", r"^{\1}", expr)
    expr = re.sub(r"_\(([^()]*)\)", r"_{\1}", expr)
    expr = combine_repeated_scripts(expr)
    expr = normalize_script_commas(expr)
    expr = re.sub(r"\\operatorname\{([^{}]+)\}\s*_\{([^{}]+)\}", r"\\operatorname{\1}_{\2}", expr)
    expr = re.sub(r"\s+([)}\]])", r"\1", expr)
    expr = re.sub(r"([({\[])\s+", r"\1", expr)
    expr = balance_latex_groups(expr.strip())
    expr = normalize_script_commas(expr)
    return wrap_aligned_if_needed(expr)


def collapse_double_latex_command_slashes(expr: str) -> str:
    commands = (
        "bar|begin|Delta|div|end|exp|frac|ge|hat|int|lambda|le|left|ln|log|"
        "max|min|mid|ne|operatorname|prod|right|Sigma|sigma|sqrt|sum|text|times|varepsilon"
    )
    return re.sub(rf"\\\\(?=({commands})\b)", r"\\", expr)


def combine_repeated_scripts(expr: str) -> str:
    previous = None
    while previous != expr:
        previous = expr
        expr = re.sub(r"([A-Za-z0-9)}])_\{([^{}]+)\}_\{([^{}]+)\}", r"\1_{\2_{\3}}", expr)
        expr = re.sub(r"([A-Za-z0-9)}])\^\{([^{}]+)\}\^\{([^{}]+)\}", r"\1^{\2^{\3}}", expr)
    return expr


def normalize_script_commas(expr: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(expr):
        if expr[i] in "_^" and i + 1 < len(expr) and expr[i + 1] == "{":
            end = matching_brace_index(expr, i + 1)
            if end >= 0:
                content = re.sub(r"\s*,\s*", ",", expr[i + 2 : end])
                out.append(expr[i] + "{" + content + "}")
                i = end + 1
                continue
        out.append(expr[i])
        i += 1
    return "".join(out)


def collapse_repeated_linebreaks(expr: str) -> str:
    return re.sub(r"(?:\\\\\s*){2,}", lambda _m: r"\\ ", expr)


def wrap_aligned_if_needed(expr: str) -> str:
    if r"\begin{" in expr:
        return expr
    if "&" in expr or re.search(r"(?<!\\)\\\\(?![A-Za-z])", expr):
        return r"\begin{aligned}" + expr + r"\end{aligned}"
    return expr


def balance_latex_groups(expr: str) -> str:
    out: list[str] = []
    balance = 0
    for i, ch in enumerate(expr):
        if ch == "{" and not is_escaped(expr, i):
            balance += 1
            out.append(ch)
        elif ch == "}" and not is_escaped(expr, i):
            if balance == 0:
                continue
            balance -= 1
            out.append(ch)
        else:
            out.append(ch)
    if balance > 0:
        out.extend("}" for _ in range(balance))
    return "".join(out)


def is_escaped(text: str, index: int) -> bool:
    count = 0
    i = index - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


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


def clean_eqedit_script(script: str) -> str:
    script = "".join(ch if ch.isprintable() else " " for ch in script)
    script = script.replace("\ufffd", " ")
    return normalize_text(script)


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
