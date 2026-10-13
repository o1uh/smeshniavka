import random
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.ops.config import BotConfig
from src.ops.log import logger


class ProactiveEngine:
    """Manages proactive message triggering according to R1 specifications."""

    def __init__(self, bot_config: BotConfig):
        self.config = bot_config
        self.min_thresh = bot_config.PROACTIVE_MIN
        self.max_thresh = bot_config.PROACTIVE_MAX
        self.random_p = bot_config.RANDOM_P
        self.cooldown_s = bot_config.COOLDOWN_S
        self.max_per_hour = bot_config.PROACTIVE_MAX_PER_HOUR
        self.max_per_day = bot_config.PROACTIVE_MAX_PER_DAY
        self.tz_name = bot_config.TZ

        self.current_threshold = random.randint(self.min_thresh, self.max_thresh)
        self.messages_count = 0
        self.last_bot_message_time: float = 0.0
        self.last_was_proactive = False

        self.hourly_timestamps: list[float] = []
        self.daily_date_str = ""
        self.daily_count = 0
        self.author_timestamps: list[tuple[float, int]] = []

    def _sync_daily_tz(self) -> None:
        tz = ZoneInfo(self.tz_name)
        now_date = datetime.now(tz).strftime("%Y-%m-%d")
        if self.daily_date_str != now_date:
            self.daily_date_str = now_date
            self.daily_count = 0

    def is_night(self) -> bool:
        tz = ZoneInfo(self.tz_name)
        now_time = datetime.now(tz).strftime("%H:%M")
        start = self.config.NIGHT_START
        end = self.config.NIGHT_END
        if start <= end:
            return start <= now_time <= end
        else:
            return now_time >= start or now_time <= end

    def record_incoming_message(self, user_id: Optional[int]) -> bool:
        """
        Records an incoming chat message. Returns True if proactive evaluation should trigger.
        """
        now = time.time()
        if user_id:
            self.author_timestamps.append((now, user_id))

        self.messages_count += 1
        if self.messages_count >= self.current_threshold:
            return True
        return False

    def check_proactive_caps(self) -> tuple[bool, Optional[str]]:
        """
        Checks all proactive conditions BEFORE making LLM call.
        Returns (can_trigger, reject_reason).
        """
        now = time.time()
        self._sync_daily_tz()

        # 1. Night check in configured TZ
        if self.is_night():
            return False, "Night time in TZ"

        # 2. Cooldown after last bot message
        if (now - self.last_bot_message_time) < self.cooldown_s:
            return False, "Cooldown after bot message active"

        # 3. Not 2 proactive messages in a row
        if self.last_was_proactive:
            return False, "Cannot send two proactive messages in a row"

        # 4. Hourly limit
        self.hourly_timestamps = [t for t in self.hourly_timestamps if (now - t) < 3600.0]
        if len(self.hourly_timestamps) >= self.max_per_hour:
            return False, "Hourly proactive cap reached"

        # 5. Daily limit in TZ
        if self.daily_count >= self.max_per_day:
            return False, "Daily proactive cap reached"

        # 6. Chat activity: >= 2 distinct authors in last PROACTIVE_ACTIVE_MIN
        active_cutoff = now - (self.config.PROACTIVE_ACTIVE_MIN * 60.0)
        self.author_timestamps = [item for item in self.author_timestamps if item[0] > active_cutoff]
        distinct_authors = {item[1] for item in self.author_timestamps}
        if len(distinct_authors) < 2:
            return False, "Chat not active enough (<2 distinct authors)"

        return True, None

    def should_force_proactive(self) -> bool:
        return random.random() < self.random_p

    def on_proactive_reply_sent(self) -> None:
        now = time.time()
        self.last_bot_message_time = now
        self.last_was_proactive = True
        self.hourly_timestamps.append(now)
        self.daily_count += 1
        self.messages_count = 0
        self.current_threshold = random.randint(self.min_thresh, self.max_thresh)
        logger.info("Proactive reply triggered. New threshold: %d", self.current_threshold)

    def on_proactive_skipped(self) -> None:
        # Backoff: add extra messages before checking again
        self.messages_count = max(0, self.messages_count - 5)
        logger.info("Proactive skipped by model. Backing off counter to %d", self.messages_count)

    def on_ping_reply_sent(self) -> None:
        self.last_bot_message_time = time.time()
        self.last_was_proactive = False
