from __future__ import annotations

from http.cookiejar import CookieJar
from pathlib import Path
from urllib import error, parse, request
import json
import mimetypes
import re
import time

from .attachment_policy import is_excluded_current_rule_attachment
from .html import attr_value, elements_by_class, first_match, html_to_markdown, strip_tags
from .models import (
    ATTACHMENT_PENDING,
    DOCUMENT_NOTICE,
    DOCUMENT_RULE,
    Attachment,
    Document,
    Item,
    first_non_empty,
    hash_text,
    now_utc,
    slug,
)

DEFAULT_BASE_URL = "https://rule.krx.co.kr"
MAX_FILE_NAME_BYTES = 180


class Client:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, rate_limit: float = 0.7) -> None:
        self.base_url = base_url.rstrip("/")
        self.rate_limit = rate_limit
        self.last_request = 0.0
        self.csrf = ""
        self.csrf_header = "X-CSRF-TOKEN"
        opener = request.build_opener(request.HTTPCookieProcessor(CookieJar()))
        self.opener = opener

    def bootstrap(self) -> None:
        body = self.get("/out/index.do").decode("utf-8", errors="replace")
        self.update_csrf(body)

    def current_rule_items(self, limit: int = 0) -> list[Item]:
        self.ensure_session()
        items = self.walk_tree("0", [], limit)
        items.sort(key=lambda item: item.effective_date, reverse=True)
        return items

    def recent_items(self) -> list[Item]:
        self.ensure_session()
        body = self.get("/out/index.do").decode("utf-8", errors="replace")
        self.update_csrf(body)
        return parse_recent_items(body)

    def fetch_document(self, item: Item) -> Document:
        if item.document_type == DOCUMENT_NOTICE:
            return self.fetch_notice(item)
        return self.fetch_rule(item)

    def fetch_rule(self, item: Item) -> Document:
        self.ensure_session()
        body = self.post_form(
            "/out/regulation/regulationViewPop.do",
            {
                "bookid": first_non_empty(item.book_id, item.id),
                "noformyn": first_non_empty(item.noformyn, "N"),
                "_csrf": self.csrf,
            },
        ).decode("utf-8", errors="replace")
        item.state_history_id = first_non_empty(item.state_history_id, extract_state_history_id(body))
        return parse_rule_document(body, item, self.base_url)

    def fetch_notice(self, item: Item) -> Document:
        self.ensure_session()
        body = self.post_form(
            "/out/pds/pdsViewPop.do",
            {"BBSID": item.id, "Menuid": first_non_empty(item.menu_id, "10000016"), "_csrf": self.csrf},
        ).decode("utf-8", errors="replace")
        return parse_notice_document(body, item, self.base_url)

    def download_attachment(self, att: Attachment) -> tuple[Attachment, bytes]:
        self.ensure_session()
        if att.source_url == "/web/regulation/downloadRuleFile.do":
            att = self.resolve_rule_attachment(att)
        token_body = self.post_form("/login/getData.do", {})
        csrf = self.csrf
        try:
            csrf = json.loads(token_body.decode("utf-8")).get("data", {}).get("_csrf") or csrf
        except json.JSONDecodeError:
            pass
        body = self.post_form(
            "/Download.do",
            {
                "Serverfile": att.server_file,
                "Pcfilename": att.file_name,
                "folder": att.folder,
                "_csrf": csrf,
            },
            referer=attachment_referer(att),
        )
        validate_download(att, body)
        return att, body

    def download_rule_file(self, item: Item, filecd: str, title: str) -> tuple[Attachment, bytes]:
        self.ensure_session()
        state_history_id = first_non_empty(item.state_history_id)
        if not state_history_id:
            raise RuntimeError("statehistoryid is required to download a rule file")
        body = self.post_form(
            "/web/regulation/downloadRuleFile.do",
            {
                "bookid": first_non_empty(item.book_id, item.id),
                "statehistoryid": state_history_id,
                "filecd": filecd,
                "_csrf": self.csrf,
            },
        )
        data = json.loads(body.decode("utf-8"))
        file_map = data.get("fileMap")
        if not file_map:
            raise FileNotFoundError(f"stored rule file {filecd} is not available")
        att = Attachment(
            id=slug(f"{first_non_empty(item.book_id, item.id)}-{filecd.lower()}"),
            title=title,
            file_name=file_map.get("PCFILENAME", ""),
            server_file=file_map.get("SERVERFILE", ""),
            folder="ATTACH",
            source_url="/Download.do",
            status=ATTACHMENT_PENDING,
        )
        return self.download_attachment(att)

    def resolve_rule_attachment(self, att: Attachment) -> Attachment:
        values = dict(parse.parse_qsl(att.server_file))
        values["_csrf"] = self.csrf
        body = self.post_form("/web/regulation/downloadRuleFile.do", values)
        data = json.loads(body.decode("utf-8"))
        file_map = data.get("fileMap")
        if not file_map:
            raise RuntimeError("stored rule attachment not available")
        att.file_name = file_map.get("PCFILENAME", att.file_name)
        att.server_file = file_map.get("SERVERFILE", att.server_file)
        att.folder = "ATTACH"
        return att

    def walk_tree(self, node: str, parents: list[str], limit: int) -> list[Item]:
        nodes = self.tree_nodes(node)
        items: list[Item] = []
        for item in nodes:
            title = first_non_empty(str(item.get("title", "")), str(item.get("text", "")))
            if item.get("leaf") and int(item.get("bookid") or 0) > 0:
                book_id = str(item.get("bookid"))
                items.append(
                    Item(
                        id=book_id,
                        title=title,
                        category=" / ".join(parents),
                        book_id=book_id,
                        noformyn=first_non_empty(str(item.get("noformyn", "")), "N"),
                        effective_date=normalize_date(str(item.get("startdt", ""))),
                        published_date=normalize_date(str(item.get("promuldt", ""))),
                        document_type=DOCUMENT_RULE,
                        state_history_id=str(item.get("statehistoryid", "")),
                    )
                )
                if limit and len(items) >= limit:
                    return items
                continue
            items.extend(self.walk_tree(str(item.get("id")), [*parents, title], limit))
            if limit and len(items) >= limit:
                return items[:limit]
        return items

    def tree_nodes(self, node: str) -> list[dict[str, object]]:
        body = self.post_form(
            "/out/regulation/getTreeNode.do",
            {
                "node": node,
                "statecd": "현행",
                "mtype": "htree",
                "Menucd": "BYLAW",
                "webgbn": "OUT",
                "gbnid": "0",
                "gbn2": "out",
                "_csrf": self.csrf,
            },
        )
        return json.loads(body.decode("utf-8"))

    def get(self, path: str) -> bytes:
        return self.do("GET", path, None)

    def post_form(self, path: str, values: dict[str, str], referer: str = "") -> bytes:
        return self.do("POST", path, parse.urlencode(values).encode("utf-8"), referer=referer)

    def do(self, method: str, path: str, data: bytes | None, referer: str = "") -> bytes:
        last_error: Exception | None = None
        for attempt in range(4):
            self.throttle()
            req = request.Request(self.base_url + path, data=data, method=method)
            req.add_header("User-Agent", "krx-rule-mcp/0.1")
            if data is not None:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            if self.csrf:
                req.add_header(self.csrf_header, self.csrf)
            if referer:
                req.add_header("Referer", self.base_url + referer)
            try:
                with self.opener.open(req, timeout=40) as resp:
                    return resp.read()
            except error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                last_error = exc
            except error.URLError as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(min(8.0, 0.8 * (2**attempt)))
        if last_error:
            raise last_error
        raise RuntimeError("request failed without an exception")

    def throttle(self) -> None:
        if self.rate_limit <= 0:
            return
        elapsed = time.monotonic() - self.last_request
        if self.last_request and elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self.last_request = time.monotonic()

    def ensure_session(self) -> None:
        if not self.csrf:
            self.bootstrap()

    def update_csrf(self, body: str) -> None:
        token = first_match(r'<meta\b[^>]*name=["\']_csrf["\'][^>]*content=["\']([^"\']+)["\']', body)
        header = first_match(r'<meta\b[^>]*name=["\']_csrf_header["\'][^>]*content=["\']([^"\']+)["\']', body)
        if not token:
            raise RuntimeError("csrf token not found")
        self.csrf = token
        self.csrf_header = header or "X-CSRF-TOKEN"


def parse_recent_items(body: str) -> list[Item]:
    items: list[Item] = []
    for board in elements_by_class(body, "div", "boardA"):
        heading = strip_tags(first_match(r"<strong\b[^>]*>(.*?)</strong>", board))
        document_type = DOCUMENT_NOTICE if "예고" in heading else DOCUMENT_RULE
        for li in re.findall(r"<li\b[^>]*>.*?</li>", board, flags=re.I | re.S):
            p = first_match(r"(<p\b[^>]*>.*?</p>)", li)
            title = first_non_empty(attr_value(p, "title"), strip_tags(p))
            onclick = attr_value(p, "onclick")
            date = normalize_date(strip_tags(first_match(r"<span\b[^>]*>(.*?)</span>", li)))
            args = parse_js_args(onclick)
            if not title or len(args) < 2:
                continue
            if document_type == DOCUMENT_NOTICE:
                items.append(Item(id=args[0], title=title, menu_id=args[1], published_date=date, document_type=document_type))
            else:
                items.append(
                    Item(
                        id=args[0],
                        book_id=args[0],
                        noformyn=args[1],
                        title=title,
                        effective_date=date,
                        document_type=document_type,
                    )
                )
    return items


def parse_rule_document(body: str, item: Item, base_url: str) -> Document:
    title = strip_tags(first_match(r"<p\b[^>]*class=[\"'][^\"']*\btitle\b[^\"']*[\"'][^>]*>(.*?)</p>", body)) or item.title
    inner = first_match(r"<div\b[^>]*id=[\"']innerbody[\"'][^>]*>(.*?)</div>", body)
    body_md = html_to_markdown(inner or body)
    jang = strip_tags(first_match(r"<p\b[^>]*class=[\"'][^\"']*\bjang\b[^\"']*[\"'][^>]*>(.*?)</p>", body))
    effective = first_non_empty(extract_effective_date(jang), item.effective_date)
    published = first_non_empty(extract_promul_date(jang), item.published_date)
    attachments = parse_rule_attachments(body, item)
    doc = Document(
        id=first_non_empty(item.book_id, item.id),
        title=title,
        category=item.category,
        source_url=base_url + "/out/regulation/regulationViewPop.do",
        effective_date=effective,
        published_date=published,
        collected_at=now_utc(),
        attachments=attachments,
        document_type=DOCUMENT_RULE,
        body=body_md,
    )
    doc.content_hash = hash_text(doc.title + "\n" + doc.body)
    return doc


def extract_state_history_id(body: str) -> str:
    return first_non_empty(
        first_match(r'obj\.put\(["\']statehistoryid["\']\s*,\s*["\']([^"\']+)["\']', body),
        first_match(r'name\s*:\s*["\']statehistoryid["\']\s*,\s*value\s*:\s*["\']([^"\']+)["\']', body),
        first_match(r'statehistoryid\s*:\s*["\']([^"\']+)["\']', body),
    )


def parse_notice_document(body: str, item: Item, base_url: str) -> Document:
    pop = first_match(r"<[^>]*class=[\"'][^\"']*\bpopTT\b[^\"']*[\"'][^>]*>(.*?)</[^>]+>", body)
    title = item.title or strip_tags(re.sub(r"<a\b[^>]*>.*?</a>", " ", pop, flags=re.I | re.S))
    content = ""
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", body, flags=re.I | re.S):
        if "내용" in strip_tags(first_match(r"<th\b[^>]*>(.*?)</th>", tr)):
            content = first_match(r"<td\b[^>]*>(.*?)</td>", tr) or tr
            break
    doc = Document(
        id=item.id,
        title=title,
        category="규정 제·개정예고",
        source_url=base_url + "/out/pds/pdsViewPop.do",
        published_date=item.published_date,
        collected_at=now_utc(),
        attachments=parse_notice_attachments(body, item),
        document_type=DOCUMENT_NOTICE,
        body=html_to_markdown(content),
    )
    doc.content_hash = hash_text(doc.title + "\n" + doc.body)
    return doc


def parse_rule_attachments(body: str, item: Item) -> list[Attachment]:
    attachments: list[Attachment] = []
    seen = {(att.server_file, att.folder) for att in attachments}
    for att in parse_direct_download_attachments(body, item):
        key = (att.server_file, att.folder)
        if key in seen:
            continue
        seen.add(key)
        attachments.append(att)
    for tag in elements_by_class(body, "li", "filename") + elements_by_class(body, "span", "filename"):
        args = parse_js_args(attr_value(tag, "onclick"))
        if len(args) >= 3:
            title = strip_tags(tag)
            if is_excluded_current_rule_attachment(title, args[0], args[1]):
                continue
            key = (args[1], args[2])
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                Attachment(
                    id=slug(f"{first_non_empty(item.book_id, item.id)}-{args[1]}"),
                    title=title,
                    file_name=args[0],
                    server_file=args[1],
                    folder=args[2],
                    source_url="/Download.do",
                    status=ATTACHMENT_PENDING,
                )
            )
    return attachments


def parse_direct_download_attachments(body: str, item: Item) -> list[Attachment]:
    attachments: list[Attachment] = []
    doc_id = first_non_empty(item.book_id, item.id)
    for tag in onclick_tags(body, "downFile"):
        onclick = attr_value(tag, "onclick")
        if "downFileView" in onclick:
            continue
        args = parse_js_args(onclick)
        if len(args) < 3:
            continue
        title = strip_tags(tag)
        if is_excluded_current_rule_attachment(title, args[0], args[1]):
            continue
        attachments.append(
            Attachment(
                id=slug(f"{doc_id}-{args[1]}"),
                title=title,
                file_name=args[0],
                server_file=args[1],
                folder=args[2],
                source_url="/Download.do",
                status=ATTACHMENT_PENDING,
            )
        )
    return attachments


def onclick_tags(body: str, function_name: str) -> list[str]:
    out: list[str] = []
    for tag in ("span", "a", "li", "p"):
        pattern = rf"<{tag}\b[^>]*onclick=[\"'][^\"']*\b{re.escape(function_name)}\s*\([^>]*>.*?</{tag}>"
        out.extend(re.findall(pattern, body, flags=re.I | re.S))
    return out


def parse_notice_attachments(body: str, item: Item) -> list[Attachment]:
    attachments: list[Attachment] = []
    for tag in elements_by_class(body, "li", "filename"):
        args = parse_js_args(attr_value(tag, "onclick"))
        if len(args) < 3:
            continue
        attachments.append(
            Attachment(
                id=slug(f"{item.id}-{args[1]}"),
                title=strip_tags(tag),
                file_name=args[0],
                server_file=args[1],
                folder=args[2],
                source_url="/out/pds/pdsViewPop.do",
                status=ATTACHMENT_PENDING,
            )
        )
    return attachments


def safe_base(name: str, fallback: str = "") -> str:
    base = Path(name or fallback or "attachment.bin").name
    if not Path(base).suffix:
        suffix = Path(parse.urlparse(fallback).path).suffix
        if suffix:
            base += suffix
    return truncate_file_name(base)


def truncate_file_name(name: str, max_bytes: int = MAX_FILE_NAME_BYTES) -> str:
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    out: list[str] = []
    used = 0
    for ch in stem:
        ch_len = len(ch.encode("utf-8"))
        if used + ch_len > budget:
            break
        out.append(ch)
        used += ch_len
    shortened = "".join(out).rstrip(" ._-")
    return (shortened or "attachment") + suffix


def attachment_referer(att: Attachment) -> str:
    if att.folder == "BBS" or "/pds/" in att.source_url:
        return "/out/pds/pdsViewPop.do"
    return "/out/regulation/regulationViewPop.do"


def validate_download(att: Attachment, body: bytes) -> None:
    trimmed = body.strip()
    lower = trimmed[:128].decode("utf-8", errors="ignore").lower()
    if lower.startswith("<script") or lower.startswith("<html") or "alert(" in lower:
        raise RuntimeError("download returned HTML error response")
    if Path(att.file_name).suffix.lower() == ".pdf" and not trimmed.startswith(b"%PDF-"):
        raise RuntimeError("downloaded file is not a PDF")


def normalize_date(raw: str) -> str:
    raw = raw.strip().strip(".")
    match = re.search(r"([0-9]{4})\s*[.\-/]\s*([0-9]{1,2})\s*[.\-/]\s*([0-9]{1,2})", raw)
    if not match:
        return raw if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) else ""
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def extract_effective_date(text: str) -> str:
    return normalize_date(first_match(r"시행일\s*:\s*([0-9]{4}\s*[.\-/]\s*[0-9]{1,2}\s*[.\-/]\s*[0-9]{1,2})", text))


def extract_promul_date(text: str) -> str:
    return normalize_date(first_match(r"([0-9]{4}\s*[.\-/]\s*[0-9]{1,2}\s*[.\-/]\s*[0-9]{1,2})", text))


def parse_js_args(call: str) -> list[str]:
    inner = js_call_inner(call)
    if not inner:
        return re.findall(r"'([^']*)'", call or "")
    args: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    for ch in inner:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        if ch == "," and depth == 0:
            args.append(eval_string_concat("".join(current)))
            current = []
            continue
        current.append(ch)
    if current:
        args.append(eval_string_concat("".join(current)))
    return args


def js_call_inner(call: str) -> str:
    call = call or ""
    start = call.find("(")
    if start < 0:
        return ""
    quote = ""
    depth = 0
    for idx in range(start, len(call)):
        ch = call[idx]
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return call[start + 1 : idx]
    return call[start + 1 :]


def eval_string_concat(expr: str) -> str:
    parts = re.findall(r"'([^']*)'|\"([^\"]*)\"", expr or "")
    if not parts:
        return expr.strip()
    return "".join(left or right for left, right in parts)


def guess_mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or ""
