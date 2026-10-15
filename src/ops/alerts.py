import os
import shutil
import time
from pathlib import Path
from typing import Optional

from src.ops.log import logger
from src.db.contracts import TgSender


class AlertManager:
    """Manages alert delivery to OWNER_IDS in private messages with 3600s deduplication per TB-13."""

    def __init__(
        self,
        sender: TgSender,
        owner_ids: list[int],
        cooldown_s: int = 3600,
        data_dir: str = "data"
    ):
        self.sender = sender
        self.owner_ids = owner_ids
        self.cooldown_s = cooldown_s
        self.data_dir = Path(data_dir)
        self.clean_shutdown_file = self.data_dir / "clean_shutdown"

        self._last_alert_times: dict[str, float] = {}

    def should_alert(self, event_type: str) -> bool:
        now = time.time()
        last_time = self._last_alert_times.get(event_type, 0.0)
        if (now - last_time) >= self.cooldown_s:
            self._last_alert_times[event_type] = now
            return True
        return False

    async def send_alert(self, event_type: str, message: str, force: bool = False) -> None:
        """Sends alert message to all OWNER_IDS if cooldown has passed."""
        if not force and not self.should_alert(event_type):
            return

        formatted = f"⚠️ [Дик Алерт] <b>{event_type}</b>\n\n{message}"
        for owner_id in self.owner_ids:
            try:
                await self.sender.send_message(
                    chat_id=owner_id,
                    text=formatted,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning("Failed to send alert to OWNER %s: %s (403 expected if user hasn't messaged bot)", owner_id, e)

    def check_clean_shutdown_marker(self) -> bool:
        """
        Checks if clean_shutdown marker exists.
        Returns False if marker is missing (meaning previous shutdown was unclean / crashed).
        Removes marker upon check.
        """
        was_clean = self.clean_shutdown_file.exists()
        if was_clean:
            try:
                self.clean_shutdown_file.unlink(missing_ok=True)
            except Exception:
                pass
        return was_clean

    def write_clean_shutdown_marker(self) -> None:
        """Writes clean_shutdown marker upon graceful shutdown."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.clean_shutdown_file.write_text("ok", encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to write clean_shutdown marker: %s", e)

    async def check_disk_space(self, min_mb: int = 500) -> None:
        """Alerts if free disk space falls below min_mb."""
        try:
            total, used, free = shutil.disk_usage(self.data_dir)
            free_mb = free / (1024 * 1024)
            if free_mb < min_mb:
                await self.send_alert(
                    "LOW_DISK_SPACE",
                    f"Свободное место на диске: {free_mb:.1f} МБ (порог: {min_mb} МБ)!"
                )
        except Exception as e:
            logger.warning("Error checking disk space: %s", e)
