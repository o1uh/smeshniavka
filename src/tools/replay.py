import argparse
import asyncio
import os
import sys
from pathlib import Path

from src.ops.config import load_config
from src.ops.log import logger
from src.db.manager import DatabaseManager
from src.gateway.client import GeminiGateway
from src.context.prompts import PromptBuilder
from src.context.assemble import ContextAssembler


async def run_replay(last_n: int = 10, output_file: str = "reports/replay.md") -> None:
    config = load_config()
    db_mgr = DatabaseManager()
    await db_mgr.connect()
    await db_mgr.apply_migrations()

    prompts = PromptBuilder()
    assembler = ContextAssembler(config.bot, db_mgr, prompts)
    gateway = GeminiGateway(config)

    chat_id = config.bot.ALLOWED_CHAT_ID
    messages = await db_mgr.get_last_messages(chat_id, limit=last_n)
    if not messages:
        print(f"Нет сообщений для чата {chat_id} для проведения реплея.")
        await db_mgr.close()
        return

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Replay Report ({len(messages)} сообщений)",
        f"Дата генерации: {asyncio.get_event_loop().time()}",
        "",
    ]

    for m in messages:
        mid = m["message_id"]
        nick = m.get("nick") or "User"
        text = m.get("text") or "[media]"

        system_instruction, contents = await assembler.assemble(
            chat_id=chat_id,
            current_msg=m,
            mode="ping"
        )

        reply_res = await gateway.generate(
            prompt_name="persona_ping",
            contents=contents,
            system_instruction=system_instruction,
            priority="P0"
        )

        lines.append(f"### Msg {mid} от {nick}:")
        lines.append(f"> {text}")
        lines.append("")
        lines.append(f"**Ответил бы:** {reply_res}")
        lines.append("---")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(out_path, 0o600)
    except Exception:
        pass

    print(f"Отчет реплея успешно записан в {out_path}")
    await db_mgr.close()


def main():
    parser = argparse.ArgumentParser(description="Replay last messages in shadow mode")
    parser.add_argument("--last", type=int, default=10, help="Number of last messages to replay")
    parser.add_argument("--out", default="reports/replay.md", help="Output file")
    args = parser.parse_args()

    asyncio.run(run_replay(args.last, args.out))


if __name__ == "__main__":
    main()
