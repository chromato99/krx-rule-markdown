from __future__ import annotations

import re


PAST_RULE_ATTACHMENT_CODES = {"rea", "gae", "sin"}
PROFESSIONAL_ATTACHMENT_CODES = {"jun"}

PAST_RULE_ATTACHMENT_KEYWORDS = (
    "개정이유",
    "개정문",
    "개정조문",
    "개정지시문",
    "신구조문",
    "신ㆍ구조문",
    "신·구조문",
    "신・구조문",
    "신구 조문",
    "대비표",
)


def is_excluded_current_rule_attachment(
    title: str = "",
    file_name: str = "",
    server_file: str = "",
    att_id: str = "",
) -> bool:
    return is_professional_attachment(title, file_name, server_file, att_id) or is_past_rule_attachment(
        title,
        file_name,
        server_file,
        att_id,
    )


def is_professional_attachment(
    title: str = "",
    file_name: str = "",
    server_file: str = "",
    att_id: str = "",
) -> bool:
    text = attachment_text(title, file_name, server_file, att_id)
    return title.strip() == "전문" or has_attachment_code(text, PROFESSIONAL_ATTACHMENT_CODES)


def is_past_rule_attachment(
    title: str = "",
    file_name: str = "",
    server_file: str = "",
    att_id: str = "",
) -> bool:
    text = attachment_text(title, file_name, server_file, att_id)
    if has_attachment_code(text, PAST_RULE_ATTACHMENT_CODES):
        return True
    return any(keyword.lower() in text for keyword in PAST_RULE_ATTACHMENT_KEYWORDS)


def attachment_text(*parts: str) -> str:
    return " ".join(part for part in parts if part).strip().lower()


def has_attachment_code(text: str, codes: set[str]) -> bool:
    if not text:
        return False
    for code in codes:
        if f"filecd={code}" in text:
            return True
        if re.search(rf"(^|[-_/]){re.escape(code)}($|[-_.?/=&\s])", text):
            return True
    return False
