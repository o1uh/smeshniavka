import re
from typing import Any, Optional
from urllib.parse import urlparse

from src.context.prompts import sanitize_nick


def format_sticker_stub(emoji: Optional[str], set_name: Optional[str]) -> str:
    """Format sticker stub per R5/TB-14: [стикер: 😀 · набор <title>]."""
    parts = ["[стикер"]
    if emoji:
        parts.append(f": {emoji}")
    if set_name:
        safe_title = sanitize_nick(set_name, max_len=32)
        parts.append(f" · набор {safe_title}")
    parts.append("]")
    return "".join(parts)


def format_animation_stub(caption: Optional[str] = None) -> str:
    """Format GIF/animation stub: [гифка] + caption."""
    if caption and caption.strip():
        return f"[гифка] {caption.strip()}"
    return "[гифка]"


def format_poll_stub(question: str, options: list[str], max_len: int = 300) -> str:
    """Format poll stub: [опрос: вопрос; варианты: a / b / c], truncated to 300 chars."""
    opts_str = " / ".join(options)
    res = f"[опрос: {question}; варианты: {opts_str}]"
    return res[:max_len]


def format_dice_stub(emoji: str, value: int) -> str:
    """Format dice stub: [кубик 🎲 4]."""
    return f"[кубик {emoji} {value}]"


def extract_clean_urls(text: Optional[str], entities: Optional[list[Any]] = None, max_urls: int = 3) -> list[str]:
    """
    Extracts up to 3 URLs (domain + path, without query or fragment) from text and entities.
    """
    if not text:
        return []

    found_raw: list[str] = []

    # Regex search for URLs
    url_pattern = re.compile(r"https?://[^\s<>\"'()]+")
    for m in url_pattern.finditer(text):
        found_raw.append(m.group(0))

    clean_urls: list[str] = []
    seen = set()

    for raw in found_raw:
        try:
            parsed = urlparse(raw)
            if not parsed.netloc:
                continue
            # Keep only scheme, netloc, and path
            cleaned = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
            if cleaned not in seen:
                seen.add(cleaned)
                clean_urls.append(cleaned)
                if len(clean_urls) >= max_urls:
                    break
        except Exception:
            continue

    return clean_urls


def format_links_in_context(text: str, urls: list[str]) -> str:
    """
    Appends [ссылка: домен/путь] to text context if not already annotated.
    """
    if not urls:
        return text

    annotations = []
    for u in urls:
        parsed = urlparse(u)
        short = f"{parsed.netloc}{parsed.path}"
        annotations.append(f"[ссылка: {short}]")

    return f"{text} {' '.join(annotations)}".strip()
