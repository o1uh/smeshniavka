import pytest
from src.persona.sanitizer import (
    has_persona_leak,
    clean_media_reply_boilerplate,
)


def test_leak_pattern_detection():
    leak_examples = [
        "Анализ трека: качественный бит и вокал",
        "анализ трека : вот подробный разбор",
        "Правила поведения: бот должен отвечать кратко",
        "Контекст: в чате спорят о музыке",
        '{"action": "reply", "reply": "привет"}',
        'something "action": "reply" something',
        "Role: system assistant",
        "Goal: roaste user",
    ]
    for ex in leak_examples:
        assert has_persona_leak(ex), f"Failed to detect leak in: {ex}"


def test_normal_replies_pass():
    safe_examples = [
        "Ну и шляпу ты скинул, если честно.",
        "75/100 — бодрый трек, гитарное соло тащит.",
        "Сам разберись со своими делами.",
        "Погнали дальше, не тормозите тред.",
    ]
    for ex in safe_examples:
        assert not has_persona_leak(ex), f"False positive on safe example: {ex}"


def test_media_forbidden_words():
    # In media reply mode, words "Исполнитель", "Жанр", "Правила" are forbidden
    media_leaks = [
        "Исполнитель: Nirvana\nЖанр: Grunge\nКрутой трек",
        "Исполнитель — неизвестен, жанр попса",
        "Правила оценки треков соблюдены",
    ]
    for ex in media_leaks:
        assert has_persona_leak(ex, is_media=True), f"Failed to flag media leak in: {ex}"

    # In general ping mode, "исполнитель" in normal conversation is not flagged
    assert not has_persona_leak("Кто твой любимый исполнитель?", is_media=False)


def test_clean_media_reply_boilerplate():
    raw_response = (
        "Исполнитель: Linkin Park\n"
        "Жанр: Alternative Rock\n"
        "Правила: оценка объективна\n"
        "Бодрый трек из нулевых, ностальгия пробила.\n"
        "85/100"
    )
    cleaned = clean_media_reply_boilerplate(raw_response)
    assert "Исполнитель" not in cleaned
    assert "Жанр" not in cleaned
    assert "Правила" not in cleaned
    assert "Бодрый трек из нулевых, ностальгия пробила." in cleaned
    assert "85/100" in cleaned
