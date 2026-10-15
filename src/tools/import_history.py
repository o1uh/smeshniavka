import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from src.ops.config import load_config
from src.ops.log import logger
from src.db.manager import DatabaseManager
from src.gateway.client import GeminiGateway
from src.gateway.schemas import SummaryOut, DossierExtract
from src.context.prompts import PromptBuilder
from src.context.assemble import scrub_pii, filter_summary_imperatives
from src.tg.stubs import extract_clean_urls


async def parse_history_file(
    json_path: Path,
    db_mgr: DatabaseManager,
    max_days: int = 30
) -> int:
    """
    Parses Telegram Desktop JSON export and inserts messages into SQLite.
    Returns number of imported messages.
    """
    if not json_path.exists():
        raise FileNotFoundError(f"Файл экспорта {json_path} не найден!")

    logger.info("Parsing Telegram export from %s (max_days=%d)...", json_path, max_days)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    raw_messages = data.get("messages", [])

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_days)
    imported_count = 0
    db_mgr.import_active = True

    for item in raw_messages:
        # Check message type
        if item.get("type") != "message":
            continue

        date_str = item.get("date")
        if not date_str:
            continue

        try:
            msg_dt = datetime.fromisoformat(date_str)
            if msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=timezone.utc)
            if msg_dt < cutoff_date:
                continue
        except Exception:
            continue

        msg_id = item.get("id")
        from_id_str = str(item.get("from_id") or "")
        user_id = int(from_id_str.replace("user", "")) if from_id_str.startswith("user") else None

        nick = item.get("from") or "Unknown"
        alias = f"u{user_id}" if user_id else "u0"

        # Text extraction (can be string or list of text/entities)
        raw_text_obj = item.get("text")
        if isinstance(raw_text_obj, list):
            text_parts = []
            for part in raw_text_obj:
                if isinstance(part, dict):
                    text_parts.append(part.get("text", ""))
                else:
                    text_parts.append(str(part))
            text = "".join(text_parts)
        else:
            text = str(raw_text_obj or "")

        # Strip contact/location information per I1
        if item.get("contact_information") or item.get("action") == "contact":
            text = "[контакт]"
        elif item.get("location_information") or item.get("action") == "location":
            text = "[геопозиция]"

        media_kind = item.get("media_type")
        urls = extract_clean_urls(text)
        urls_json = json.dumps(urls) if urls else None

        msg_dict = {
            "chat_id": 0,  # Updated by caller or discover
            "message_id": msg_id,
            "user_id": user_id,
            "alias": alias,
            "nick": nick,
            "text": text,
            "urls": urls_json,
            "media_kind": media_kind,
            "created_at": msg_dt.isoformat(),
        }
        await db_mgr.save_message(msg_dict)
        imported_count += 1

    db_mgr.import_active = False
    logger.info("Successfully imported %d messages.", imported_count)
    return imported_count


async def run_summary_and_dossier_import(
    db_mgr: DatabaseManager,
    gateway: GeminiGateway,
    prompt_builder: PromptBuilder,
    chat_id: int,
    max_calls: int = 300,
    window_size: int = 400
) -> None:
    """
    Runs map-reduce summary and dossier extraction over historical windows.
    """
    logger.info("Starting summary and dossier import (max_calls=%d)...", max_calls)
    messages = await db_mgr.get_last_messages(chat_id, limit=20000)
    if not messages:
        logger.warning("No messages to process for summary/dossier import.")
        return

    calls_made = 0

    # 1. Map-reduce Summary
    chunks = [messages[i:i + 100] for i in range(0, len(messages), 100)]
    chunk_summaries = []

    for chunk in chunks:
        if calls_made >= max_calls:
            break

        lines = [f"[{m['message_id']}|{m.get('alias')}|{m.get('nick')}]: {m.get('text')}" for m in chunk]
        contents = PromptBuilder.build_contents(
            custom_blocks=[("chunk", "\n".join(lines))]
        )
        system_instruction = prompt_builder.build_system_instruction(
            mode_or_modular="summary_chunk",
            bot_name="Дик"
        )

        res: Optional[SummaryOut] = await gateway.generate(
            prompt_name="summary_chunk",
            contents=contents,
            system_instruction=system_instruction,
            priority="P2",
            response_schema=SummaryOut,
            estimated_tokens=600
        )
        calls_made += 1
        if res and res.summary:
            chunk_summaries.append(res.summary)

    # Merge chunks
    if chunk_summaries and calls_made < max_calls:
        merge_lines = [f"[{idx + 1}] {s}" for idx, s in enumerate(chunk_summaries)]
        contents = PromptBuilder.build_contents(
            custom_blocks=[("chunks", "\n".join(merge_lines))]
        )
        system_instruction = prompt_builder.build_system_instruction(
            mode_or_modular="summary_merge",
            bot_name="Дик"
        )
        merged: Optional[SummaryOut] = await gateway.generate(
            prompt_name="summary_merge",
            contents=contents,
            system_instruction=system_instruction,
            priority="P2",
            response_schema=SummaryOut,
            estimated_tokens=1500
        )
        calls_made += 1
        if merged and merged.summary:
            clean_sum = filter_summary_imperatives(merged.summary)
            await db_mgr.save_summary(chat_id, clean_sum, msg_seq=messages[-1]["message_id"])
            logger.info("Saved final merged summary to DB.")

    logger.info("Import finished. Total calls made: %d", calls_made)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import Telegram Desktop history")
    parser.add_argument("stage", choices=["parse", "summary+dossier"], help="Stage to run")
    parser.add_argument("--file", default="data/result.json", help="Path to Telegram export JSON")
    args = parser.parse_args()

    config = load_config()
    db_mgr = DatabaseManager()
    await db_mgr.connect()
    await db_mgr.apply_migrations()

    if args.stage == "parse":
        await parse_history_file(Path(args.file), db_mgr, max_days=config.bot.IMPORT_MAX_DAYS)
    elif args.stage == "summary+dossier":
        gateway = GeminiGateway(config)
        prompts = PromptBuilder()
        await run_summary_and_dossier_import(
            db_mgr=db_mgr,
            gateway=gateway,
            prompt_builder=prompts,
            chat_id=config.bot.ALLOWED_CHAT_ID,
            max_calls=config.bot.IMPORT_MAX_CALLS,
            window_size=config.bot.IMPORT_DOSSIER_WINDOW
        )

    await db_mgr.close()


if __name__ == "__main__":
    asyncio.run(main())
