import os
import re
from pathlib import Path
from typing import Any, Optional


ALLOWED_PLACEHOLDERS = {
    "<<BOT_NAME>>",
    "<<NOW>>",
    "<<STYLE_EXAMPLES>>",
    "<<SUMMARY_TOK>>",
    "<<CHUNK_SUMMARY_TOK>>",
}


def escape_xml(text: Optional[str]) -> str:
    """Escapes XML/HTML special characters: &, <, >."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sanitize_nick(nick: Optional[str], max_len: int = 32) -> str:
    """
    Sanitizes user nick / sticker pack name per I9:
    - remove control characters, newlines, brackets `[]`
    - limit length to max_len (default 32)
    """
    if not nick:
        return "Unknown"
    # Remove newlines, carriage returns, tabs, control characters, brackets
    s = re.sub(r"[\r\n\t\[\]\x00-\x1f\x7f-\x9f]", " ", str(nick))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] if s else "Unknown"


class PromptBuilder:
    def __init__(self, prompts_dir: str = "prompts"):
        self.prompts_dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

    def _read_prompt(self, filename: str) -> str:
        if filename not in self._cache:
            path = self.prompts_dir / filename
            if not path.exists():
                raise FileNotFoundError(f"Файл промпта {path} не найден!")
            self._cache[filename] = path.read_text(encoding="utf-8")
        return self._cache[filename]

    def build_system_instruction(
        self,
        mode_or_modular: str,
        bot_name: str = "Смешнявка",
        now_str: str = "",
        style_examples: Optional[list[str]] = None,
        summary_tok: int = 800,
        chunk_summary_tok: int = 300,
    ) -> str:
        """
        Builds system instruction according to TB-15:
        - persona modes: persona_base.txt + '\n\n' + mode_<mode>.txt
        - modular calls: <modular>.txt alone
        - safe str.replace of whitelisted placeholders with order:
          BOT_NAME, NOW, SUMMARY_TOK, CHUNK_SUMMARY_TOK, and STYLE_EXAMPLES last
        - checks for any remaining '<<' and raises ValueError if found.
        """
        persona_modes = {"ping", "proactive", "media_reply", "who", "research"}
        if mode_or_modular in persona_modes:
            base = self._read_prompt("persona_base.txt")
            mode_text = self._read_prompt(f"mode_{mode_or_modular}.txt")
            raw = f"{base}\n\n{mode_text}"
        else:
            raw = self._read_prompt(f"{mode_or_modular}.txt")

        # 1. Substitute BOT_NAME, NOW, SUMMARY_TOK, CHUNK_SUMMARY_TOK first
        res = raw.replace("<<BOT_NAME>>", bot_name)
        res = res.replace("<<NOW>>", now_str)
        res = res.replace("<<SUMMARY_TOK>>", str(summary_tok))
        res = res.replace("<<CHUNK_SUMMARY_TOK>>", str(chunk_summary_tok))

        # 2. Escape any '<<' or '>>' inside style examples so they can never expand placeholders
        formatted_examples = ""
        if style_examples:
            escaped_examples = []
            for ex in style_examples:
                safe_ex = str(ex).replace("<<", "&lt;&lt;").replace(">>", "&gt;&gt;")
                escaped_examples.append(f"- {safe_ex}")
            formatted_examples = "\n".join(escaped_examples)

        res = res.replace("<<STYLE_EXAMPLES>>", formatted_examples)

        # 3. Validation: no unresolved '<<' placeholder should remain
        if "<<" in res:
            remaining = re.findall(r"<<[^>]+>>", res)
            raise ValueError(f"Обнаружены неразрешённые плейсхолдеры в промпте: {remaining}")

        return res

    @staticmethod
    def build_contents(
        summary: Optional[str] = None,
        dossiers: Optional[str] = None,
        thread_excerpt: Optional[str] = None,
        history: Optional[str] = None,
        current: Optional[str] = None,
        media: Optional[str] = None,
        sources: Optional[str] = None,
        request: Optional[str] = None,
        custom_blocks: Optional[list[tuple[str, str]]] = None
    ) -> list[dict[str, Any]]:
        """
        Builds Gemini user-turn contents with strict I4 ordering:
        <summary> -> <dossiers> -> <thread_excerpt> -> <history> -> <current>
        Optional after current: <media>, <sources>, <request>, or custom_blocks.
        """
        parts = []

        if custom_blocks:
            for tag, content in custom_blocks:
                if content is not None:
                    parts.append(f"<{tag}>\n{content}\n</{tag}>")
            combined = "\n\n".join(parts)
            return [{"role": "user", "parts": [{"text": combined}]}]

        if summary is not None:
            parts.append(f"<summary>\n{summary}\n</summary>")
        if dossiers is not None:
            parts.append(f"<dossiers>\n{dossiers}\n</dossiers>")
        if thread_excerpt is not None:
            parts.append(f'<thread_excerpt note="старое">\n{thread_excerpt}\n</thread_excerpt>')
        if history is not None:
            parts.append(f"<history>\n{history}\n</history>")
        if current is not None:
            parts.append(f"<current>\n{current}\n</current>")
        if media is not None:
            parts.append(f"<media>\n{media}\n</media>")
        if sources is not None:
            parts.append(f"<sources>\n{sources}\n</sources>")
        if request is not None:
            parts.append(f"<request {request}/>" if not request.startswith("<") else request)

        combined = "\n\n".join(parts)
        return [{"role": "user", "parts": [{"text": combined}]}]
