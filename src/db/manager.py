import os
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import aiosqlite

from src.ops.log import logger


class DatabaseManager:
    """
    Manages SQLite connection, WAL mode, migrations with pre-migration VACUUM INTO backups,
    retention cleanup, and data operations across all tables.
    """

    def __init__(self, db_path: str = "data/bot.db", backups_dir: str = "backups"):
        self.db_path = Path(db_path)
        self.backups_dir = Path(backups_dir)
        self.db: Optional[aiosqlite.Connection] = None
        self._import_active = False

    @property
    def import_active(self) -> bool:
        return self._import_active

    @import_active.setter
    def import_active(self, val: bool) -> None:
        self._import_active = val

    async def connect(self) -> aiosqlite.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
            os.chmod(self.backups_dir, 0o700)
        except Exception:
            pass

        self.db = await aiosqlite.connect(str(self.db_path))
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode = WAL;")
        await self.db.execute("PRAGMA busy_timeout = 5000;")
        await self.db.execute("PRAGMA foreign_keys = ON;")
        return self.db

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def get_user_version(self) -> int:
        assert self.db is not None
        async with self.db.execute("PRAGMA user_version;") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def set_user_version(self, version: int) -> None:
        assert self.db is not None
        await self.db.execute(f"PRAGMA user_version = {version};")

    async def backup(self, prefix: str = "manual") -> Optional[str]:
        """Performs atomic VACUUM INTO backup and keeps at most 7 recent backups."""
        assert self.db is not None
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = self.backups_dir / f"bot_{prefix}_{timestamp}.db"

        # Check if DB has tables / data before vacuuming
        try:
            await self.db.execute(f"VACUUM INTO '{str(backup_file)}';")
            try:
                os.chmod(backup_file, 0o600)
            except Exception:
                pass
            logger.info("Created SQLite backup: %s", backup_file)
            self._prune_backups(keep=7)
            return str(backup_file)
        except Exception as e:
            logger.warning("Backup failed: %s", e)
            return None

    def _prune_backups(self, keep: int = 7) -> None:
        """Keeps only the most recent 'keep' backups."""
        try:
            files = sorted(
                self.backups_dir.glob("bot_*.db"),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )
            for f in files[keep:]:
                f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Error pruning backups: %s", e)

    async def apply_migrations(self, migrations_dir: str = "migrations") -> list[int]:
        """
        Discovers NNN_*.sql files, checks user_version, runs VACUUM INTO before migration,
        and executes missing migrations in ascending order.
        Returns list of applied migration versions.
        """
        assert self.db is not None
        current_version = await self.get_user_version()
        mig_dir = Path(migrations_dir)
        if not mig_dir.exists():
            return []

        migration_files: list[tuple[int, Path]] = []
        for file in mig_dir.glob("*.sql"):
            match = re.match(r"^(\d+)_.*\.sql$", file.name)
            if match:
                v = int(match.group(1))
                migration_files.append((v, file))

        migration_files.sort(key=lambda x: x[0])
        applied: list[int] = []

        # Find migrations > current_version
        pending = [m for m in migration_files if m[0] > current_version]
        if not pending:
            return []

        # If DB file already exists and has data, backup before migration
        if self.db_path.exists() and self.db_path.stat().st_size > 0:
            await self.backup(prefix=f"pre_mig_v{current_version}")

        for version, path in pending:
            logger.info("Applying migration %03d: %s", version, path.name)
            sql = path.read_text(encoding="utf-8")
            async with self.db.cursor() as cursor:
                await cursor.executescript(sql)
            await self.set_user_version(version)
            await self.db.commit()
            applied.append(version)

        return applied

    # --- Message CRUD ---

    async def save_message(self, msg_dict: dict[str, Any]) -> None:
        assert self.db is not None
        defaults = {
            "user_id": None,
            "alias": None,
            "nick": None,
            "nick_history": None,
            "sender_kind": "user",
            "fwd_kind": None,
            "fwd_origin_name": None,
            "reply_to": None,
            "reply_to_user_id": None,
            "quote_text": None,
            "via_bot": None,
            "text": None,
            "urls": None,
            "media_kind": None,
            "file_unique_id": None,
            "is_bot": 0,
            "from_other_bot": 0,
            "edited_at": None,
        }
        full_dict = {**defaults, **msg_dict}
        sql = """
        INSERT OR REPLACE INTO messages (
            chat_id, message_id, user_id, alias, nick, nick_history, sender_kind,
            fwd_kind, fwd_origin_name, reply_to, reply_to_user_id, quote_text,
            via_bot, text, urls, media_kind, file_unique_id, is_bot, from_other_bot,
            edited_at, created_at
        ) VALUES (
            :chat_id, :message_id, :user_id, :alias, :nick, :nick_history, :sender_kind,
            :fwd_kind, :fwd_origin_name, :reply_to, :reply_to_user_id, :quote_text,
            :via_bot, :text, :urls, :media_kind, :file_unique_id, :is_bot, :from_other_bot,
            :edited_at, :created_at
        );
        """
        await self.db.execute(sql, full_dict)
        await self.db.commit()

    async def update_message_text(self, chat_id: int, message_id: int, text: str, edited_at: str) -> bool:
        assert self.db is not None
        cur = await self.db.execute(
            "UPDATE messages SET text = ?, edited_at = ? WHERE chat_id = ? AND message_id = ?",
            (text, edited_at, chat_id, message_id)
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def get_message(self, chat_id: int, message_id: int) -> Optional[dict[str, Any]]:
        assert self.db is not None
        async with self.db.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_last_messages(self, chat_id: int, limit: int = 60) -> list[dict[str, Any]]:
        assert self.db is not None
        async with self.db.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY message_id DESC LIMIT ?",
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            # Return in chronological order
            return [dict(r) for r in reversed(rows)]

    async def get_thread_ancestors(self, chat_id: int, start_reply_to: int, max_depth: int = 3) -> list[dict[str, Any]]:
        assert self.db is not None
        ancestors = []
        curr_id = start_reply_to
        for _ in range(max_depth):
            if not curr_id:
                break
            msg = await self.get_message(chat_id, curr_id)
            if not msg:
                break
            ancestors.append(msg)
            curr_id = msg.get("reply_to")
        return list(reversed(ancestors))

    async def get_message_neighbors(self, chat_id: int, center_id: int, count: int = 10) -> list[dict[str, Any]]:
        assert self.db is not None
        # count before and count after
        async with self.db.execute(
            """
            SELECT * FROM messages 
            WHERE chat_id = ? AND message_id >= ? - ? AND message_id <= ? + ?
            ORDER BY message_id ASC
            """,
            (chat_id, center_id, count, center_id, count)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # --- Retention cleanup ---

    async def cleanup_retention(self, chat_id: int, max_days: int = 30, max_rows: int = 20000) -> int:
        """Deletes messages older than max_days and keeps total messages <= max_rows. Skipped during import."""
        if self._import_active:
            return 0
        assert self.db is not None

        # 1. Clean by days
        deleted = 0
        cur1 = await self.db.execute(
            """
            DELETE FROM messages 
            WHERE chat_id = ? AND datetime(created_at) < datetime('now', '-' || ? || ' days')
            """,
            (chat_id, max_days)
        )
        deleted += cur1.rowcount

        # 2. Clean excess rows beyond max_rows
        cur2 = await self.db.execute(
            """
            DELETE FROM messages 
            WHERE chat_id = ? AND message_id NOT IN (
                SELECT message_id FROM messages WHERE chat_id = ? ORDER BY message_id DESC LIMIT ?
            )
            """,
            (chat_id, chat_id, max_rows)
        )
        deleted += cur2.rowcount
        await self.db.commit()
        return deleted

    # --- Dossier CRUD ---

    async def get_dossier(self, chat_id: int, user_id: int) -> Optional[dict[str, Any]]:
        assert self.db is not None
        async with self.db.execute(
            "SELECT * FROM dossiers WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_dossier(
        self,
        chat_id: int,
        user_id: int,
        dossier_data: str,
        version: int = 1,
        last_processed_seq: int = 0,
        was: Optional[str] = None
    ) -> None:
        assert self.db is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT OR REPLACE INTO dossiers (
                chat_id, user_id, dossier_data, version, last_processed_seq, updated_at, was
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, dossier_data, version, last_processed_seq, now_iso, was)
        )
        await self.db.commit()

    async def delete_dossier(self, chat_id: int, user_id: int) -> None:
        assert self.db is not None
        await self.db.execute(
            "DELETE FROM dossiers WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        await self.db.commit()

    # --- Opt Out ---

    async def set_opt_out(self, chat_id: int, user_id: int) -> None:
        assert self.db is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT OR REPLACE INTO opt_out (chat_id, user_id, created_at) VALUES (?, ?, ?)",
            (chat_id, user_id, now_iso)
        )
        await self.db.commit()

    async def remove_opt_out(self, chat_id: int, user_id: int) -> bool:
        assert self.db is not None
        cur = await self.db.execute(
            "DELETE FROM opt_out WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def is_opt_out(self, chat_id: int, user_id: int) -> bool:
        assert self.db is not None
        async with self.db.execute(
            "SELECT 1 FROM opt_out WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    # --- Chat Summary ---

    async def get_summary(self, chat_id: int) -> Optional[dict[str, Any]]:
        assert self.db is not None
        async with self.db.execute(
            "SELECT * FROM chat_summary WHERE chat_id = ?",
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_summary(self, chat_id: int, summary: str, msg_seq: int = 0) -> None:
        assert self.db is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT OR REPLACE INTO chat_summary (chat_id, summary, msg_seq, updated_at) VALUES (?, ?, ?, ?)",
            (chat_id, summary, msg_seq, now_iso)
        )
        await self.db.commit()

    # --- Media Cache ---

    async def get_media_cache(self, file_unique_id: str) -> Optional[dict[str, Any]]:
        assert self.db is not None
        async with self.db.execute(
            "SELECT * FROM media_cache WHERE file_unique_id = ?",
            (file_unique_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                # Update last_accessed
                now_iso = datetime.now(timezone.utc).isoformat()
                await self.db.execute(
                    "UPDATE media_cache SET last_accessed = ? WHERE file_unique_id = ?",
                    (now_iso, file_unique_id)
                )
                await self.db.commit()
                return dict(row)
            return None

    async def save_media_cache(self, item: dict[str, Any], max_items: int = 2000) -> None:
        assert self.db is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        item_copy = dict(item)
        item_copy.setdefault("created_at", now_iso)
        item_copy["last_accessed"] = now_iso

        sql = """
        INSERT OR REPLACE INTO media_cache (
            file_unique_id, kind, transcript, ocr_text, summary, track_meta,
            tags, verdict, score100, score_reason, unintelligible, injection_suspected,
            created_at, last_accessed
        ) VALUES (
            :file_unique_id, :kind, :transcript, :ocr_text, :summary, :track_meta,
            :tags, :verdict, :score100, :score_reason, :unintelligible, :injection_suspected,
            :created_at, :last_accessed
        );
        """
        await self.db.execute(sql, item_copy)

        # Evict LRU if beyond max_items
        await self.db.execute(
            """
            DELETE FROM media_cache WHERE file_unique_id NOT IN (
                SELECT file_unique_id FROM media_cache ORDER BY last_accessed DESC LIMIT ?
            )
            """,
            (max_items,)
        )
        await self.db.commit()

    # --- Research Cache ---

    async def get_research_cache(self, cache_key: str, ttl_hours: int = 72) -> Optional[dict[str, Any]]:
        assert self.db is not None
        async with self.db.execute(
            """
            SELECT * FROM research_cache 
            WHERE cache_key = ? AND datetime(created_at) >= datetime('now', '-' || ? || ' hours')
            """,
            (cache_key, ttl_hours)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                now_iso = datetime.now(timezone.utc).isoformat()
                await self.db.execute(
                    "UPDATE research_cache SET last_accessed = ? WHERE cache_key = ?",
                    (now_iso, cache_key)
                )
                await self.db.commit()
                return dict(row)
            return None

    async def save_research_cache(
        self,
        cache_key: str,
        query: Optional[str],
        url: Optional[str],
        result_text: str,
        max_items: int = 1000
    ) -> None:
        assert self.db is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT OR REPLACE INTO research_cache (
                cache_key, query, url, result_text, created_at, last_accessed
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cache_key, query, url, result_text, now_iso, now_iso)
        )
        # Evict LRU
        await self.db.execute(
            """
            DELETE FROM research_cache WHERE cache_key NOT IN (
                SELECT cache_key FROM research_cache ORDER BY last_accessed DESC LIMIT ?
            )
            """,
            (max_items,)
        )
        await self.db.commit()

    # --- Foreign Chats ---

    async def add_foreign_chat(self, chat_id: int, title: Optional[str] = None) -> None:
        assert self.db is not None
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT OR REPLACE INTO foreign_chats (chat_id, title, left_at) VALUES (?, ?, ?)",
            (chat_id, title or "", now_iso)
        )
        await self.db.commit()

    async def is_foreign_chat(self, chat_id: int) -> bool:
        assert self.db is not None
        async with self.db.execute(
            "SELECT 1 FROM foreign_chats WHERE chat_id = ?",
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    # --- Chat Migration (group -> supergroup) ---

    async def migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> None:
        """Migrates all records from old_chat_id to new_chat_id atomically."""
        assert self.db is not None
        async with self.db.cursor() as cur:
            await cur.execute("BEGIN TRANSACTION;")
            await cur.execute("UPDATE messages SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
            await cur.execute("UPDATE dossiers SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
            await cur.execute("UPDATE chat_summary SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
            await cur.execute("UPDATE opt_out SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
            await cur.execute("COMMIT;")
        logger.info("Chat migrated successfully from %s to %s", old_chat_id, new_chat_id)
