import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.ops.config import BotConfig, AppConfig, PersonaConfig
from src.db.manager import DatabaseManager
from src.tg.bot import FunnyBotApp
from src.context.assemble import ContextAssembler


@pytest.fixture
def mock_app(tmp_path: Path):
    db_file = tmp_path / "test_logs.db"
    db_mgr = DatabaseManager(str(db_file), str(tmp_path / "backups"))

    bot_cfg = BotConfig(
        MODEL_ID="fast",
        OWNER_IDS=[111],
        ALLOWED_CHAT_ID=-1001,
    )
    persona_cfg = PersonaConfig()
    app_cfg = AppConfig(bot=bot_cfg, persona=persona_cfg, keys=[], limits={}, gen={}, tg_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345")

    app = FunnyBotApp(config=app_cfg, db_manager=db_mgr)
    app.bot = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_logs_command_non_admin_rejected(mock_app, tmp_path: Path):
    message = AsyncMock()
    message.chat.id = -1001
    message.message_id = 42

    msg_dict = {
        "chat_id": -1001,
        "message_id": 42,
        "user_id": 999,  # Not in OWNER_IDS
        "text": "/logs",
    }

    # Mock get_chat_member to return regular member
    mock_member = MagicMock()
    mock_member.status = "member"
    mock_app.bot.get_chat_member = AsyncMock(return_value=mock_member)

    await mock_app._handle_logs_command(message, msg_dict)

    message.reply.assert_called_once_with("Команда доступна только админам.")


@pytest.mark.asyncio
async def test_logs_command_admin_success(mock_app, tmp_path: Path, monkeypatch):
    # Prepare dummy log file
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot.log"
    dummy_lines = [f"2026-10-16 00:00:{i:02d} [INFO] [funny_bot] Log line {i}" for i in range(1, 60)]
    log_file.write_text("\n".join(dummy_lines), encoding="utf-8")

    # Patch log_path in bot
    monkeypatch.chdir(tmp_path)

    message = AsyncMock()
    message.chat.id = -1001
    message.message_id = 50

    # 1. Default 15 lines
    msg_dict = {
        "chat_id": -1001,
        "message_id": 50,
        "user_id": 111,  # Owner
        "text": "/logs",
    }
    await mock_app._handle_logs_command(message, msg_dict)
    assert message.reply.called
    call_args, call_kwargs = message.reply.call_args
    reply_text = call_args[0]
    # Must contain lines 45 to 59 (last 15 lines)
    assert "Log line 59" in reply_text
    assert "Log line 45" in reply_text
    assert "Log line 44" not in reply_text
    assert call_kwargs.get("parse_mode") == "HTML"

    # 2. Custom n = 5
    message.reset_mock()
    msg_dict["text"] = "/logs 5"
    await mock_app._handle_logs_command(message, msg_dict)
    call_args, _ = message.reply.call_args
    reply_text = call_args[0]
    assert "Log line 59" in reply_text
    assert "Log line 55" in reply_text
    assert "Log line 54" not in reply_text

    # 3. Clamped n = 100 -> max 50
    message.reset_mock()
    msg_dict["text"] = "/logs 100"
    await mock_app._handle_logs_command(message, msg_dict)
    call_args, _ = message.reply.call_args
    reply_text = call_args[0]
    assert "Log line 59" in reply_text
    assert "Log line 10" in reply_text
    assert "Log line 9" not in reply_text


@pytest.mark.asyncio
async def test_logs_command_does_not_pollute_context_or_db(tmp_path: Path):
    db_file = tmp_path / "test_context.db"
    db_mgr = DatabaseManager(str(db_file), str(tmp_path / "backups"))
    await db_mgr.connect()
    await db_mgr.apply_migrations("migrations")

    bot_cfg = BotConfig(
        MODEL_ID="fast",
        OWNER_IDS=[111],
        ALLOWED_CHAT_ID=-1001,
    )
    app_cfg = AppConfig(bot=bot_cfg, persona=PersonaConfig(), keys=[], limits={}, gen={}, tg_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345")

    app = FunnyBotApp(config=app_cfg, db_manager=db_mgr)
    app.bot = AsyncMock()

    # Create dummy user message
    await db_mgr.save_message({
        "chat_id": -1001,
        "message_id": 1,
        "user_id": 222,
        "alias": "u222",
        "nick": "Alice",
        "text": "Привет всем",
        "created_at": "2026-10-16T00:00:00",
    })

    from datetime import datetime, timezone
    from aiogram.enums import ChatType

    mock_msg = AsyncMock()
    mock_msg.chat = MagicMock(id=-1001, type=ChatType.SUPERGROUP, title="Test Group")
    mock_msg.message_id = 2
    mock_msg.from_user = MagicMock(id=111, is_bot=False, full_name="Admin")
    mock_msg.date = datetime.now(timezone.utc)
    mock_msg.text = "/logs 15"
    mock_msg.caption = None
    mock_msg.reply_to_message = None
    mock_msg.migrate_to_chat_id = None

    mock_msg.photo = None
    mock_msg.video = None
    mock_msg.video_note = None
    mock_msg.audio = None
    mock_msg.voice = None
    mock_msg.document = None
    mock_msg.sticker = None
    mock_msg.animation = None
    mock_msg.poll = None
    mock_msg.dice = None
    mock_msg.forward_from = None
    mock_msg.forward_from_chat = None

    # Handle incoming message
    await app.handle_message(mock_msg)

    # 1. Message must NOT be saved in DB
    saved_msg = await db_mgr.get_message(-1001, 2)
    assert saved_msg is None

    # 2. Check last messages from DB
    last_msgs = await db_mgr.get_last_messages(-1001, limit=10)
    assert len(last_msgs) == 1
    assert last_msgs[0]["message_id"] == 1
    assert last_msgs[0]["text"] == "Привет всем"

    from src.context.prompts import PromptBuilder
    prompt_builder = PromptBuilder("prompts")
    assembler = ContextAssembler(bot_cfg, db_mgr, prompt_builder)
    # Even if someone directly inserted a /logs message in DB:
    await db_mgr.save_message({
        "chat_id": -1001,
        "message_id": 3,
        "user_id": 111,
        "alias": "u111",
        "nick": "Admin",
        "text": "/logs 50",
        "created_at": "2026-10-16T00:00:01",
    })
    current_msg = {
        "chat_id": -1001,
        "message_id": 4,
        "user_id": 222,
        "alias": "u222",
        "nick": "Alice",
        "text": "Как дела?",
        "created_at": "2026-10-16T00:00:02",
    }
    _, contents = await assembler.assemble(-1001, current_msg, mode="ping")
    assembled_text = contents[0]["parts"][0]["text"]
    assert "/logs" not in assembled_text

    await db_mgr.close()
