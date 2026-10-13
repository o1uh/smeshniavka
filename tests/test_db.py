import pytest
import aiosqlite
from pathlib import Path
from src.db.manager import DatabaseManager


@pytest.mark.asyncio
async def test_empty_db_and_migration(tmp_path: Path):
    db_file = tmp_path / "test.db"
    backups_dir = tmp_path / "backups"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()

    # Create dummy migration 001
    (mig_dir / "001_init.sql").write_text(
        "CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT);", encoding="utf-8"
    )

    manager = DatabaseManager(str(db_file), str(backups_dir))
    await manager.connect()

    assert await manager.get_user_version() == 0
    applied = await manager.apply_migrations(str(mig_dir))
    assert applied == [1]
    assert await manager.get_user_version() == 1

    # Idempotent second run
    applied_again = await manager.apply_migrations(str(mig_dir))
    assert applied_again == []
    assert await manager.get_user_version() == 1

    await manager.close()


@pytest.mark.asyncio
async def test_migration_backup_and_upgrade(tmp_path: Path):
    db_file = tmp_path / "test.db"
    backups_dir = tmp_path / "backups"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()

    (mig_dir / "001_init.sql").write_text(
        "CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT);", encoding="utf-8"
    )

    manager = DatabaseManager(str(db_file), str(backups_dir))
    await manager.connect()
    await manager.apply_migrations(str(mig_dir))

    # Insert data
    assert manager.db is not None
    await manager.db.execute("INSERT INTO test (val) VALUES ('hello');")
    await manager.db.commit()

    # Add migration 002
    (mig_dir / "002_add_col.sql").write_text(
        "ALTER TABLE test ADD COLUMN extra TEXT DEFAULT 'def';", encoding="utf-8"
    )

    applied = await manager.apply_migrations(str(mig_dir))
    assert applied == [2]
    assert await manager.get_user_version() == 2

    # Check backup created
    backups = list(backups_dir.glob("bot_pre_mig_v1_*.db"))
    assert len(backups) == 1

    # Check data preserved
    async with manager.db.execute("SELECT val, extra FROM test WHERE id = 1;") as cur:
        row = await cur.fetchone()
        assert row[0] == "hello"
        assert row[1] == "def"

    await manager.close()
