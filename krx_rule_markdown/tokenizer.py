from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
ASCII_SINGLE_RE = re.compile(r"[0-9A-Za-z]")


def tokenize(text: str) -> list[str]:
    raw = TOKEN_RE.findall(text.lower())
    seen: set[str] = set()
    tokens: list[str] = []
    for token in raw:
        add_token(tokens, seen, token)
        if is_hangul_token(token):
            for n in (2, 3):
                if len(token) < n:
                    continue
                for i in range(0, len(token) - n + 1):
                    add_token(tokens, seen, token[i : i + n])
    return tokens


def add_token(tokens: list[str], seen: set[str], token: str) -> None:
    if not token:
        return
    if len(token) == 1 and not ASCII_SINGLE_RE.fullmatch(token):
        return
    if token in seen:
        return
    seen.add(token)
    tokens.append(token)


def is_hangul_token(token: str) -> bool:
    return bool(token) and all("가" <= ch <= "힣" for ch in token)


def chunk_text(text: str, max_chars: int = 1600) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if max_chars <= 0:
        max_chars = 1600
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        para_len = len(para)
        if current_len > 0 and current_len + para_len + 2 > max_chars:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0
        if para_len > max_chars:
            if current:
                chunks.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            for start in range(0, para_len, max_chars):
                chunks.append(para[start : start + max_chars].strip())
            continue
        current.append(para)
        current_len += para_len + (2 if current_len else 0)
    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks
