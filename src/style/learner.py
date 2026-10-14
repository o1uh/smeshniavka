"""Живой банк стиля: сбор кандидатов из чата + промоушн через style_filter.

Дообучения модели нет — адаптация только через few-shot примеры. Раньше
сбор был описан лишь в архитектурном плане: candidates пустовал, в активе
лежали 16 зашитых фраз. Теперь бот сам тащит живые фразы из срачей:
короткие жёсткие реплики участников копятся в
prompts/style_examples.candidates.yaml, батчами уходят на P2-проверку
style_filter.txt, прошедшие дописываются в prompts/style_examples.yaml.

Правила отбора (TB-05/R2): PII-маска, ≤150 символов, предпочтение коротким
жёстким в духе хорошего бота, словарь R6, P2-проверка батчами.
Файлы в .gitignore, права 600; на RPi prompts/ должен быть на запись
(см. docker-compose.yml) и rsync не должен их затирать (см. deploy.sh).
"""
import asyncio
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from src.ops.log import logger
from src.context.assemble import scrub_pii
from src.gateway.schemas import StyleVerdicts

CANDIDATES_FILE = "style_examples.candidates.yaml"
ACTIVE_FILE = "style_examples.yaml"

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@\w+|\bu\d+\b|tg://\S+", re.IGNORECASE)
LONG_DIGITS_RE = re.compile(r"\b\d{8,}\b")
CODE_FENCE_RE = re.compile(r"```|^\s*[-*•]\s+|\d+\.\s+", re.MULTILINE)


def prefilter_ok(text: Optional[str], min_len: int = 6, max_len: int = 150) -> bool:
    """Дешёвый кодовый префильтр до LLM-проверки: только живой текст чата."""
    if not text:
        return False
    t = text.strip()
    if not (min_len <= len(t) <= max_len):
        return False
    if t.startswith("/") or t.startswith("["):
        return False  # команды и кодовые заглушки ([фото], [стикер]...)
    if URL_RE.search(t) or MENTION_RE.search(t) or LONG_DIGITS_RE.search(t):
        return False  # ссылки/упоминания/id — не тащим в банк
    if CODE_FENCE_RE.search(t):
        return False
    if len(t.split()) < 2 and len(t) < 10:
        return False  # одиночные междометия без контекста не самодостаточны
    return True


class StyleLearner:
    """Сбор + промоушн примеров стиля. Вся LLM-работа — фоном P2."""

    def __init__(
        self,
        bot_config: Any,
        db_manager: Any,
        gateway: Any,
        prompts_dir: str = "prompts",
    ):
        self.config = bot_config
        self.db = db_manager
        self.gateway = gateway
        self.prompts_dir = Path(prompts_dir)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "STYLE_LEARN_ENABLED", True))

    def _paths(self) -> tuple[Path, Path]:
        return (
            self.prompts_dir / CANDIDATES_FILE,
            self.prompts_dir / ACTIVE_FILE,
        )

    def _read_yaml_list(self, path: Path, key: str) -> list:
        try:
            if not path.exists():
                return []
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            items = data.get(key, [])
            return items if isinstance(items, list) else []
        except Exception as e:
            logger.warning("Style learner: failed to read %s: %s", path, e)
            return []

    def _write_yaml(self, path: Path, payload: dict) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning("Style learner: failed to write %s: %s", path, e)
            return False

    async def collect_from_message(
        self, chat_id: int, user_id: Optional[int], text: Optional[str]
    ) -> bool:
        """Положить живую фразу в кандидаты. Возвращает True если взяли."""
        if not self.enabled or not user_id or not text:
            return False
        min_len = int(getattr(self.config, "STYLE_MIN_LEN", 6) or 6)
        max_len = int(getattr(self.config, "STYLE_MAX_LEN", 150) or 150)
        clean = scrub_pii(text.strip())
        if not prefilter_ok(clean, min_len, max_len):
            return False
        try:
            if await self.db.is_opt_out(chat_id, user_id):
                return False
        except Exception:
            return False

        async with self._lock:
            cand_path, active_path = self._paths()
            candidates = self._read_yaml_list(cand_path, "candidates")
            active = self._read_yaml_list(active_path, "examples")
            known = {c.get("text") for c in candidates if isinstance(c, dict)}
            known |= {a for a in active if isinstance(a, str)}
            if clean in known:
                return False
            cid = f"c{int(time.time())}{random.randint(100, 999)}"
            candidates.append({"id": cid, "text": clean, "ts": int(time.time())})
            # Потолок кандидатов, чтобы файл не пух
            candidates = candidates[-200:]
            ok = self._write_yaml(cand_path, {"candidates": candidates})
        if ok:
            logger.info("Style learner: candidate collected (%.40s...)", clean)
        return ok

    async def maybe_promote(self) -> int:
        """Прогнать полный батч кандидатов через style_filter и дописать ok в актив.

        Возвращает число добавленных примеров.
        """
        if not self.enabled:
            return 0
        batch_n = int(getattr(self.config, "STYLE_FILTER_BATCH", 30) or 30)
        max_active = int(getattr(self.config, "STYLE_EXAMPLES_MAX", 128) or 128)
        async with self._lock:
            cand_path, active_path = self._paths()
            candidates = self._read_yaml_list(cand_path, "candidates")
            if len(candidates) < batch_n:
                return 0
            batch = candidates[:batch_n]
            rest = candidates[batch_n:]

        lines = "\n".join(f"[{c.get('id')}] {c.get('text')}" for c in batch)
        filter_path = self.prompts_dir / "style_filter.txt"
        try:
            system_instruction = (
                filter_path.read_text(encoding="utf-8")
                if filter_path.exists()
                else "Отбери короткие живые реплики."
            )
        except Exception:
            system_instruction = "Отбери короткие живые реплики."
        try:
            verdicts: Optional[StyleVerdicts] = await self.gateway.generate(
                prompt_name="style_filter",
                contents=[{"role": "user", "parts": [{"text": f"<candidates>\n{lines}\n</candidates>"}]}],
                system_instruction=system_instruction,
                priority="P2",
                response_schema=StyleVerdicts,
                estimated_tokens=2000,
                lane="sys",
            )
        except Exception as e:
            logger.warning("Style learner: filter call failed: %s", e)
            return 0

        ok_ids: set[str] = set()
        try:
            for v in (verdicts.verdicts if verdicts else []):
                if v.ok:
                    ok_ids.add(v.id)
        except Exception:
            pass
        ok_texts = [c.get("text") for c in batch if c.get("id") in ok_ids and c.get("text")]

        async with self._lock:
            cand_path, active_path = self._paths()
            candidates = self._read_yaml_list(cand_path, "candidates")
            done_ids = {c.get("id") for c in batch}
            candidates = [c for c in candidates if c.get("id") not in done_ids]
            self._write_yaml(cand_path, {"candidates": candidates})
            added = 0
            if ok_texts:
                active = [a for a in self._read_yaml_list(active_path, "examples") if isinstance(a, str)]
                have = set(active)
                for t in ok_texts:
                    if t not in have:
                        active.append(t)
                        have.add(t)
                        added += 1
                # FIFO-потолок: свежие срачи вытесняют старые сиды
                active = active[-max_active:]
                self._write_yaml(active_path, {"examples": active})
        if added:
            logger.info("Style learner: promoted %d examples to active bank", added)
        return added
