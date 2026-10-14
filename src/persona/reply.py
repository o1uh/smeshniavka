import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

from src.ops.config import BotConfig, PersonaConfig
from src.ops.log import logger, shadow_logger
from src.persona.sanitizer import sanitize_telegram_html, strip_all_tags, split_message_chunks
from src.db.contracts import TgSender


class PingDetector:
    """Detects whether an incoming message is a ping to the bot per R1."""

    def __init__(self, bot_username: str, bot_id: int, bot_config: BotConfig):
        self.bot_username = bot_username.lower().lstrip("@")
        self.bot_id = bot_id
        self.aliases = [a.lower() for a in bot_config.BOT_ALIASES]
        self.followup_window_s = bot_config.FOLLOWUP_WINDOW_S
        self.last_bot_reply_to_user: Optional[int] = None
        self.last_bot_reply_time: float = 0.0
        self.messages_since_last_reply: int = 0
        self._followup_done: bool = False

    def record_bot_reply(self, target_user_id: int) -> None:
        self.last_bot_reply_to_user = target_user_id
        self.last_bot_reply_time = time.time()
        self.messages_since_last_reply = 0
        self._followup_done = False

    def record_other_message(self) -> None:
        self.messages_since_last_reply += 1

    def is_ping(self, msg: dict[str, Any]) -> bool:
        user_id = msg.get("user_id")
        text = (msg.get("text") or "").lower()

        # 1. Reply to bot's message
        if msg.get("reply_to_user_id") == self.bot_id:
            return True

        # 2. @mention of bot
        if f"@{self.bot_username}" in text:
            return True

        # 3. Alias match (word starting with alias)
        words = text.split()
        for w in words:
            clean_w = "".join(c for c in w if c.isalnum() or c in ("-", "_"))
            for alias in self.aliases:
                if clean_w.startswith(alias):
                    return True

        # 4. Follow-up: только reply на сообщение бота в окне (R1):
        # тот же автор, <=1 чужого между, цепочка не продлевается
        now = time.time()
        if (
            self.followup_window_s > 0
            and user_id == self.last_bot_reply_to_user
            and msg.get("reply_to_user_id") == self.bot_id
            and (now - self.last_bot_reply_time) <= self.followup_window_s
            and self.messages_since_last_reply <= 1
            and not self._followup_done
        ):
            self._followup_done = True
            return True

        return False


class AuthorSpamLimiter:
    """Author-level rate limiting (20 / 60s) with FIFO queue per R1."""

    def __init__(self, bot_config: BotConfig, persona_config: PersonaConfig):
        self.ping_limit = bot_config.PING_LIMIT
        self.window_s = bot_config.PING_WINDOW_S
        self.queue_max = bot_config.PING_QUEUE_MAX
        self.queue_ttl = bot_config.PING_QUEUE_TTL_S
        self.rate_notice_cooldown = bot_config.RATE_NOTICE_COOLDOWN_S
        self.persona_config = persona_config

        self.user_timestamps: dict[int, list[float]] = {}
        self.last_notice_sent: dict[int, float] = {}

    def check_author(self, user_id: int) -> tuple[bool, Optional[str]]:
        """
        Returns (allowed, static_excuse).
        If user exceeds rate limit and cooldown expired, returns static excuse.
        """
        now = time.time()
        timestamps = self.user_timestamps.get(user_id, [])
        cutoff = now - self.window_s
        timestamps = [t for t in timestamps if t > cutoff]
        self.user_timestamps[user_id] = timestamps

        if len(timestamps) >= self.ping_limit:
            # Check notice cooldown
            last_notice = self.last_notice_sent.get(user_id, 0.0)
            if (now - last_notice) >= self.rate_notice_cooldown:
                self.last_notice_sent[user_id] = now
                excuse = (
                    random.choice(self.persona_config.rate_limit_replies)
                    if self.persona_config.rate_limit_replies
                    else "Тише, подожди минуту."
                )
                return False, excuse
            return False, None

        timestamps.append(now)
        return True, None


class TgSendQueue:
    """
    Manages outgoing messages with rate limit 18 msg/min, priority ordering (P0 > P1 > P2),
    entity retry fallback, and shadow mode execution.
    """

    def __init__(self, sender: TgSender, bot_config: BotConfig):
        self.sender = sender
        self.bot_config = bot_config
        self.send_per_min = bot_config.TG_SEND_PER_MIN
        self.queue_max = bot_config.TG_SEND_QUEUE_MAX
        self.shadow_mode = bot_config.SHADOW_MODE

        self._queue: list[dict[str, Any]] = []
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False
        # Колбэк (chat_id, message_id, full_text, reply_to) после успешной отправки:
        # бот пишет свои исходящие в БД (контекст + прайм по смеякам), best-effort
        self.on_sent = None

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self._process_queue())

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def enqueue(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        priority: str = "P0"
    ) -> None:
        async with self._lock:
            if len(self._queue) >= self.queue_max:
                # Overflow: drop oldest P1/P2 message
                dropped = False
                for i, item in enumerate(self._queue):
                    if item["priority"] in ("P1", "P2"):
                        self._queue.pop(i)
                        dropped = True
                        break
                if not dropped and priority != "P0":
                    # Cannot drop P0, reject new non-P0
                    return

            self._queue.append({
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to_message_id,
                "priority": priority,
                "created_at": time.time()
            })

    async def _process_queue(self) -> None:
        while self._running:
            item = None
            async with self._lock:
                if self._queue:
                    # Pick highest priority item: P0 first, then P1, then P2
                    self._queue.sort(key=lambda x: (0 if x["priority"] == "P0" else 1, x["created_at"]))
                    item = self._queue.pop(0)

            if not item:
                await asyncio.sleep(0.1)
                continue

            # Rate limit check (TG_SEND_PER_MIN)
            now = time.time()
            self._timestamps = [t for t in self._timestamps if (now - t) < 60.0]
            if len(self._timestamps) >= self.send_per_min:
                sleep_delay = 60.0 - (now - self._timestamps[0]) + 0.1
                await asyncio.sleep(max(0.1, sleep_delay))

            self._timestamps.append(time.time())

            # Shadow mode check
            if self.shadow_mode:
                shadow_logger.info(
                    "[SHADOW] chat=%s reply_to=%s prio=%s | ответил бы: %s",
                    item["chat_id"], item["reply_to"], item["priority"], item["text"]
                )
                continue

            # Real send with HTML sanitizing and 4096 chunking
            chunks = split_message_chunks(item["text"])
            for idx, chunk in enumerate(chunks):
                reply_id = item["reply_to"] if idx == 0 else None
                sanitized_html = sanitize_telegram_html(chunk)

                sent_id = None
                try:
                    sent_id = await self.sender.send_message(
                        chat_id=item["chat_id"],
                        text=sanitized_html,
                        reply_to_message_id=reply_id,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    err_msg = str(e).lower()
                    if "can't parse entities" in err_msg or "bad request" in err_msg:
                        logger.warning("Telegram HTML parse error, retrying plain text: %s", e)
                        plain = strip_all_tags(chunk)
                        try:
                            sent_id = await self.sender.send_message(
                                chat_id=item["chat_id"],
                                text=plain,
                                reply_to_message_id=reply_id,
                                parse_mode=None
                            )
                        except Exception as retry_err:
                            logger.error("Failed sending plain text fallback: %s", retry_err)
                    else:
                        logger.error("Error sending TG message: %s", e)

                if idx == 0 and sent_id is not None and self.on_sent is not None:
                    try:
                        await self.on_sent(item["chat_id"], sent_id, item["text"], item["reply_to"])
                    except Exception as cb_err:
                        logger.warning("Send queue on_sent callback failed: %s", cb_err)
