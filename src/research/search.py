import hashlib
import re
from typing import Any, Optional

from src.ops.config import BotConfig
from src.ops.log import logger
from src.db.manager import DatabaseManager
from src.db.contracts import Gateway
from src.gateway.schemas import ResearchDecision
from src.context.prompts import PromptBuilder


USER_ANON_RE = re.compile(r"(@[a-zA-Z0-9_]+|\bu\d+\b|\b\d{8,12}\b)")


def anonymize_query(query: str, known_nicks: Optional[list[str]] = None) -> Optional[str]:
    """
    Anonymizes query per R8:
    - strips @username, uN, numeric ids
    - strips known participant nicks
    """
    if not query:
        return None

    clean = USER_ANON_RE.sub("", query)
    if known_nicks:
        for nick in known_nicks:
            if nick and len(nick) >= 3:
                clean = re.sub(re.escape(nick), "", clean, flags=re.IGNORECASE)

    clean = re.sub(r"\s+", " ", clean).strip()
    # If query is too short after anonymization, cannot search
    if len(clean) < 3:
        return None
    return clean


class ResearchModule:
    """Handles query deciding, query anonymization, caching, and research responses per R8."""

    def __init__(
        self,
        bot_config: BotConfig,
        db_manager: DatabaseManager,
        gateway: Gateway,
        prompt_builder: PromptBuilder
    ):
        self.config = bot_config
        self.db = db_manager
        self.gateway = gateway
        self.prompts = prompt_builder

    def should_force_search(self, text: str) -> bool:
        t_low = text.lower()
        return any(trig in t_low for trig in self.config.SEARCH_FORCE_TRIGGERS)

    def should_show_links(self, text: str) -> bool:
        t_low = text.lower()
        return any(trig in t_low for trig in self.config.LINK_TRIGGERS_SHOW)

    async def decide_search(
        self,
        history_text: str,
        current_text: str,
        known_nicks: Optional[list[str]] = None
    ) -> tuple[bool, Optional[str]]:
        """
        Determines whether search is needed. Returns (need_search, anonymized_query).
        """
        forced = self.should_force_search(current_text)

        system_instruction = self.prompts.build_system_instruction(
            mode_or_modular="research_decide",
            bot_name="Смешнявка"
        )
        contents = PromptBuilder.build_contents(
            history=history_text,
            current=current_text,
            request=f'forced="{"true" if forced else "false"}"'
        )

        dec_result: Optional[ResearchDecision] = await self.gateway.generate(
            prompt_name="research_decide",
            contents=contents,
            system_instruction=system_instruction,
            priority="P0",
            response_schema=ResearchDecision,
            estimated_tokens=200
        )

        if not dec_result or not dec_result.need_search:
            return False, None

        raw_query = dec_result.query or current_text
        safe_query = anonymize_query(raw_query, known_nicks)
        if not safe_query:
            logger.info("Search query could not be anonymized, skipping search")
            return False, None

        return True, safe_query

    async def execute_research(
        self,
        query: str,
        chat_id: int,
        current_msg: dict[str, Any],
        system_instruction: str,
        contents: list[dict[str, Any]],
        show_links: bool = False
    ) -> Optional[str]:
        """
        Executes Google Search tool call or retrieves from research_cache.
        """
        cache_key = hashlib.sha256(f"q:{query}".encode()).hexdigest()[:16]
        cached = await self.db.get_research_cache(cache_key, ttl_hours=self.config.RESEARCH_TTL_H)
        if cached:
            logger.info("Research query '%s' served from cache", query)
            return cached["result_text"]

        # Call Gemini with Google Search tool enabled
        res = await self.gateway.generate(
            prompt_name="persona_research",
            contents=contents,
            system_instruction=system_instruction,
            priority="P0",
            enable_search=True,
            estimated_tokens=700
        )

        if isinstance(res, str) and res.strip():
            # If show_links is false, ensure no bare URLs leaked
            if not show_links:
                # Strip markdown/html links
                res = re.sub(r'<a\s+href="[^"]*">([^<]*)</a>', r"\1", res)
                res = re.sub(r'https?://[^\s<>"]+', "", res)

            await self.db.save_research_cache(
                cache_key=cache_key,
                query=query,
                url=None,
                result_text=res,
                max_items=self.config.RESEARCH_CACHE_MAX
            )
            return res

        return None
