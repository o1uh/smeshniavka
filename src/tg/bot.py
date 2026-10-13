import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import UpdateType, ChatType
from aiogram.filters import Command

from src.ops.config import AppConfig
from src.ops.log import logger, shadow_logger
from src.db.manager import DatabaseManager
from src.db.contracts import TgSender
from src.gateway.client import GeminiGateway
from src.context.prompts import PromptBuilder, sanitize_nick, escape_xml
from src.context.assemble import ContextAssembler
from src.persona.reply import PingDetector, AuthorSpamLimiter, TgSendQueue
from src.persona.proactive import ProactiveEngine
from src.persona.sanitizer import replace_un_aliases
from src.dossier.worker import DossierWorker
from src.media.analyzer import MediaAnalyzer
from src.research.search import ResearchModule
from src.tg.stubs import (
    format_sticker_stub,
    format_animation_stub,
    format_poll_stub,
    format_dice_stub,
    extract_clean_urls,
)
from src.tg.filters import RuntimeChatResolver, check_media_caps
from src.ops.alerts import AlertManager


class RealTgSender(TgSender):
    """Real Telegram sender using aiogram Bot instance."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "HTML",
        disable_web_page_preview: bool = True
    ) -> Optional[int]:
        res = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview
        )
        return res.message_id

    async def leave_chat(self, chat_id: int) -> bool:
        return await self.bot.leave_chat(chat_id)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self.bot.send_chat_action(chat_id=chat_id, action=action)


class FunnyBotApp:
    """Main Telegram Bot Application coordinating all subsystems."""

    def __init__(
        self,
        config: AppConfig,
        db_manager: DatabaseManager,
        bot: Optional[Bot] = None
    ):
        self.config = config
        self.bot_cfg = config.bot
        self.db = db_manager

        self.bot = bot or Bot(token=config.tg_token)
        self.dp = Dispatcher()
        self.sender = RealTgSender(self.bot)

        self.chat_resolver = RuntimeChatResolver(self.bot_cfg.ALLOWED_CHAT_ID)
        self.alert_mgr = AlertManager(self.sender, self.bot_cfg.OWNER_IDS)
        self.gateway = GeminiGateway(config)
        self.prompts = PromptBuilder()
        self.assembler = ContextAssembler(self.bot_cfg, self.db, self.prompts)
        self.send_queue = TgSendQueue(self.sender, self.bot_cfg)
        self.proactive = ProactiveEngine(self.bot_cfg)
        self.author_limiter = AuthorSpamLimiter(self.bot_cfg, config.persona)
        self.dossier_worker = DossierWorker(self.bot_cfg, self.db, self.gateway, self.prompts)
        self.media_analyzer = MediaAnalyzer(self.bot_cfg, self.db, self.gateway, self.prompts)
        self.research = ResearchModule(self.bot_cfg, self.db, self.gateway, self.prompts)

        self.ping_detector: Optional[PingDetector] = None
        self.bot_id = 0
        self.bot_username = ""
        self.start_time = time.time()
        self.muted_until: float = 0.0

        self._setup_handlers()

    def _setup_handlers(self) -> None:
        self.dp.message.register(self.handle_message)

    async def initialize(self) -> None:
        me = await self.bot.get_me()
        self.bot_id = me.id
        self.bot_username = me.username or "funny_bot"
        self.ping_detector = PingDetector(self.bot_username, self.bot_id, self.bot_cfg)
        self.send_queue.start()
        logger.info("Bot initialized: @%s (id=%d)", self.bot_username, self.bot_id)

    async def handle_message(self, message: types.Message) -> None:
        chat = message.chat
        user = message.from_user
        text = message.text or message.caption or ""

        # 1. Private chat handling
        if chat.type == ChatType.PRIVATE:
            if user and user.id in self.bot_cfg.OWNER_IDS:
                if text.strip().startswith("/start"):
                    await message.answer("Смешнявка на связи. Алерты будут приходить сюда.")
                elif text.strip().startswith("/status"):
                    summary = self.gateway.get_key_status_summary()
                    await message.answer(f"Статус ключей: {json.dumps(summary, indent=2)}")
            return  # Non-owner DMs ignored per I3

        # 2. Check for chat migration (group -> supergroup)
        if message.migrate_to_chat_id:
            new_id = message.migrate_to_chat_id
            old_id = chat.id
            logger.info("Chat migration detected: %s -> %s", old_id, new_id)
            self.chat_resolver.set_allowed_chat_id(new_id)
            await self.db.migrate_chat_id(old_id, new_id)
            await self.alert_mgr.send_alert("CHAT_MIGRATION", f"Чат мигрировал из {old_id} в {new_id}")
            return

        # 3. Foreign chat filtering per I3
        if not self.chat_resolver.is_allowed(chat.id):
            logger.warning("Message from foreign chat %s (%s). Leaving chat...", chat.id, chat.title)
            await self.db.add_foreign_chat(chat.id, chat.title)
            try:
                await self.bot.leave_chat(chat.id)
            except Exception:
                pass
            await self.alert_mgr.send_alert(
                "FOREIGN_CHAT_LEFT",
                f"Бот был добавлен в посторонний чат {chat.id} ({chat.title}) и покинул его."
            )
            return

        # 4. Check stale update
        msg_age = time.time() - message.date.timestamp()
        is_stale = msg_age > self.bot_cfg.STALE_UPDATE_S

        # 5. Extract sender kind, media kind, stubs
        media_kind = None
        file_unique_id = None
        file_id = None
        file_size = 0
        duration = None

        if message.sticker:
            media_kind = "sticker"
            text = format_sticker_stub(message.sticker.emoji, message.sticker.set_name)
        elif message.animation:
            media_kind = "animation"
            file_unique_id = message.animation.file_unique_id
            file_id = message.animation.file_id
            file_size = message.animation.file_size or 0
            duration = message.animation.duration
            text = format_animation_stub(message.caption)
        elif message.voice:
            media_kind = "voice"
            file_unique_id = message.voice.file_unique_id
            file_id = message.voice.file_id
            file_size = message.voice.file_size or 0
            duration = message.voice.duration
            text = text or "[войс]"
        elif message.video_note:
            media_kind = "video_note"
            file_unique_id = message.video_note.file_unique_id
            file_id = message.video_note.file_id
            file_size = message.video_note.file_size or 0
            duration = message.video_note.duration
            text = text or "[кружочек]"
        elif message.video:
            media_kind = "video"
            file_unique_id = message.video.file_unique_id
            file_id = message.video.file_id
            file_size = message.video.file_size or 0
            duration = message.video.duration
            text = text or "[видео]"
        elif message.photo:
            media_kind = "photo"
            photo_obj = message.photo[-1]
            file_unique_id = photo_obj.file_unique_id
            file_id = photo_obj.file_id
            file_size = photo_obj.file_size or 0
            text = text or "[фото]"
        elif message.audio:
            media_kind = "audio"
            file_unique_id = message.audio.file_unique_id
            file_id = message.audio.file_id
            file_size = message.audio.file_size or 0
            duration = message.audio.duration
            meta_parts = []
            if message.audio.performer:
                meta_parts.append(message.audio.performer)
            if message.audio.title:
                meta_parts.append(message.audio.title)
            audio_info = f" ({' - '.join(meta_parts)})" if meta_parts else ""
            text = text or f"[аудио{audio_info}]"
        elif message.poll:
            media_kind = "poll"
            opts = [o.text for o in message.poll.options]
            text = format_poll_stub(message.poll.question, opts)
        elif message.dice:
            media_kind = "dice"
            text = format_dice_stub(message.dice.emoji, message.dice.value)
        elif message.contact:
            media_kind = "contact"
            text = "[контакт]"
        elif message.location or message.venue:
            media_kind = "location"
            text = "[геопозиция]"

        # Check media caps BEFORE downloading
        media_allowed = True
        if media_kind and file_size > 0:
            media_allowed, cap_reason = check_media_caps(
                media_kind, file_size, duration, self.config.limits
            )
            if not media_allowed:
                logger.info("Media rejected before download: %s", cap_reason)

        # 6. Extract URLs
        urls = extract_clean_urls(text)
        urls_json = json.dumps(urls) if urls else None

        user_id = user.id if user else None
        nick = sanitize_nick(user.full_name if user else "Anon", max_len=32)
        alias = f"u{user_id}" if user_id else "u0"

        # Reply info
        reply_to_mid = message.reply_to_message.message_id if message.reply_to_message else None
        reply_to_uid = (
            message.reply_to_message.from_user.id
            if message.reply_to_message and message.reply_to_message.from_user
            else None
        )

        msg_dict = {
            "chat_id": chat.id,
            "message_id": message.message_id,
            "user_id": user_id,
            "alias": alias,
            "nick": nick,
            "text": text,
            "urls": urls_json,
            "media_kind": media_kind,
            "file_unique_id": file_unique_id,
            "reply_to": reply_to_mid,
            "reply_to_user_id": reply_to_uid,
            "created_at": message.date.astimezone(timezone.utc).isoformat(),
        }

        # Save to database
        await self.db.save_message(msg_dict)

        # If update is stale, do not generate responses
        if is_stale:
            return

        # 7. Check bot commands
        cmd_handled = await self._handle_commands(message, msg_dict)
        if cmd_handled:
            return

        # 8. Check Ping
        assert self.ping_detector is not None
        is_ping = self.ping_detector.is_ping(msg_dict)

        if is_ping and user_id:
            # Check author spam limit
            allowed, excuse = self.author_limiter.check_author(user_id)
            if not allowed:
                if excuse:
                    await self.send_queue.enqueue(
                        chat_id=chat.id,
                        text=excuse,
                        reply_to_message_id=message.message_id,
                        priority="P0"
                    )
                return

            await self._process_ping(message, msg_dict, media_kind, file_id, file_unique_id)
            self.ping_detector.record_bot_reply(user_id)
            self.proactive.on_ping_reply_sent()
        else:
            self.ping_detector.record_other_message()
            # 9. Check Proactive if not muted
            now = time.time()
            if now > self.muted_until:
                should_eval = self.proactive.record_incoming_message(user_id)
                if should_eval:
                    can_trigger, reject_reason = self.proactive.check_proactive_caps()
                    if can_trigger:
                        await self._process_proactive(message, msg_dict)

        # 10. Check dossier extraction window (every 40 messages)
        await self.dossier_worker.process_window(chat.id, message.message_id)

    async def _handle_commands(self, message: types.Message, msg_dict: dict[str, Any]) -> bool:
        text = (msg_dict.get("text") or "").strip()
        user_id = msg_dict.get("user_id")

        if text.startswith("/alive"):
            uptime_min = (time.time() - self.start_time) / 60.0
            key_summary = self.gateway.get_key_status_summary()
            shadow_tag = " [SHADOW]" if self.bot_cfg.SHADOW_MODE else " [LIVE]"
            status_text = (
                f"🤖 <b>Смешнявка</b>{shadow_tag}\n"
                f"Аптайм: {uptime_min:.1f} мин\n"
                f"Модель: <code>{self.bot_cfg.MODEL_ID}</code>\n"
                f"Ключи Gemini: {key_summary['active_keys']} акт / {key_summary['reserve_keys']} рез "
                f"(кулдаун: {key_summary['in_cooldown']}, мертвых: {key_summary['dead_keys']})\n"
                f"Дневной RPD: {key_summary['total_rpd_used']} / {key_summary['total_rpd_cap']}"
            )
            await message.reply(status_text, parse_mode="HTML")
            return True

        elif text.startswith("/me"):
            alias = msg_dict.get("alias")
            await message.reply(f"Твой псевдоним: <code>{alias}</code>\nID: <code>{user_id}</code>")
            return True

        elif text.startswith("/forget me") and user_id:
            await self.db.set_opt_out(message.chat.id, user_id)
            await self.db.delete_dossier(message.chat.id, user_id)
            await message.reply("Досье удалено, режим opt-out включён. Я тебя забыл.")
            return True

        elif text.startswith("/remember me") and user_id:
            await self.db.remove_opt_out(message.chat.id, user_id)
            await message.reply("Режим opt-out снят. Досье снова ведётся.")
            return True

        elif text.startswith("/mute"):
            if user_id not in self.bot_cfg.OWNER_IDS:
                return False
            # e.g. /mute 10м or /mute 1h
            match = re.search(r"/mute\s+(\d+)\s*([мhhdд]?)", text)
            duration_s = 600  # Default 10 min
            if match:
                val = int(match.group(1))
                unit = match.group(2).lower()
                if unit in ("h", "ч"):
                    duration_s = val * 3600
                elif unit in ("d", "д"):
                    duration_s = val * 86400
                else:
                    duration_s = val * 60

            self.muted_until = time.time() + duration_s
            mins = duration_s // 60
            await message.reply(f"🔇 Проактив заглушен на {mins} мин.")
            return True

        elif text.startswith("/refresh"):
            if user_id not in self.bot_cfg.OWNER_IDS:
                return False
            await self.dossier_worker.process_window(message.chat.id, message.message_id)
            await message.reply("🔄 Цикл досье и саммари запущен вручную.")
            return True

        return False

    async def _process_ping(
        self,
        message: types.Message,
        msg_dict: dict[str, Any],
        media_kind: Optional[str],
        file_id: Optional[str],
        file_unique_id: Optional[str]
    ) -> None:
        chat_id = message.chat.id
        media_cache_item = None

        # If media is present, do Step 1 media_analyze
        if media_kind and file_id and file_unique_id:
            download_path = Path(f"/tmp/smesh-{file_unique_id}")
            try:
                tg_file = await self.bot.get_file(file_id)
                if tg_file.file_path:
                    await self.bot.download_file(tg_file.file_path, destination=download_path)
                    media_cache_item = await self.media_analyzer.analyze_media_step1(
                        file_path=download_path,
                        kind=media_kind,
                        file_unique_id=file_unique_id,
                        caption=message.caption,
                        priority="P0"
                    )
            except Exception as e:
                logger.error("Error downloading/analyzing media: %s", e)
            finally:
                download_path.unlink(missing_ok=True)

        # Assemble context
        mode = "media_reply" if media_cache_item else "ping"
        system_instruction, contents = await self.assembler.assemble(
            chat_id=chat_id,
            current_msg=msg_dict,
            mode=mode,
            media_json=json.dumps(media_cache_item, ensure_ascii=False) if media_cache_item else None
        )

        reply_raw = await self.gateway.generate(
            prompt_name=f"persona_{mode}",
            contents=contents,
            system_instruction=system_instruction,
            priority="P0",
            estimated_tokens=700
        )

        if not reply_raw:
            return

        if isinstance(reply_raw, dict) and reply_raw.get("is_degraded"):
            # Instant degraded excuse
            import random
            excuse = (
                random.choice(self.config.persona.degraded_replies)
                if self.config.persona.degraded_replies
                else "Башка не варит, позже."
            )
            await self.send_queue.enqueue(chat_id, excuse, message.message_id, priority="P0")
            return

        if isinstance(reply_raw, dict) and reply_raw.get("is_blocked"):
            # Filter block comeback
            reply_raw = "Не, эту хуйню я озвучивать не буду."

        # If media reply, verify and patch N/100 score
        if media_cache_item and isinstance(reply_raw, str):
            reply_raw = self.media_analyzer.verify_and_patch_reply_score(reply_raw, media_cache_item)

        await self.send_queue.enqueue(
            chat_id=chat_id,
            text=str(reply_raw),
            reply_to_message_id=message.message_id,
            priority="P0"
        )

    async def _process_proactive(self, message: types.Message, msg_dict: dict[str, Any]) -> None:
        chat_id = message.chat.id
        from src.gateway.schemas import ProactiveDecision

        force_val = self.proactive.should_force_proactive()
        system_instruction, contents = await self.assembler.assemble(
            chat_id=chat_id,
            current_msg=msg_dict,
            mode="proactive",
            request_attrs=f'force="{"true" if force_val else "false"}"'
        )

        decision: Optional[ProactiveDecision] = await self.gateway.generate(
            prompt_name="proactive",
            contents=contents,
            system_instruction=system_instruction,
            priority="P1",
            response_schema=ProactiveDecision,
            estimated_tokens=300
        )

        if not decision:
            return

        if decision.action.value == "reply" and decision.reply:
            reply_to = decision.target_msg_id or None
            await self.send_queue.enqueue(
                chat_id=chat_id,
                text=decision.reply,
                reply_to_message_id=reply_to,
                priority="P1"
            )
            self.proactive.on_proactive_reply_sent()
        else:
            self.proactive.on_proactive_skipped()
