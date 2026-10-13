import os
import shutil
import sqlite3
import sys
from pathlib import Path


def restore_backup(backup_path_str: str, target_db_str: str = "data/bot.db") -> bool:
    backup_path = Path(backup_path_str)
    target_db = Path(target_db_str)

    if not backup_path.exists():
        print(f"Ошибка: Файл бэкапа {backup_path} не найден!", file=sys.stderr)
        return False

    # Validate that backup is a valid SQLite DB
    try:
        conn = sqlite3.connect(str(backup_path))
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchone()
        conn.close()
        if not res or res[0] != "ok":
            print(f"Ошибка: Целостность файла бэкапа повреждена ({res})!", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Ошибка проверки базы данных бэкапа: {e}", file=sys.stderr)
        return False

    # Create safety backup of current target DB if it exists
    if target_db.exists():
        safety_path = target_db.with_suffix(".pre_restore.bak")
        shutil.copy2(target_db, safety_path)
        print(f"Создана страховочная копия текущей БД: {safety_path}")

    # Remove any WAL/SHM files
    wal = target_db.with_name(target_db.name + "-wal")
    shm = target_db.with_name(target_db.name + "-shm")
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)

    # Overwrite target DB
    shutil.copy2(backup_path, target_db)
    os.chmod(target_db, 0o600)
    print(f"Успешно восстановлена БД из {backup_path} в {target_db}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Использование: python -m src.tools.restore <путь_к_бэкапу>")
        sys.exit(1)

    backup_file = sys.argv[1]
    ok = restore_backup(backup_file)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
