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
    assert mod.should_force_search("смешнявка, сделай ресерч пожалуйста") is True
    assert mod.should_force_search("смешнявка, сделай ресёрч пожалуйста") is True
    assert mod.should_force_search("прогугли что там") is True
    assert mod.should_force_search("поищи инфу") is True
    assert mod.should_force_search("какие новости сегодня") is True
    assert mod.should_force_search("обычный текст ни о чем") is False

    # Soft triggers
    assert mod.should_soft_search("А в связи с последними событиями как тебе мем") is True
    assert mod.should_soft_search("что случилось в мире") is True
    assert mod.should_soft_search("поясни за этот тренд") is True
    assert mod.should_soft_search("просто привет") is False


def test_anonymize_query():
    raw = "кто такой @vladimir u123456 и 1234567890?"
    anon = anonymize_query(raw, known_nicks=["vladimir"])
    assert "@vladimir" not in (anon or "")
    assert "u123456" not in (anon or "")
    assert "1234567890" not in (anon or "")
    assert "кто такой" in (anon or "")
