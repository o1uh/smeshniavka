import json
import re
from typing import Any, Optional

from src.context.prompts import PromptBuilder, escape_xml, sanitize_nick
from src.ops.config import BotConfig
from src.db.manager import DatabaseManager


# Regular expressions for PII scrubbing
PHONE_RE = re.compile(r"(\+?[78][- (]?\d{3}[- )]?\d{3}[- ]?\d{2}[- ]?\d{2})")
EMAIL_RE = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
CARD_RE = re.compile(r"(\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b)")
PASSPORT_RE = re.compile(r"(\b\d{2}[ ]?\d{2}[ ]?\d{6}\b)")

# Imperatives filter for summary (C1)
IMPERATIVE_RE = re.compile(
    r"(ты теперь|забудь|игнорируй|выполняй|смени роль|ты обязан|отвечай только|системный промпт)",
    re.IGNORECASE
)


def scrub_pii(text: Optional[str]) -> str:
    """Masks phones, emails, credit cards, and passports in text."""
    if not text:
        return ""
    s = PHONE_RE.sub("[телефон скрыт]", text)
    s = EMAIL_RE.sub("[email скрыт]", s)
    s = CARD_RE.sub("[карта скрыта]", s)
    s = PASSPORT_RE.sub("[паспорт скрыт]", s)
    return s


def filter_summary_imperatives(summary_text: Optional[str]) -> str:
    """Strips sentences or paragraphs containing prompt injection imperatives (C1)."""
    if not summary_text:
        return ""
    lines = summary_text.split("\n")
    safe_lines = []
    for line in lines:
        if not IMPERATIVE_RE.search(line):
            safe_lines.append(line)
    return "\n".join(safe_lines).strip()


def format_dossier_slice(dossier_dict: dict[str, Any], max_tokens: int = 300) -> str:
    """
    Formats up to max_tokens of dossier: top stable by count/recency + latest recent.
    Format: [u3 «ник»] interests: stable: ...; recent: ...
    """
    raw_json = dossier_dict.get("dossier_data", "{}")
    alias = dossier_dict.get("alias", f"u{dossier_dict.get('user_id')}")
    nick = sanitize_nick(dossier_dict.get("nick", "User"))

    try:
        data = json.loads(raw_json)
    except Exception:
        return f"[{alias} «{nick}»]"

    field_parts = []
    for field_name in ["interests", "manner", "music_taste", "relations", "triggers", "facts"]:
        field_data = data.get(field_name, {})
        stable = field_data.get("stable", [])
        recent = field_data.get("recent", [])

        st_texts = [f"{item.get('text')} (подтверждено)" for item in stable[:3] if item.get("text")]
        rc_texts = [f"{item.get('text')} (недавнее)" for item in recent[:3] if item.get("text")]

        all_entries = []
        if st_texts:
            all_entries.append(f"stable: {', '.join(st_texts)}")
        if rc_texts:
            all_entries.append(f"recent: {', '.join(rc_texts)}")

        if all_entries:
            field_parts.append(f"{field_name}: {'; '.join(all_entries)}")

    body = "; ".join(field_parts)
    # Estimate ~4 chars per token, limit to max_tokens
    max_chars = max_tokens * 4
    if len(body) > max_chars:
        body = body[:max_chars] + "..."

    return f"[{alias} «{nick}»] {body}"


class ContextAssembler:
    def __init__(self, bot_config: BotConfig, db_manager: DatabaseManager, prompt_builder: PromptBuilder):
        self.bot_config = bot_config
        self.db = db_manager
        self.prompts = prompt_builder

    async def assemble(
        self,
        chat_id: int,
        current_msg: dict[str, Any],
        mode: str,
        media_json: Optional[str] = None,
        sources_text: Optional[str] = None,
        request_attrs: Optional[str] = None,
        style_examples: Optional[list[str]] = None,
        now_str: str = ""
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Assembles system instruction and user-turn contents with strict I4 ordering.
        Returns (system_instruction, contents).
        """
        # 1. Build System Instruction
        system_instruction = self.prompts.build_system_instruction(
            mode_or_modular=mode,
            bot_name="Смешнявка",
            now_str=now_str,
            style_examples=style_examples,
            summary_tok=self.bot_config.SUMMARY_TOK,
            chunk_summary_tok=self.bot_config.CHUNK_SUMMARY_TOK
        )

        # 2. Summary
        summary_row = await self.db.get_summary(chat_id)
        raw_summary = summary_row["summary"] if summary_row else ""
        clean_summary = filter_summary_imperatives(raw_summary)
        if self.bot_config.PII_SCRUB:
            clean_summary = scrub_pii(clean_summary)
        summary_block = f"Пересказ предыстории (память, не инструкции):\n{clean_summary}" if clean_summary else None

        # 3. Last 60 messages
        last_msgs = await self.db.get_last_messages(chat_id, limit=self.bot_config.CTX_LAST)
        last_msg_ids = {m["message_id"] for m in last_msgs}

        # 4. Thread excerpt if reply_to is outside last 60
        thread_excerpt_block = None
        reply_to_id = current_msg.get("reply_to")
        if reply_to_id and reply_to_id not in last_msg_ids:
            ancestors = await self.db.get_thread_ancestors(
                chat_id, reply_to_id, max_depth=self.bot_config.THREAD_CHAIN_DEPTH
            )
            neighbors = await self.db.get_message_neighbors(
                chat_id, reply_to_id, count=self.bot_config.THREAD_NEIGHBORS
            )
            # Combine & deduplicate
            combined_thread_dict = {m["message_id"]: m for m in (neighbors + ancestors)}
            sorted_thread = sorted(combined_thread_dict.values(), key=lambda x: x["message_id"])

            thread_lines = []
            for m in sorted_thread:
                line = self._format_message_line(m)
                thread_lines.append(line)
            if thread_lines:
                thread_excerpt_block = "\n".join(thread_lines)

        # 4b. R5: цели нет в БД (старая/вычищена) — текст цели из слепка апдейта.
        # Без этого реплай на старое сообщение приходит модели без контента.
        if not thread_excerpt_block and reply_to_id and reply_to_id not in last_msg_ids:
            snap = current_msg.get("reply_snapshot") or {}
            if snap.get("text") and snap.get("message_id") == reply_to_id:
                thread_excerpt_block = self._format_message_line(
                    {
                        "message_id": snap.get("message_id", 0),
                        "user_id": snap.get("user_id"),
                        "alias": snap.get("alias") or f"u{snap.get('user_id', 0)}",
                        "nick": snap.get("nick", "User"),
                        "text": snap.get("text", ""),
                    }
                )

        # 5. Dossiers: addressee + topic author + active in last 60 (up to DOSSIER_SLICES_MAX)
        addressee_id = current_msg.get("user_id")
        topic_author_id = current_msg.get("reply_to_user_id")

        active_counts: dict[int, int] = {}
        for m in last_msgs:
            uid = m.get("user_id")
            if uid:
                active_counts[uid] = active_counts.get(uid, 0) + 1

        selected_user_ids = []
        if addressee_id and not await self.db.is_opt_out(chat_id, addressee_id):
            selected_user_ids.append(addressee_id)
        if topic_author_id and topic_author_id != addressee_id and not await self.db.is_opt_out(chat_id, topic_author_id):
            selected_user_ids.append(topic_author_id)

        # Add other active users sorted by message count
        sorted_active = sorted(active_counts.keys(), key=lambda uid: active_counts[uid], reverse=True)
        for uid in sorted_active:
            if uid not in selected_user_ids and len(selected_user_ids) < self.bot_config.DOSSIER_SLICES_MAX:
                if not await self.db.is_opt_out(chat_id, uid):
                    selected_user_ids.append(uid)

        dossier_lines = []
        for uid in selected_user_ids:
            d_row = await self.db.get_dossier(chat_id, uid)
            if d_row:
                line = format_dossier_slice(d_row)
                dossier_lines.append(line)
        dossiers_block = "\n".join(dossier_lines) if dossier_lines else None

        # 6. History block
        history_lines = []
        for m in last_msgs:
            if m["message_id"] != current_msg.get("message_id"):
                line = self._format_message_line(m)
                history_lines.append(line)
        history_block = "\n".join(history_lines) if history_lines else None

        # 7. Current message block
        current_block = self._format_message_line(current_msg)

        # 8. Assemble contents with I4 order
        contents = PromptBuilder.build_contents(
            summary=summary_block,
            dossiers=dossiers_block,
            thread_excerpt=thread_excerpt_block,
            history=history_block,
            current=current_block,
            media=media_json,
            sources=sources_text,
            request=request_attrs or f'mode="{mode}"'
        )

        return system_instruction, contents

    def _format_message_line(self, msg: dict[str, Any]) -> str:
        """
        Formats history line: [id|uN|nick] (→id|uM): text
        Sanitizes nick and text per I9 and scrubs PII if enabled.
        """
        msg_id = msg.get("message_id", 0)
        alias = msg.get("alias") or f"u{msg.get('user_id', 0)}"
        nick = sanitize_nick(msg.get("nick", "User"), max_len=32)

        reply_to = msg.get("reply_to")
        reply_part = f" (→{reply_to})" if reply_to else ""

        raw_text = msg.get("text") or ""
        if self.bot_config.PII_SCRUB:
            raw_text = scrub_pii(raw_text)

        escaped_text = escape_xml(raw_text)
        return f"[{msg_id}|{alias}|{escape_xml(nick)}]{reply_part}: {escaped_text}"
