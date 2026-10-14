from types import SimpleNamespace

from src.tg.stubs import extract_reply_snapshot


def _user(uid=42, name="Вован Пупкин"):
    return SimpleNamespace(id=uid, full_name=name, is_bot=False)


def test_none_returns_none():
    assert extract_reply_snapshot(None) is None


def test_text_reply_with_quote():
    reply = SimpleNamespace(
        message_id=100, from_user=_user(), text="привет братва",
        caption=None, quote=SimpleNamespace(text="ау"),
        sticker=None, animation=None, voice=None, video_note=None,
        video=None, photo=None, audio=None, poll=None, dice=None,
        contact=None, location=None, venue=None, via_bot=None,
        sender_chat=None,
    )
    snap = extract_reply_snapshot(reply)
    assert snap["message_id"] == 100
    assert snap["alias"] == "u42"
    assert "привет братва" in snap["text"]
    assert "ау" in snap["text"]
    assert snap["media_kind"] is None
    assert snap["file_id"] is None


def test_photo_reply_keeps_file_id():
    photo = SimpleNamespace(file_unique_id="uniq1", file_id="file1", file_size=1000)
    reply = SimpleNamespace(
        message_id=101, from_user=_user(), text=None, caption="зацени",
        quote=None, sticker=None, animation=None, voice=None,
        video_note=None, video=None, photo=[photo], audio=None,
        poll=None, dice=None, contact=None, location=None, venue=None,
        via_bot=None, sender_chat=None,
    )
    snap = extract_reply_snapshot(reply)
    assert snap["media_kind"] == "photo"
    assert snap["file_id"] == "file1"
    assert snap["file_unique_id"] == "uniq1"
    assert snap["caption"] == "зацени"


def test_video_reply_sizes():
    video = SimpleNamespace(
        file_unique_id="v1", file_id="vf1", file_size=20_000_000, duration=120,
    )
    reply = SimpleNamespace(
        message_id=102, from_user=_user(), text=None, caption=None,
        quote=None, sticker=None, animation=None, voice=None,
        video_note=None, video=video, photo=None, audio=None,
        poll=None, dice=None, contact=None, location=None, venue=None,
        via_bot=None, sender_chat=None,
    )
    snap = extract_reply_snapshot(reply)
    assert snap["media_kind"] == "video"
    assert snap["file_size"] == 20_000_000
    assert snap["duration"] == 120


def test_sticker_has_no_file_id():
    sticker = SimpleNamespace(emoji="😀", set_name="pack")
    reply = SimpleNamespace(
        message_id=103, from_user=_user(), text=None, caption=None,
        quote=None, sticker=sticker, animation=None, voice=None,
        video_note=None, video=None, photo=None, audio=None,
        poll=None, dice=None, contact=None, location=None, venue=None,
        via_bot=None, sender_chat=None,
    )
    snap = extract_reply_snapshot(reply)
    assert snap["media_kind"] == "sticker"
    assert snap["file_id"] is None
    assert "😀" in snap["text"]


def test_via_bot_audio_meta():
    audio = SimpleNamespace(
        file_unique_id="a1", file_id="af1", file_size=5000, duration=180,
        performer=" Gruppa ", title="Trek",
    )
    reply = SimpleNamespace(
        message_id=104, from_user=SimpleNamespace(id=777, full_name="MusicBot", is_bot=True),
        text=None, caption=None, quote=None, sticker=None, animation=None,
        voice=None, video_note=None, video=None, photo=None, audio=audio,
        poll=None, dice=None, contact=None, location=None, venue=None,
        via_bot=SimpleNamespace(username="TgSoundBot"), sender_chat=None,
    )
    snap = extract_reply_snapshot(reply)
    assert snap["media_kind"] == "audio"
    assert snap["via_bot"] == "TgSoundBot"
    assert snap["is_bot"] is True
    assert "Gruppa" in snap["text"]
