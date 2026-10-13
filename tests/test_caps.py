import pytest
from pathlib import Path
from src.tg.filters import check_media_caps, RuntimeChatResolver


def test_media_caps_pre_download():
    limits = {
        "voice": {"max_bytes": 2 * 1024 * 1024, "max_seconds": 180},
        "video": {"max_bytes": 15 * 1024 * 1024, "max_seconds": 60},
        "photo": {"max_bytes": 5 * 1024 * 1024},
        "doc": {"allowed": False}
    }

    # 25 MB video -> rejected before download
    ok, reason = check_media_caps("video", 25 * 1024 * 1024, 30, limits)
    assert not ok
    assert "превышает лимит" in reason

    # 10 MB video -> allowed
    ok, _ = check_media_caps("video", 10 * 1024 * 1024, 30, limits)
    assert ok

    # Document -> rejected
    ok, reason = check_media_caps("doc", 1024, None, limits)
    assert not ok

    # Voice > 180s -> rejected
    ok, reason = check_media_caps("voice", 1024, 200, limits)
    assert not ok


def test_runtime_chat_resolver(tmp_path: Path):
    state_file = tmp_path / "runtime_state.json"
    resolver = RuntimeChatResolver(initial_chat_id=-100111222, state_file=str(state_file))

    assert resolver.get_allowed_chat_id() == -100111222
    assert resolver.is_allowed(-100111222)
    assert not resolver.is_allowed(-100999888)

    # Migrate chat
    resolver.set_allowed_chat_id(-100999888)
    assert resolver.get_allowed_chat_id() == -100999888
    assert resolver.is_allowed(-100999888)

    # Persisted to file
    resolver2 = RuntimeChatResolver(initial_chat_id=-100111222, state_file=str(state_file))
    assert resolver2.get_allowed_chat_id() == -100999888
