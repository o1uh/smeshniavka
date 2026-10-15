from unittest.mock import MagicMock
import pytest

from src.gateway.client import convert_contents_to_messages
from src.tg.bot import extract_media_info
from src.research.search import ResearchModule, anonymize_query
from src.ops.config import BotConfig


def test_convert_contents_to_messages_media():
    contents = [
        {
            "role": "user",
            "parts": [
                {"text": "Посмотри это фото"},
                {"inline_data": {"mime_type": "image/jpeg", "data": "aW1hZ2VkYXRh"}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"text": "Послушай это аудио"},
                {"inline_data": {"mime_type": "audio/ogg", "data": "YXVkaW9kYXRh"}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"text": "Глянь видео"},
                {"inline_data": {"mime_type": "video/mp4", "data": "dmlkZW9kYXRh"}},
            ],
        },
    ]

    messages = convert_contents_to_messages(contents, system_instruction="Sys")
    assert messages[0] == {"role": "system", "content": "Sys"}

    # Image block
    assert messages[1]["role"] == "user"
    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][0] == {"type": "text", "text": "Посмотри это фото"}
    assert messages[1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,aW1hZ2VkYXRh"},
    }

    # Audio block
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][1] == {
        "type": "audio_url",
        "audio_url": {"url": "data:audio/ogg;base64,YXVkaW9kYXRh"},
    }

    # Video block
    assert messages[3]["role"] == "user"
    assert messages[3]["content"][1] == {
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,dmlkZW9kYXRh"},
    }


def test_extract_media_info_photo():
    msg = MagicMock()
    msg.sticker = None
    msg.animation = None
    msg.voice = None
    msg.video_note = None
    msg.video = None
    msg.audio = None
    photo_part = MagicMock(file_unique_id="uniq_123", file_id="fid_123", file_size=4096)
    msg.photo = [photo_part]
    msg.caption = "Подпись к фото"

    info = extract_media_info(msg)
    assert info.media_kind == "photo"
    assert info.file_id == "fid_123"
    assert info.file_unique_id == "uniq_123"
    assert info.file_size == 4096
    assert info.caption == "Подпись к фото"
    assert info.text_stub == "[фото]"


def test_extract_media_info_voice():
    msg = MagicMock()
    msg.sticker = None
    msg.animation = None
    msg.photo = None
    msg.video_note = None
    msg.video = None
    msg.audio = None
    msg.voice = MagicMock(file_unique_id="voice_u", file_id="voice_fid", file_size=10240, duration=15)
    msg.caption = None

    info = extract_media_info(msg)
    assert info.media_kind == "voice"
    assert info.file_id == "voice_fid"
    assert info.duration == 15
    assert info.text_stub == "[войс]"


def test_extract_media_info_sticker_static():
    msg = MagicMock()
    msg.animation = None
    msg.photo = None
    msg.voice = None
    msg.video_note = None
    msg.video = None
    msg.audio = None
    msg.caption = None
    sticker = MagicMock(
        file_unique_id="stk_uniq",
        file_id="stk_fid",
        file_size=2048,
        emoji="😎",
        set_name="CoolPack",
        is_animated=False,
        is_video=False,
        thumbnail=None
    )
    msg.sticker = sticker

    info = extract_media_info(msg)
    assert info.media_kind == "sticker"
    assert info.file_id == "stk_fid"
    assert info.file_unique_id == "stk_uniq"
    assert info.file_size == 2048
    assert "😎" in (info.text_stub or "")


def test_extract_media_info_sticker_animated_with_thumb():
    msg = MagicMock()
    msg.animation = None
    msg.photo = None
    msg.voice = None
    msg.video_note = None
    msg.video = None
    msg.audio = None
    msg.caption = None
    thumb = MagicMock(
        file_unique_id="thumb_uniq",
        file_id="thumb_fid",
        file_size=1024
    )
    sticker = MagicMock(
        file_unique_id="anim_stk_uniq",
        file_id="anim_stk_fid",
        file_size=50000,
        emoji="🔥",
        set_name="AnimPack",
        is_animated=True,
        is_video=False,
        thumbnail=thumb
    )
    msg.sticker = sticker

    info = extract_media_info(msg)
    assert info.media_kind == "sticker"
    assert info.file_id == "thumb_fid"
    assert info.file_unique_id == "thumb_uniq"
    assert info.file_size == 1024


def test_research_triggers():
    cfg = BotConfig()
    cfg.SEARCH_FORCE_TRIGGERS = ["ресёрч", "поищи", "прогугли", "новости"]
    mod = ResearchModule(
        bot_config=cfg,
        db_manager=MagicMock(),
        gateway=MagicMock(),
        prompt_builder=MagicMock(),
    )

    # Force triggers
    assert mod.should_force_search("дик, сделай ресерч пожалуйста") is True
    assert mod.should_force_search("дик, сделай ресёрч пожалуйста") is True
    assert mod.should_force_search("прогугли что там") is True
    assert mod.should_force_search("поищи инфу") is True
    assert mod.should_force_search("какие новости сегодня") is True
    assert mod.should_force_search("обычный текст ни о чем") is False

    # Soft triggers
    assert mod.should_soft_search("А в связи с последними событиями как тебе мем") is True
    assert mod.should_soft_search("что случилось в мире") is True
    assert mod.should_soft_search("поясни за этот тренд") is True
    assert mod.should_soft_search("а причем тут иркутск") is True
    assert mod.should_soft_search("в чем мем") is True
    assert mod.should_soft_search("что за мем вообще") is True
    assert mod.should_soft_search("просто привет") is False


def test_anonymize_query():
    raw = "кто такой @vladimir u123456 и 1234567890 дик?"
    anon = anonymize_query(raw, known_nicks=["vladimir"], bot_aliases=["дик"])
    assert "@vladimir" not in (anon or "")
    assert "u123456" not in (anon or "")
    assert "1234567890" not in (anon or "")
    assert "дик" not in (anon or "").lower()
    assert "кто такой" in (anon or "")


def test_anonymize_keeps_dikij_intact():
    """«дикий» — обычное слово, вырезать из него «дик» нельзя."""
    anon = anonymize_query("дикий трек", known_nicks=[], bot_aliases=["дик"])
    assert anon == "дикий трек"


def test_convert_contents_with_search_query_directive():
    from src.gateway.client import convert_contents_to_messages
    contents = [{"role": "user", "parts": [{"text": "в чем мем"}]}]
    messages = convert_contents_to_messages(contents, system_instruction="SysPrompt")
    assert len(messages) == 2
    assert messages[0]["content"] == "SysPrompt"
    assert messages[1]["content"] == "в чем мем"


def test_verify_and_patch_reply_score_audio_vs_photo():
    from src.media.analyzer import MediaAnalyzer
    analyzer = MediaAnalyzer(bot_config=MagicMock(), db_manager=MagicMock(), gateway=MagicMock(), prompt_builder=MagicMock())

    # Audio should get score appended if missing
    audio_item = {"kind": "audio", "score100": 85, "score_reason": "крутой бит"}
    res_audio = analyzer.verify_and_patch_reply_score("нормальный трек", audio_item)
    assert "85/100 — крутой бит" in res_audio

    # Photo should NEVER get score appended even if score100 was in cache
    photo_item = {"kind": "photo", "score100": 70, "score_reason": "забавный кроссовер"}
    res_photo = analyzer.verify_and_patch_reply_score("обычный шизокроссовер", photo_item)
    assert "/100" not in res_photo
    assert res_photo == "обычный шизокроссовер"

    # Photo should have any hallucinated score stripped
    res_photo_hallucinated = analyzer.verify_and_patch_reply_score("обычный шизокроссовер\n70/100 — круто", photo_item)
    assert "/100" not in res_photo_hallucinated
    assert "70/100" not in res_photo_hallucinated


