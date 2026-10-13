import re
from typing import Any, Optional


ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "tg-spoiler"}

# Matches HTML tags: <tag ...> or </tag>
TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9_-]+)([^>]*)>")
UN_RE = re.compile(r"\b(u\d+)\b")
USER_ID_RE = re.compile(r"\b\d{8,12}\b")

# Outgoing Leak Guards per user requirement
# Patterns: Анализ трека: | Правила поведения | Контекст: | "action".*"reply" | Role: | Goal:
LEAK_PATTERN = re.compile(
    r"(анализ трека\s*:|правила поведения|контекст\s*:|\"action\"\s*.*\"reply\"|role\s*:|goal\s*:)",
    re.IGNORECASE | re.DOTALL
)

# For mode_media_reply: forbid words Исполнитель / Жанр / Правила in outgoing reply
MEDIA_FORBIDDEN_PATTERN = re.compile(
    r"\b(исполнитель\s*:?|жанр\s*:?|правила\s*:?)\b",
    re.IGNORECASE
)


def has_persona_leak(text: str, is_media: bool = False) -> bool:
    """
    Checks if model output contains internal prompt/reasoning leaks:
    - Анализ трека: | Правила поведения | Контекст: | "action".*"reply" | Role: | Goal:
    - For media replies: also checks for forbidden words Исполнитель | Жанр | Правила
    """
    if not text:
        return False
    if LEAK_PATTERN.search(text):
        return True
    if is_media and MEDIA_FORBIDDEN_PATTERN.search(text):
        return True
    return False


def clean_media_reply_boilerplate(text: str) -> str:
    """
    Strips boilerplate template lines like 'Исполнитель: ...' or 'Жанр: ...' or 'Правила: ...'
    leaving only the real commentary and N/100 score.
    """
    if not text:
        return ""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if MEDIA_FORBIDDEN_PATTERN.search(line):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()



def sanitize_telegram_html(text: str) -> str:
    """
    Sanitizes HTML for Telegram Bot API:
    - keeps only allowed tags: b, i, u, s, a, code, pre, blockquote, tg-spoiler
    - strips/escapes all unknown tags
    - escapes raw < and > outside valid tags
    """
    if not text:
        return ""

    def replace_tag(match: re.Match) -> str:
        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        attributes = match.group(3)

        if tag_name not in ALLOWED_TAGS:
            # Escape disallowed tag
            return match.group(0).replace("<", "&lt;").replace(">", "&gt;")

        if tag_name == "a":
            # For <a> tag, ensure only href attribute is preserved
            href_m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', attributes, re.IGNORECASE)
            if href_m and not is_closing:
                href = href_m.group(1)
                return f'<a href="{href}">'
            elif is_closing:
                return "</a>"
            return ""

        return f"</{tag_name}>" if is_closing else f"<{tag_name}>"

    sanitized = TAG_RE.sub(replace_tag, text)
    return sanitized


def strip_all_tags(text: str) -> str:
    """Removes all HTML tags, returning plain text."""
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def replace_un_aliases(text: str, user_mapping: dict[str, dict[str, Any]]) -> str:
    """
    Replaces uN pseudonyms (e.g. u3) with member mention or clean nick:
    - if member has username: @username
    - else: <a href="tg://user?id=123">Nick</a>
    - strips remaining unresolved uN references.
    """
    if not text:
        return ""

    def replace_match(m: re.Match) -> str:
        alias = m.group(1)
        user_info = user_mapping.get(alias)
        if not user_info:
            return ""  # Cut unresolved uN

        username = user_info.get("username")
        user_id = user_info.get("user_id")
        nick = user_info.get("nick") or alias

        if username:
            return f"@{username.lstrip('@')}"
        elif user_id:
            return f'<a href="tg://user?id={user_id}">{nick}</a>'
        return nick

    res = UN_RE.sub(replace_match, text)
    # Never leak raw numeric user_ids
    res = USER_ID_RE.sub("[id]", res)
    return res


def split_message_chunks(text: str, max_chars: int = 4096) -> list[str]:
    """Splits a long message into chunks <= 4096 characters on paragraph/line boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            if len(line) > max_chars:
                # Force split very long line
                for i in range(0, len(line), max_chars):
                    chunks.append(line[i:i + max_chars])
                continue

        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks
