import os
import re
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Masking secret tokens in logs: show only last 4 chars, e.g. ****abcd
TOKEN_MASK_RE = re.compile(r"(AIzaSy[A-Za-z0-9_-]{4,33})|([0-9]{8,11}:[A-Za-z0-9_-]{30,40})")

class SecretMaskingFormatter(logging.Formatter):
    """Masks API keys and bot tokens in log records."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        def mask_match(m: re.Match) -> str:
            val = m.group(0)
            if len(val) > 4:
                return "****" + val[-4:]
            return "****"
        return TOKEN_MASK_RE.sub(mask_match, original)


def setup_logging(
    log_dir: str = "logs",
    reports_dir: str = "reports",
    log_content: bool = False,
    level: int = logging.INFO
) -> tuple[logging.Logger, logging.Logger]:
    """
    Sets up application logging according to R10:
    - logs/bot.log: 5 x 5MB RotatingFileHandler
    - reports/shadow.log: shadow mode output (mode 600)
    - stdout handler
    """
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(reports_dir, exist_ok=True)

    bot_log_path = Path(log_dir) / "bot.log"
    shadow_log_path = Path(reports_dir) / "shadow.log"

    # Root / App logger
    logger = logging.getLogger("funny_bot")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    formatter = SecretMaskingFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating file handler (5 x 5MB)
    file_handler = RotatingFileHandler(
        bot_log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # Shadow logger
    shadow_logger = logging.getLogger("shadow_log")
    shadow_logger.setLevel(logging.INFO)
    shadow_logger.propagate = False
    shadow_logger.handlers.clear()

    shadow_handler = RotatingFileHandler(
        shadow_log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    shadow_formatter = SecretMaskingFormatter(
        fmt="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    shadow_handler.setFormatter(shadow_formatter)
    shadow_logger.addHandler(shadow_handler)

    # Set secure permissions (600 on files, 700 on dirs)
    try:
        if shadow_log_path.exists():
            os.chmod(shadow_log_path, 0o600)
        if bot_log_path.exists():
            os.chmod(bot_log_path, 0o600)
        os.chmod(reports_dir, 0o700)
    except Exception:
        pass

    return logger, shadow_logger

logger = logging.getLogger("funny_bot")
shadow_logger = logging.getLogger("shadow_log")
