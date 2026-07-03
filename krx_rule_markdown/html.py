from __future__ import annotations

from html import escape, unescape
from html.parser import HTMLParser
import re


class MarkdownHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.strong = 0
        self.in_li = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "tr"}:
            self.newline(2)
        elif tag == "br":
            self.newline(1)
        elif tag == "li":
            self.newline(1)
            self.parts.append("- ")
            self.in_li = True
        elif tag in {"strong", "b"}:
            self.parts.append("**")
            self.strong += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "tr", "li"}:
            self.newline(2 if tag != "li" else 1)
            if tag == "li":
                self.in_li = False
        elif tag in {"strong", "b"} and self.strong:
            self.parts.append("**")
            self.strong -= 1

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self.parts.append(text.strip())

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


def html_to_markdown(html: str) -> str:
    parser = MarkdownHTMLParser()
    parser.feed(html)
    return parser.markdown()


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
