from __future__ import annotations

from html import unescape
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
