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


def extract_reply_snapshot(reply: Any) -> Optional[dict[str, Any]]:
    """
    R5: слепок реплай-цели из апдейта (когда цели нет в БД: старая/вычищена).
    Возвращает plain-dict: message_id, user_id, alias, nick, text, caption,
    media_kind, file_id, file_unique_id, file_size, duration, via_bot, is_bot.
    file_id цели брать из апдейта даже если цели нет в БД (медиа-пинг).
    """
    if reply is None:
        return None

    def _g(obj: Any, name: str, default: Any = None) -> Any:
        return getattr(obj, name, default) if obj is not None else default

    user = _g(reply, "from_user")
    user_id = _g(user, "id")
    nick = _g(user, "full_name", None) or _g(_g(reply, "sender_chat"), "title", None) or "Anon"
    alias = f"u{user_id}" if user_id else "u0"

    text = _g(reply, "text") or _g(reply, "caption") or ""
    caption = _g(reply, "caption")
    quote = _g(reply, "quote")
    quote_text = _g(quote, "text") if quote else None
    if quote_text:
        text = f"{text} [цитата: {quote_text}]".strip()

    media_kind = None
    file_id = None
    file_unique_id = None
    file_size = 0
    duration = None

    sticker = _g(reply, "sticker")
    animation = _g(reply, "animation")
    voice = _g(reply, "voice")
    video_note = _g(reply, "video_note")
    video = _g(reply, "video")
    photo = _g(reply, "photo")
    audio = _g(reply, "audio")
    poll = _g(reply, "poll")
    dice = _g(reply, "dice")

    if sticker is not None:
        media_kind = "sticker"
        text = text or format_sticker_stub(_g(sticker, "emoji"), _g(sticker, "set_name"))
    elif animation is not None:
        media_kind = "animation"
        file_unique_id = _g(animation, "file_unique_id")
        file_id = _g(animation, "file_id")
        file_size = _g(animation, "file_size") or 0
        duration = _g(animation, "duration")
        text = text or format_animation_stub(caption)
    elif voice is not None:
        media_kind = "voice"
        file_unique_id = _g(voice, "file_unique_id")
        file_id = _g(voice, "file_id")
        file_size = _g(voice, "file_size") or 0
        duration = _g(voice, "duration")
        text = text or "[войс]"
    elif video_note is not None:
        media_kind = "video_note"
        file_unique_id = _g(video_note, "file_unique_id")
        file_id = _g(video_note, "file_id")
        file_size = _g(video_note, "file_size") or 0
        duration = _g(video_note, "duration")
        text = text or "[кружочек]"
    elif video is not None:
        media_kind = "video"
        file_unique_id = _g(video, "file_unique_id")
        file_id = _g(video, "file_id")
        file_size = _g(video, "file_size") or 0
        duration = _g(video, "duration")
        text = text or "[видео]"
    elif photo:
        media_kind = "photo"
        photo_obj = photo[-1]
        file_unique_id = _g(photo_obj, "file_unique_id")
        file_id = _g(photo_obj, "file_id")
        file_size = _g(photo_obj, "file_size") or 0
        text = text or "[фото]"
    elif audio is not None:
        media_kind = "audio"
        file_unique_id = _g(audio, "file_unique_id")
        file_id = _g(audio, "file_id")
        file_size = _g(audio, "file_size") or 0
        duration = _g(audio, "duration")
        meta_parts = []
        if _g(audio, "performer"):
            meta_parts.append(audio.performer)
        if _g(audio, "title"):
            meta_parts.append(audio.title)
        audio_info = f" ({' - '.join(meta_parts)})" if meta_parts else ""
        text = text or f"[аудио{audio_info}]"
    elif poll is not None:
        media_kind = "poll"
        text = text or format_poll_stub(_g(poll, "question", ""), [ _g(o, "text", "") for o in (_g(poll, "options") or []) ])
    elif dice is not None:
        media_kind = "dice"
        text = text or format_dice_stub(_g(dice, "emoji", ""), _g(dice, "value", 0))
    elif _g(reply, "contact") is not None:
        media_kind = "contact"
        text = text or "[контакт]"
    elif _g(reply, "location") is not None or _g(reply, "venue") is not None:
        media_kind = "location"
        text = text or "[геопозиция]"

    via = _g(reply, "via_bot")
    via_bot = _g(via, "username") if via else None
    is_bot = bool(_g(user, "is_bot", False))

    return {
        "message_id": _g(reply, "message_id", 0),
        "user_id": user_id,
        "alias": alias,
        "nick": nick,
        "text": text,
        "caption": caption,
        "media_kind": media_kind,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "file_size": file_size,
        "duration": duration,
        "via_bot": via_bot,
        "is_bot": is_bot,
    }


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
