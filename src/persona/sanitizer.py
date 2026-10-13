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


# Forbidden structural tokens per Point 2: [], {}, <request> or prompt tags
STRUCTURAL_DUMP_RE = re.compile(
    r"(\[|\]|\{|\}|<request|<summary|<current|<history|<dossier|<media|<source|```)",
    re.IGNORECASE
)

# Section markers and headers
SECTION_MARKERS_RE = re.compile(
    r"(#{1,6}\s+|---|===|анализ\s*:|разбор\s*:|режим\s*:|правила\s*:|контекст\s*:|instructions?\s*:|system\s*:|output\s*:)",
    re.IGNORECASE
)

# Bullet list detection (dump sign)
BULLET_LIST_RE = re.compile(
    r"(^|\n)\s*([-*•]|\d+\.)\s+",
    re.MULTILINE
)


def validate_persona_reply(
    reply: str,
    max_chars: int = 2000,
    is_media: bool = False
) -> tuple[bool, Optional[str]]:
    """
    Validates persona reply per Point 2 of specification:
    - 1-3 sentences default, reasonable length
    - no structural leak tokens: [], {}, <request>, <summary>, etc.
    - no section markers (###, ---, Анализ:, Правила:, etc.)
    - no dump indicators:
      * too many colons (>= 4 colons in a short text)
      * bullet lists (- / * / 1.)
      * JSON fences or braces
    - for media: no forbidden words Исполнитель / Жанр / Правила
    Returns (is_valid, rejection_reason).
    """
    if not reply or not reply.strip():
        return False, "Empty reply"

    text = reply.strip()

    # 1. Length check
    if len(text) > max_chars:
        return False, f"Reply length {len(text)} exceeds limit {max_chars}"

    # 2. Structural tokens: [], {}, <request>, prompt tags, code fences
    if STRUCTURAL_DUMP_RE.search(text):
        return False, "Contains structural leak tokens (brackets, braces, prompt tags, or code fences)"

    # 3. Prompt / reasoning leaks (Анализ трека:, Правила поведения:, etc.)
    if has_persona_leak(text, is_media=is_media):
        return False, "Contains prompt/reasoning leak or forbidden section headers"

    # 4. Section markers (###, ---, ===, Режим:, etc.)
    if SECTION_MARKERS_RE.search(text):
        return False, "Contains section markers (markdown headers, horizontal rules, section titles)"

    # 5. Dump indicators: too many colons (>= 4)
    if text.count(":") >= 4:
        return False, "Too many colons (field-value dump sign)"

    # 6. Dump indicators: bullet lists (e.g. - item 1\n- item 2)
    list_matches = BULLET_LIST_RE.findall(text)
    if len(list_matches) >= 2:
        return False, "Contains list formatting (dump sign)"

    return True, None




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
