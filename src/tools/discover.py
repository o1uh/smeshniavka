import asyncio
import os
import sys
import time
from aiogram import Bot, Dispatcher, types
from aiogram.enums import UpdateType

from src.ops.config import load_env_file, Path


async def run_discover(token: str, duration_s: int = 120) -> None:
    print(f"=== Discover mode started ({duration_s}s) ===")
    print("Напишите любое сообщение в целевой группе Telegram...")
    print("Бот НЕ сохраняет сообщения в БД и НЕ отвечает.\n")

    bot = Bot(token=token)
    dp = Dispatcher()

    @dp.message()
    async def on_message(message: types.Message):
        chat = message.chat
        user = message.from_user
        print(f"----------------------------------------")
        print(f"CHAT ID:   {chat.id}")
        print(f"CHAT TYPE: {chat.type} ({chat.title or 'Private'})")
        if user:
            print(f"USER ID:   {user.id}")
            print(f"USER NICK: {user.full_name} (@{user.username or 'none'})")
        print(f"TEXT:      {message.text or message.caption or '[media]'}")
        print(f"----------------------------------------\n")

    try:
        polling_task = asyncio.create_task(dp.start_polling(bot, allowed_updates=[UpdateType.MESSAGE]))
        await asyncio.sleep(duration_s)
        polling_task.cancel()
    except asyncio.CancelledError:
        pass
    finally:
        await bot.session.close()
        print("\n=== Discover mode finished ===")


def main():
    env = load_env_file(Path(".env"))
    token = os.environ.get("TG_TOKEN") or env.get("TG_TOKEN")
    if not token:
        print("Ошибка: TG_TOKEN не найден в .env или переменных окружения!", file=sys.stderr)
        sys.exit(1)

    duration = 120
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        duration = int(sys.argv[1])

    asyncio.run(run_discover(token, duration))


if __name__ == "__main__":
    main()
