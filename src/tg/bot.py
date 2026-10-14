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
from src.context.assemble import ContextAssembler
from src.context.prompts import PromptBuilder, sanitize_nick
from src.persona.reply import PingDetector, AuthorSpamLimiter, TgSendQueue
from src.persona.proactive import ProactiveEngine
from src.persona.sanitizer import (
    replace_un_aliases,
    has_persona_leak,
    clean_media_reply_boilerplate,
    validate_persona_reply,
)
from src.gateway.schemas import PersonaTextReply
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
from dataclasses import dataclass
from src.tg.filters import RuntimeChatResolver, check_media_caps
from src.ops.alerts import AlertManager


def to_iso_date(dt_or_ts: Any) -> str:
    if isinstance(dt_or_ts, (int, float)):
        return datetime.fromtimestamp(dt_or_ts, tz=timezone.utc).isoformat()
    if isinstance(dt_or_ts, datetime):
        return dt_or_ts.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExtractedMedia:
    media_kind: Optional[str] = None
    file_id: Optional[str] = None
    file_unique_id: Optional[str] = None
    file_size: int = 0
    duration: Optional[int] = None
    caption: Optional[str] = None
    text_stub: Optional[str] = None


def extract_media_info(message: types.Message) -> ExtractedMedia:
    media_kind = None
    file_unique_id = None
    file_id = None
    file_size = 0
    duration = None
    text_stub = None

    if message.sticker:
        media_kind = "sticker"
        text_stub = format_sticker_stub(message.sticker.emoji, message.sticker.set_name)
        if (getattr(message.sticker, "is_animated", False) or getattr(message.sticker, "is_video", False)) and getattr(message.sticker, "thumbnail", None):
            file_unique_id = message.sticker.thumbnail.file_unique_id
            file_id = message.sticker.thumbnail.file_id
            file_size = getattr(message.sticker.thumbnail, "file_size", 0) or 0
        else:
            file_unique_id = message.sticker.file_unique_id
            file_id = message.sticker.file_id
            file_size = getattr(message.sticker, "file_size", 0) or 0
    elif message.animation:
        media_kind = "animation"
        file_unique_id = message.animation.file_unique_id
        file_id = message.animation.file_id
        file_size = message.animation.file_size or 0
        duration = message.animation.duration
        text_stub = format_animation_stub(message.caption)
    elif message.voice:
        media_kind = "voice"
        file_unique_id = message.voice.file_unique_id
        file_id = message.voice.file_id
        file_size = message.voice.file_size or 0
        duration = message.voice.duration
        text_stub = "[войс]"
    elif message.video_note:
        media_kind = "video_note"
        file_unique_id = message.video_note.file_unique_id
        file_id = message.video_note.file_id
        file_size = message.video_note.file_size or 0
        duration = message.video_note.duration
        text_stub = "[кружочек]"
    elif message.video:
        media_kind = "video"
        file_unique_id = message.video.file_unique_id
        file_id = message.video.file_id
        file_size = message.video.file_size or 0
        duration = message.video.duration
        text_stub = "[видео]"
    elif message.photo:
        media_kind = "photo"
        photo_obj = message.photo[-1]
        file_unique_id = photo_obj.file_unique_id
        file_id = photo_obj.file_id
        file_size = photo_obj.file_size or 0
        text_stub = "[фото]"
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
        text_stub = f"[аудио{audio_info}]"
    elif message.poll:
        media_kind = "poll"
        opts = [o.text for o in message.poll.options]
        text_stub = format_poll_stub(message.poll.question, opts)
    elif message.dice:
        media_kind = "dice"
        text_stub = format_dice_stub(message.dice.emoji, message.dice.value)
    elif message.contact:
        media_kind = "contact"
        text_stub = "[контакт]"
    elif message.location or message.venue:
        media_kind = "location"
        text_stub = "[геопозиция]"

    return ExtractedMedia(
        media_kind=media_kind,
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_size=file_size,
        duration=duration,
        caption=message.caption,
        text_stub=text_stub,
    )


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
        self.dp.edited_message.register(self.handle_edited_message)
        self.dp.my_chat_member.register(self.handle_my_chat_member)

    async def initialize(self) -> None:
        me = await self.bot.get_me()
        self.bot_id = me.id
        self.bot_username = me.username or "funny_bot"
        self.ping_detector = PingDetector(self.bot_username, self.bot_id, self.bot_cfg)
        self.send_queue.start()
        logger.info("Bot initialized: @%s (id=%d)", self.bot_username, self.bot_id)

    async def handle_edited_message(self, message: types.Message) -> None:
        """TB-03: edit обновляет текст в БД, без ответов и без P1."""
        chat = message.chat
        if not self.chat_resolver.is_allowed(chat.id):
            return
        text = message.text or message.caption or ""
        try:
            await self.db.update_message_text(
                chat.id,
                message.message_id,
                text,
                to_iso_date(message.edit_date if message.edit_date is not None else message.date),
            )
        except Exception as e:
            logger.warning("Failed to update edited message %s/%s: %s", chat.id, message.message_id, e)

    async def handle_my_chat_member(self, event: types.ChatMemberUpdated) -> None:
        """I3/C6: чужой чат со статусом member/administrator — немедленный leaveChat."""
        chat = event.chat
        try:
            new_status = event.new_chat_member.status
        except Exception:
            return
        if self.chat_resolver.is_allowed(chat.id):
            return
        if str(new_status) not in ("member", "administrator", "creator"):
            return
        inviting_id = None
        try:
            inviting_id = event.from_user.id if event.from_user else None
        except Exception:
            pass
        logger.warning(
            "Bot added to foreign chat %s (%s) by %s. Leaving...",
            chat.id, getattr(chat, "title", ""), inviting_id,
        )
        await self.db.add_foreign_chat(chat.id, getattr(chat, "title", ""))
        try:
            await self.bot.leave_chat(chat.id)
        except Exception:
            pass
        await self.alert_mgr.send_alert(
            "FOREIGN_CHAT_LEFT",
            f"Бот добавлен в посторонний чат {chat.id} и покинул его.",
        )

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
        media_ext = extract_media_info(message)
        media_kind = media_ext.media_kind
        file_unique_id = media_ext.file_unique_id
        file_id = media_ext.file_id
        file_size = media_ext.file_size
        duration = media_ext.duration

        if media_ext.text_stub:
            text = text or media_ext.text_stub

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
            "created_at": to_iso_date(message.date),
        }

        # Save to database
        await self.db.save_message(msg_dict)
        logger.info(
            "📩 [msg #%d] %s (%s): %s",
            message.message_id, nick, alias, text[:80] if text else f"[{media_kind or 'text'}]"
        )

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
            bridge_status = "✅ OK" if key_summary.get("bridge_status") == "ok" else "❌ DOWN"
            bridge_url = key_summary.get("bridge_url", self.bot_cfg.BRIDGE_URL)
            status_text = (
                f"🤖 <b>Смешнявка</b>{shadow_tag}\n"
                f"Аптайм: {uptime_min:.1f} мин\n"
                f"Модель: <code>{self.bot_cfg.MODEL_ID}</code>\n"
                f"Мост Antigravity: {bridge_status} (<code>{bridge_url}</code>)\n"
                f"Запросов к мосту: {key_summary.get('total_rpd_used', 0)}"
            )
            await message.reply(status_text, parse_mode="HTML")
            return True

        elif text.startswith("/reset"):
            ok = await self.gateway.reset_session(f"tg_{message.chat.id}")
            if ok:
                await message.reply("Сессия контекста диалога на мосту Antigravity сброшена.")
            else:
                await message.reply("Сессия не найдена или уже была очищена.")
            return True

        elif text.startswith("/me"):
            alias = msg_dict.get("alias")
            await message.reply(f"Твой псевдоним: <code>{alias}</code>\nID: <code>{user_id}</code>")
            return True

        elif text.startswith("/who"):
            await self._process_who(message, msg_dict, text)
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

    async def _process_who(
        self, message: types.Message, msg_dict: dict[str, Any], text: str
    ) -> None:
        """R12/TB-05: /who — роаст/сводка по досье (mode_who, P0)."""
        import random

        chat_id = message.chat.id
        target_arg = text[len("/who"):].strip().lstrip("@").lower()
        target_uid: Optional[int] = None
        target_alias = "all"

        # Цель: реплай > алиас uN > ник из last60 > все
        if message.reply_to_message and message.reply_to_message.from_user:
            target_uid = message.reply_to_message.from_user.id
            target_alias = f"u{target_uid}"
        elif target_arg:
            last_msgs = await self.db.get_last_messages(chat_id, limit=60)
            for m in last_msgs:
                if (m.get("alias") or "").lower() == target_arg or (
                    m.get("nick") or ""
                ).lower().startswith(target_arg):
                    target_uid = m.get("user_id")
                    target_alias = m.get("alias") or f"u{target_uid}"
                    break

        # opt_out цели — без API, статика
        if target_uid and await self.db.is_opt_out(chat_id, target_uid):
            excuse = (
                random.choice(self.config.persona.opt_out_replies)
                if self.config.persona.opt_out_replies
                else "Про него ничего нет."
            )
            await self.send_queue.enqueue(chat_id, excuse, message.message_id, priority="P0")
            return

        who_msg = dict(msg_dict)
        if target_uid:
            who_msg["reply_to_user_id"] = target_uid
        system_instruction, contents = await self.assembler.assemble(
            chat_id=chat_id,
            current_msg=who_msg,
            mode="who",
            request_attrs=f'target="{target_alias}"',
        )
        res = await self.gateway.generate(
            prompt_name="persona_who",
            contents=contents,
            system_instruction=system_instruction,
            priority="P0",
            response_schema=PersonaTextReply,
            estimated_tokens=400,
            session_id=f"tg_{chat_id}",
        )
        candidate = res.reply.strip() if isinstance(res, PersonaTextReply) else (
            res.strip() if isinstance(res, str) and res.strip() else ""
        )
        is_valid, _ = validate_persona_reply(candidate, is_media=False)
        if not candidate or not is_valid:
            # Одна repair-попытка тем же приоритетом (I11), затем молчи
            res2 = await self.gateway.generate(
                prompt_name="persona_who",
                contents=contents,
                system_instruction=system_instruction,
                priority="P0",
                response_schema=PersonaTextReply,
                estimated_tokens=400,
                session_id=f"tg_{chat_id}",
            )
            candidate2 = res2.reply.strip() if isinstance(res2, PersonaTextReply) else (
                res2.strip() if isinstance(res2, str) and res2.strip() else ""
            )
            ok2, _ = validate_persona_reply(candidate2, is_media=False)
            if not candidate2 or not ok2:
                logger.warning("Outgoing guard: /who dropped after repair, not sending raw.")
                return
            candidate = candidate2
        final_text = await self._resolve_aliases_to_text(chat_id, candidate)
        await self.send_queue.enqueue(
            chat_id=chat_id, text=final_text,
            reply_to_message_id=message.message_id, priority="P0",
        )

    async def _auto_process_background_media(
        self,
        chat_id: int,
        media_kind: str,
        file_id: str,
        file_unique_id: str,
        caption: Optional[str] = None
    ) -> None:
        """Background P2 auto-analysis of media files per R7 (MEDIA_AUTO_ANALYZE_P)."""
        import random
        # Check cache first
        cached = await self.db.get_media_cache(file_unique_id)
        if cached:
            return

        prob_map = getattr(self.bot_cfg, "MEDIA_AUTO_ANALYZE_P", {}) or {}
        p = prob_map.get(media_kind, 0.0)
        if random.random() > p:
            return

        download_path = Path(f"/tmp/smesh-{file_unique_id}")
        try:
            tg_file = await self.bot.get_file(file_id)
            if tg_file.file_path:
                await self.bot.download_file(tg_file.file_path, destination=download_path)
                await self.media_analyzer.analyze_media_step1(
                    file_path=download_path,
                    kind=media_kind,
                    file_unique_id=file_unique_id,
                    caption=caption,
                    priority="P2"
                )
                logger.info("Background P2 analyzed and cached media %s (%s)", file_unique_id, media_kind)
        except Exception as e:
            logger.warning("Background media analysis failed for %s: %s", file_unique_id, e)
        finally:
            download_path.unlink(missing_ok=True)

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
        asyncio.create_task(self.sender.send_chat_action(chat_id, "typing"))

        # 1. Resolve media: check current message, and if absent, check reply_to_message!
        target_kind = media_kind
        target_file_id = file_id
        target_file_unique_id = file_unique_id
        target_caption = message.caption

        if not target_file_unique_id and message.reply_to_message:
            reply_media = extract_media_info(message.reply_to_message)
            if reply_media.file_unique_id:
                target_kind = reply_media.media_kind
                target_file_id = reply_media.file_id
                target_file_unique_id = reply_media.file_unique_id
                target_caption = reply_media.caption

        # 2. Analyze media if present
        if target_file_unique_id:
            media_cache_item = await self.db.get_media_cache(target_file_unique_id)
            if not media_cache_item and target_file_id and target_kind:
                download_path = Path(f"/tmp/smesh-{target_file_unique_id}")
                try:
                    tg_file = await self.bot.get_file(target_file_id)
                    if tg_file.file_path:
                        await self.bot.download_file(tg_file.file_path, destination=download_path)
                        media_cache_item = await self.media_analyzer.analyze_media_step1(
                            file_path=download_path,
                            kind=target_kind,
                            file_unique_id=target_file_unique_id,
                            caption=target_caption,
                            priority="P0"
                        )
                except Exception as e:
                    logger.error("Error downloading/analyzing media for ping: %s", e)
                finally:
                    download_path.unlink(missing_ok=True)

        # 3. Check Research (force triggers or soft triggers)
        text = (msg_dict.get("text") or "").strip()
        need_search = False
        safe_query = None
        sources_text = None
        show_links = self.research.should_show_links(text)

        if self.research.should_force_search(text) or self.research.should_soft_search(text):
            last_msgs = await self.db.get_last_messages(chat_id, limit=10)
            history_lines = [f"{m.get('nick', 'User')}: {m.get('text', '')}" for m in last_msgs if m.get("text")]
            history_str = "\n".join(history_lines)
            known_nicks = [m.get("nick") for m in last_msgs if m.get("nick")]

            need_search, safe_query = await self.research.decide_search(
                history_text=history_str,
                current_text=text,
                known_nicks=known_nicks
            )

        if need_search and safe_query:
            sources_text = f"[n=1 | Поиск в сети | query: {safe_query}]\nАктуальные данные получены через веб-поиск"

        # 4. Mode determination
        if media_cache_item:
            mode = "media_reply"
        elif need_search:
            mode = "research"
        else:
            mode = "ping"

        # 5. Assemble context
        step2_media_json = None
        if media_cache_item:
            summary_val = media_cache_item.get("summary") or ""
            step2_data = {
                "kind": media_cache_item.get("kind"),
                "summary": summary_val[:300],  # Include summary per TB-15
                "verdict": media_cache_item.get("verdict"),
                "score100": media_cache_item.get("score100"),
                "score_reason": media_cache_item.get("score_reason"),
                "track_meta": media_cache_item.get("track_meta"),
                "tags": media_cache_item.get("tags"),
                "unintelligible": media_cache_item.get("unintelligible"),
                "injection_suspected": media_cache_item.get("injection_suspected"),
            }
            step2_media_json = json.dumps(step2_data, ensure_ascii=False)

        request_attrs = f'mode="{mode}"'
        if mode == "research":
            request_attrs += f' show_links="{"true" if show_links else "false"}"'

        system_instruction, contents = await self.assembler.assemble(
            chat_id=chat_id,
            current_msg=msg_dict,
            mode=mode,
            media_json=step2_media_json,
            sources_text=sources_text,
            request_attrs=request_attrs,
        )

        reply_raw = None
        is_media_mode = (mode == "media_reply")

        # Outgoing guard: if leak or bad parse is detected, drop + 1 repair-retry, do not send raw
        for attempt in range(2):
            res = await self.gateway.generate(
                prompt_name=f"persona_{mode}",
                contents=contents,
                system_instruction=system_instruction,
                priority="P0",
                response_schema=PersonaTextReply,
                estimated_tokens=700,
                enable_search=bool(need_search),
                session_id=f"tg_{chat_id}"
            )

            if isinstance(res, dict) and (res.get("is_degraded") or res.get("is_blocked")):
                reply_raw = res
                break

            if isinstance(res, PersonaTextReply):
                candidate_text = res.reply.strip()
            elif isinstance(res, str) and res.strip():
                candidate_text = res.strip()
            else:
                logger.warning("Outgoing guard: failed to parse JSON {reply} on attempt %d/2", attempt + 1)
                if attempt == 0:
                    continue  # 1 repair retry
                else:
                    reply_raw = None
                    break

            # Validate reply per Point 2
            is_valid, reject_reason = validate_persona_reply(candidate_text, is_media=is_media_mode)
            if not is_valid:
                logger.warning(
                    "Outgoing guard: invalid reply on attempt %d/2: %s (text: %.80s)",
                    attempt + 1, reject_reason, candidate_text
                )
                if attempt == 0:
                    continue  # 1 repair retry
                else:
                    reply_raw = None
                    break
            else:
                reply_raw = candidate_text
                break

        if not reply_raw:
            # Fallback to static degraded excuse instead of sending raw/broken text
            import random
            excuse = (
                random.choice(self.config.persona.degraded_replies)
                if self.config.persona.degraded_replies
                else "Башка не варит, позже."
            )
            await self.send_queue.enqueue(chat_id, excuse, message.message_id, priority="P0")
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

        # If media reply, strip any boilerplate and verify/patch N/100 score
        if is_media_mode and isinstance(reply_raw, str):
            reply_raw = clean_media_reply_boilerplate(reply_raw)
            if media_cache_item:
                reply_raw = self.media_analyzer.verify_and_patch_reply_score(reply_raw, media_cache_item)

        final_text = await self._resolve_aliases_to_text(chat_id, str(reply_raw))
        await self.send_queue.enqueue(
            chat_id=chat_id,
            text=final_text,
            reply_to_message_id=message.message_id,
            priority="P0"
        )

    async def _resolve_aliases_to_text(self, chat_id: int, text: str) -> str:
        last_msgs = await self.db.get_last_messages(chat_id, limit=60)
        user_mapping = {
            m.get("alias") or f"u{m.get('user_id')}": {
                "user_id": m.get("user_id"),
                "nick": m.get("nick"),
                "username": None
            }
            for m in last_msgs
        }
        return replace_un_aliases(text, user_mapping)

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
            # Check outgoing leak guard on proactive reply as well
            if has_persona_leak(decision.reply, is_media=False):
                logger.warning("Outgoing guard: proactive reply contained leak, dropping.")
                self.proactive.on_proactive_skipped()
                return

            reply_to = decision.target_msg_id or None
            final_reply = await self._resolve_aliases_to_text(chat_id, decision.reply)
            await self.send_queue.enqueue(
                chat_id=chat_id,
                text=final_reply,
                reply_to_message_id=reply_to,
                priority="P1"
            )
            self.proactive.on_proactive_reply_sent()
        else:
            self.proactive.on_proactive_skipped()
