import argparse
import asyncio
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from src.ops.config import load_config
from src.ops.log import setup_logging, logger
from src.db.manager import DatabaseManager
from src.tg.bot import FunnyBotApp


async def heartbeat_loop(heartbeat_path: Path, interval_s: float = 15.0) -> None:
    """Updates mtime of data/heartbeat for container/systemd healthcheck."""
    while True:
        try:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.touch()
        except Exception as e:
            logger.warning("Heartbeat update failed: %s", e)
        await asyncio.sleep(interval_s)


async def daily_backup_loop(db_mgr: DatabaseManager, interval_hours: float = 24.0) -> None:
    """Performs daily VACUUM INTO backup, keeping up to 7 backups."""
    while True:
        await asyncio.sleep(interval_hours * 3600.0)
        try:
            await db_mgr.backup(prefix="daily")
        except Exception as e:
            logger.error("Daily backup failed: %s", e)


def check_ffmpeg() -> bool:
    try:
        res = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return res.returncode == 0
    except Exception:
        return False


def run_dry_run() -> None:
    print("=== Запуск --dry-run проверки ===")

    # 1. Check ffmpeg
    if not check_ffmpeg():
        print("ОШИБКА: ffmpeg не найден или не работает! Установите ffmpeg.", file=sys.stderr)
        sys.exit(1)
    print("1. ffmpeg: OK")

    # 2. Check configuration
    try:
        config = load_config(dry_run=True)
        print(f"2. Конфигурация: OK (MODEL_ID: {config.bot.MODEL_ID})")
    except Exception as e:
        print(f"ОШИБКА конфигурации: {e}", file=sys.stderr)
        sys.exit(1)

    # 3. Check DB and migrations
    async def _test_db():
        db_mgr = DatabaseManager("data/test_dry_run.db")
        await db_mgr.connect()
        applied = await db_mgr.apply_migrations()
        await db_mgr.close()
        Path("data/test_dry_run.db").unlink(missing_ok=True)
        return applied

    try:
        asyncio.run(_test_db())
        print("3. База данных и миграции: OK")
    except Exception as e:
        print(f"ОШИБКА базы данных: {e}", file=sys.stderr)
        sys.exit(1)

    print("=== Dry-run успешно завершён ===")
    sys.exit(0)


async def async_main():
    # 1. Setup logging
    setup_logging()
    logger.info("Starting FunnyBot...")

    # 2. Check ffmpeg
    if not check_ffmpeg():
        logger.error("ffmpeg is missing or not executable!")
        sys.exit(1)

    # 3. Load config
    try:
        config = load_config(dry_run=False)
    except Exception as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    # 4. Acquire file lock on data/bot.lock (code 75 on collision)
    lock_path = Path("data/bot.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        logger.error("Another bot instance is running (lock contention on %s)! Exiting with code 75.", lock_path)
        sys.exit(75)

    # 5. Connect DB and run migrations
    db_mgr = DatabaseManager()
    await db_mgr.connect()
    applied_migrations = await db_mgr.apply_migrations()
    if applied_migrations:
        logger.info("Applied migrations: %s", applied_migrations)

    # 6. Initialize bot app
    app = FunnyBotApp(config=config, db_manager=db_mgr)
    await app.initialize()

    # 7. Check clean shutdown marker
    was_clean = app.alert_mgr.check_clean_shutdown_marker()
    if not was_clean:
        logger.warning("Unclean shutdown detected! Sending alert...")
        await app.alert_mgr.send_alert(
            "UNCLEAN_SHUTDOWN",
            "Бот стартовал после некорректного завершения (отсутствовал маркер clean_shutdown)."
        )

    # 8. Start background tasks
    heartbeat_task = asyncio.create_task(heartbeat_loop(Path("data/heartbeat")))
    backup_task = asyncio.create_task(daily_backup_loop(db_mgr))

    # Graceful shutdown handler
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Received termination signal, initiating graceful shutdown...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    # Start Polling
    polling_task = asyncio.create_task(
        app.dp.start_polling(app.bot, allowed_updates=[UpdateType.MESSAGE, UpdateType.EDITED_MESSAGE])
    )

    # Wait until stop_event or polling error
    done, pending = await asyncio.wait(
        [polling_task, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED
    )

    # Cleanup & Graceful shutdown
    logger.info("Stopping services...")
    heartbeat_task.cancel()
    backup_task.cancel()
    await app.send_queue.stop()
    await app.bot.session.close()
    await db_mgr.close()

    # Write clean shutdown marker
    app.alert_mgr.write_clean_shutdown_marker()

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    except Exception:
        pass

    logger.info("Clean shutdown completed.")


def main():
    parser = argparse.ArgumentParser(description="Funny Bot Main Entrypoint")
    parser.add_argument("--dry-run", action="store_true", help="Perform pre-flight dry-run validation")
    args = parser.parse_args()

    if args.dry_run:
        run_dry_run()

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
