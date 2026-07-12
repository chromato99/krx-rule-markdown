from __future__ import annotations

from html import escape, unescape
from html.parser import HTMLParser
import re
from dataclasses import dataclass
from urllib.parse import urljoin


IGNORED_CONTENT_TAGS = {"script", "style"}
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class TableCell:
    text: str
    header: bool = False
    rowspan: int = 1
    colspan: int = 1


class MarkdownHTMLParser(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url.rstrip("/") + "/" if base_url else ""
        self.parts: list[str] = []
        self.strong = 0
        self.in_li = False
        self.table_rows: list[list[TableCell]] | None = None
        self.current_row: list[TableCell] | None = None
        self.current_cell_parts: list[str] | None = None
        self.current_cell_header = False
        self.current_cell_rowspan = 1
        self.current_cell_colspan = 1
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in IGNORED_CONTENT_TAGS:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "table":
            self.newline(2)
            self.table_rows = []
        elif tag == "tr" and self.table_rows is not None:
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            attr = dict(attrs)
            self.current_cell_parts = []
            self.current_cell_header = tag == "th"
            self.current_cell_rowspan = positive_int(attr.get("rowspan"), 1)
            self.current_cell_colspan = positive_int(attr.get("colspan"), 1)
        elif tag == "br" and self.current_cell_parts is not None:
            self.current_cell_parts.append("<br>")
        elif tag in {"p", "div", "tr"} and self.table_rows is None:
            self.newline(2)
        elif tag == "br":
            self.newline(1)
        elif tag == "li":
            self.newline(1)
            self.parts.append("- ")
            self.in_li = True
        elif tag in {"strong", "b"}:
            if self.current_cell_parts is None:
                self.parts.append("**")
            self.strong += 1
        elif tag == "img":
            self.handle_image(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.ignored_depth or tag in IGNORED_CONTENT_TAGS:
            return
        if tag == "img":
            self.handle_image(attrs)
        elif tag == "br" and self.current_cell_parts is not None:
            self.current_cell_parts.append("<br>")
        elif tag == "br":
            self.newline(1)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in IGNORED_CONTENT_TAGS:
            if self.ignored_depth:
                self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in {"td", "th"} and self.current_row is not None and self.current_cell_parts is not None:
            self.current_row.append(
                TableCell(
                    text=normalize_cell_text("".join(self.current_cell_parts)),
                    header=self.current_cell_header,
                    rowspan=self.current_cell_rowspan,
                    colspan=self.current_cell_colspan,
                )
            )
            self.current_cell_parts = None
        elif tag == "tr" and self.table_rows is not None and self.current_row is not None:
            if self.current_row:
                self.table_rows.append(self.current_row)
            self.current_row = None
        elif tag == "table" and self.table_rows is not None:
            self.parts.append(render_markdown_table(self.table_rows))
            self.table_rows = None
            self.newline(2)
        elif tag in {"p", "div", "tr", "li"} and self.table_rows is None:
            self.newline(2 if tag != "li" else 1)
            if tag == "li":
                self.in_li = False
        elif tag in {"strong", "b"} and self.strong:
            if self.current_cell_parts is None:
                self.parts.append("**")
            self.strong -= 1

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        text = re.sub(r"\s+", " ", data)
        if not text:
            return
        if self.current_cell_parts is not None:
            self.current_cell_parts.append(text)
        else:
            self.parts.append(text)

    def handle_image(self, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        src = (attr.get("src") or "").strip()
        if "dataFile/law/img/" not in src:
            return
        if self.base_url:
            src = urljoin(self.base_url, src)
        if self.current_cell_parts is not None:
            self.current_cell_parts.append(f"[이미지: {src}]")
            return
        self.newline(2)
        self.parts.append(f"[이미지: {src}]")
        self.newline(2)

    def newline(self, count: int) -> None:
        if not self.parts:
            return
        suffix = "\n" * count
        if not "".join(self.parts[-2:]).endswith(suffix):
            self.parts.append(suffix)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str, base_url: str = "") -> str:
    parser = MarkdownHTMLParser(base_url)
    parser.feed(html)
    return parser.markdown()


def positive_int(value: str | None, fallback: int) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        return fallback
    return max(1, parsed)


def normalize_cell_text(text: str) -> str:
    text = re.sub(r"\s*(<br>)\s*", r"<br>", text)
    return re.sub(r"\s+", " ", text).strip()


def render_markdown_table(rows: list[list[TableCell]]) -> str:
    if not rows:
        return ""
    if any(cell.rowspan > 1 or cell.colspan > 1 for row in rows for cell in row):
        return render_html_table(rows)
    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    padded = [row + [TableCell("") for _ in range(width - len(row))] for row in rows]
    header = padded[0]
    body = padded[1:]
    lines = [
        "| " + " | ".join(markdown_cell(cell.text) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(markdown_cell(cell.text) for cell in row) + " |")
    return "\n\n" + "\n".join(lines) + "\n\n"


def render_html_table(rows: list[list[TableCell]]) -> str:
    lines = ["\n\n<table>"]
    for row in rows:
        lines.append("  <tr>")
        for cell in row:
            tag = "th" if cell.header else "td"
            attrs = ""
            if cell.rowspan > 1:
                attrs += f' rowspan="{cell.rowspan}"'
            if cell.colspan > 1:
                attrs += f' colspan="{cell.colspan}"'
            lines.append(f"    <{tag}{attrs}>{html_cell_text(cell.text)}</{tag}>")
        lines.append("  </tr>")
    lines.append("</table>\n\n")
    return "\n".join(lines)


def markdown_cell(text: str) -> str:
    return text.replace("|", r"\|").replace("\n", "<br>")


def html_cell_text(text: str) -> str:
    return escape(text, quote=False).replace("&lt;br&gt;", "<br>")


class ElementByIDParser(HTMLParser):
    def __init__(self, element_id: str) -> None:
        super().__init__(convert_charrefs=False)
        self.element_id = element_id
        self.parts: list[str] = []
        self.depth = 0
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if self.depth == 0:
            if any(name.lower() == "id" and value == self.element_id for name, value in attrs):
                self.depth = 1
            return
        self.parts.append(render_start_tag(tag, attrs))
        if tag.lower() not in VOID_ELEMENTS:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.depth > 0 and not self.done:
            self.parts.append(render_start_tag(tag, attrs)[:-1] + " />")

    def handle_endtag(self, tag: str) -> None:
        if self.depth == 0 or self.done:
            return
        self.depth -= 1
        if self.depth == 0:
            self.done = True
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.depth > 0 and not self.done:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.depth > 0 and not self.done:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.depth > 0 and not self.done:
            self.parts.append(f"&#{name};")

    def html(self) -> str:
        return "".join(self.parts).strip()


def render_start_tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    rendered = []
    for name, value in attrs:
        if value is None:
            rendered.append(f" {name}")
        else:
            rendered.append(f' {name}="{escape(value, quote=True)}"')
    return f"<{tag}{''.join(rendered)}>"


def element_by_id(html: str, element_id: str) -> str:
    parser = ElementByIDParser(element_id)
    parser.feed(html)
    return parser.html()


def strip_tags(html: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def first_match(pattern: str, text: str, default: str = "", flags: int = re.I | re.S) -> str:
    match = re.search(pattern, text, flags)
    if not match:
        return default
    return unescape(match.group(1)).strip()


def elements_by_class(html: str, tag: str, class_name: str) -> list[str]:
    pattern = rf"<{tag}\b(?=[^>]*\bclass=[\"'][^\"']*\b{re.escape(class_name)}\b)[^>]*>.*?</{tag}>"
    return re.findall(pattern, html, flags=re.I | re.S)


def attr_value(tag_html: str, name: str) -> str:
    double_quoted = re.search(rf"\b{re.escape(name)}=\"([^\"]*)\"", tag_html, flags=re.I | re.S)
    if double_quoted:
        return unescape(double_quoted.group(1)).strip()
    single_quoted = re.search(rf"\b{re.escape(name)}='([^']*)'", tag_html, flags=re.I | re.S)
    if single_quoted:
        return unescape(single_quoted.group(1)).strip()
    return ""
