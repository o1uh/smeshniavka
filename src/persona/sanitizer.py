import re
from typing import Any, Optional


ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "tg-spoiler"}

# Matches HTML tags: <tag ...> or </tag>
TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9_-]+)([^>]*)>")
UN_RE = re.compile(r"\b(u\d+)\b")
USER_ID_RE = re.compile(r"\b\d{8,12}\b")

# Outgoing Leak Guards per I11/R3
# Patterns: Анализ трека: | Правила поведения | Контекст: | "action".*"reply" | Role: | Goal:
LEAK_PATTERN = re.compile(
    r"(анализ трека\s*:|исполнитель\s*:|правила поведения|контекст\s*:|\"action\"\s*.*\"reply\"|role\s*:|goal\s*:)",
    re.IGNORECASE | re.DOTALL
)

# I11/R3: голые uN, @username, tg://-ссылки в исходящих запрещены всегда
MENTION_PATTERN = re.compile(
    r"(\bu\d+\b|@\w+|tg://\S+|<a\s+href=\"tg://[^>]*>)",
    re.IGNORECASE
)

# I11/R2: извинения и лесть запрещены (точные фразы I11; голое «понял» вне
# «понял извини» — обычное слово, не баним, иначе ложняки вида «сам-то понял?»)
FLATTERY_PATTERN = re.compile(
    r"(понял\s+извини|извини(сь)?|прости(те)?|больше не буду|отличный вопрос|ты прав|согласен|как ии|надеюсь[,\s]+помог)",
    re.IGNORECASE
)

# For mode_media_reply: forbid words Исполнитель / Жанр / Правила in outgoing reply
MEDIA_FORBIDDEN_PATTERN = re.compile(
    r"\b(исполнитель\s*:?|жанр\s*:?|правила\s*:?)\b",
    re.IGNORECASE
)

# Склейка «констатация, подъёб» через запятую в коротком ответе:
# две мысли, слепленные запятой вместо одной резкой
def has_comma_glue(text: str, max_len: int = 200) -> bool:
    """True, если короткий ответ слеплен из двух мыслей через запятую.

    Длинные ответы по запросу (объяснения) не трогаем — там запятые норма.
    """
    t = (text or "").strip()
    if not t or len(t) > max_len:
        return False
    if t.count(",") < 1:
        return False
    # одна запятая, обе части самодостаточны — склейка
    parts = [p.strip() for p in t.split(",")]
    if len(parts) == 2 and all(len(p) >= 8 for p in parts):
        return True
    if t.count(",") >= 2:
        return True
    return False


def _norm_words(text: str) -> set[str]:
    return set(re.findall(r"[а-яёa-z]{3,}", (text or "").lower()))


def is_repetitive(candidate: str, recent_bot_texts: list[str], threshold: float = 0.6) -> bool:
    """True, если кандидат повторяет одну из своих последних реплик.

    Сравниваем пересечение знаменательных слов: тот же панч теми же словами.
    """
    cand = _norm_words(candidate)
    if len(cand) < 3:
        return False
    for prev in recent_bot_texts or []:
        prev_words = _norm_words(prev)
        if len(prev_words) < 3:
            continue
        inter = cand & prev_words
        # доля общих слов от меньшей реплики
        ratio = len(inter) / max(1, min(len(cand), len(prev_words)))
        if ratio >= threshold:
            return True
    return False


# Cliché insult patterns: noun + adjective or adjective + noun templates like
# "ебака грозный", "клоун комнатный", "боец диванный", "диванный воин", "мамкин бандит", etc.
CLICHE_INSULT_RE = re.compile(
    r"\b(ебака|клоун|боец|воин|эксперт|бандит|гений|мыслитель|терпила)\s+"
    r"(грозный|комнатный|диванный|хуев|великий|недоделанный)\b|"
    r"\b(грозный|комнатный|диванный|мамкин|великий)\s+"
    r"(ебака|клоун|боец|воин|бандит|эксперт|гений|мыслитель|терпила)\b",
    re.IGNORECASE
)


def has_persona_leak(text: str, is_media: bool = False) -> bool:
    """
    I11/R3: исходящий гард — дамп/маркеры, упоминания, лесть.
    - Анализ трека: | Исполнитель: | Правила поведения | Контекст: | "action".*"reply" | Role: | Goal:
    - голые uN, @username, tg:// (всегда, is_media не важен)
    - извинения/лесть (понял извини, ты прав, отличный вопрос, ...)
    - для медиа: также Жанр:/Правила: (is_media=True)
    """
    if not text:
        return False
    if LEAK_PATTERN.search(text):
        return True
    if MENTION_PATTERN.search(text):
        return True
    if FLATTERY_PATTERN.search(text):
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

    # 1. Length check (I11: 1–2000 символов)
    if len(text) < 1 or len(text) > max_chars:
        return False, f"Reply length {len(text)} outside 1-{max_chars}"

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

    # 7. I11: списки 3+ строк с двоеточием (field-value dump)
    colon_lines = sum(1 for line in text.split("\n") if ":" in line)
    if colon_lines >= 3:
        return False, "Contains 3+ colon lines (dump sign)"

    # 8. Cliché adjective-noun insult template rejection
    if CLICHE_INSULT_RE.search(text):
        return False, "Contains cliché adjective-noun insult template"

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
    I7/R3: остатки uN/@username/tg:// вырезать, НЕ превращать в упоминание.
    - известный uN -> короткий ник plain-текстом (без фамилии-ссылки)
    - неизвестный uN -> вырезать
    - @username / tg://user -> вырезать
    """
    if not text:
        return ""

    def replace_match(m: re.Match) -> str:
        alias = m.group(1)
        user_info = user_mapping.get(alias)
        if not user_info:
            return ""  # Cut unresolved uN
        nick = (user_info.get("nick") or alias).strip()
        # Только короткий ник plain-текстом, без упоминаний
        return nick.split()[0] if nick else ""

    res = UN_RE.sub(replace_match, text)
    # Вырезать @username и tg://-ссылки наружу (I7: запрещены всегда)
    res = re.sub(r"@\w+", "", res)
    res = re.sub(r'<a\s+href="tg://[^"]*">.*?</a>', "", res, flags=re.IGNORECASE | re.DOTALL)
    res = re.sub(r"tg://\S+", "", res)
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
