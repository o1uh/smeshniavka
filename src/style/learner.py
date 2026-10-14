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


def prefilter_ok(text: Optional[str], min_len: int = 6, max_len: int = 150, allow_single: bool = False) -> bool:
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
    if not allow_single and len(t.split()) < 2 and len(t) < 10:
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

    def _pinned_seeds(self) -> list[str]:
        seeds = getattr(self.config, "STYLE_PINNED_SEEDS", None) or []
        return [str(s) for s in seeds if str(s).strip()]

    def _read_active(self, path: Path) -> tuple[list[str], list[str]]:
        """(examples, pinned): пермач отдельно, вытеснение его не касается."""
        examples = [a for a in self._read_yaml_list(path, "examples") if isinstance(a, str)]
        pinned = [a for a in self._read_yaml_list(path, "pinned") if isinstance(a, str)]
        return examples, pinned

    def _ensure_seeds(self) -> None:
        """Сиды пермача (дефолт «бум») — дописать в файл один раз, если их там нет."""
        seeds = self._pinned_seeds()
        if not seeds:
            return
        _, active_path = self._paths()
        examples, pinned = self._read_active(active_path)
        missing = [s for s in seeds if s not in pinned and s not in examples]
        if missing:
            self._write_yaml(active_path, {"examples": examples, "pinned": pinned + missing})

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
            self._ensure_seeds()
            cand_path, active_path = self._paths()
            candidates = self._read_yaml_list(cand_path, "candidates")
            examples, pinned = self._read_active(active_path)
            known = {c.get("text") for c in candidates if isinstance(c, dict)}
            known |= set(examples) | set(pinned)
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

    async def collect_reacted(
        self, chat_id: int, user_id: Optional[int], text: Optional[str], direct: bool = False
    ) -> bool:
        """Кандидат по смеякам.

        direct=True (3+ смеяк): сразу в банк, минуя очередь и фильтр — смех чата
        и есть вердикт (фильтр бы «бум» выкинул как «без контекста»). Но это
        НЕ пермач: при переполнении вытесняется на общих основаниях, пермач —
        только сиды. Безопасность кодовая: PII-маска, без ссылок/упоминаний.
        direct=False (1–2 смеяки): в очередь на отбор обычным путём,
        но префильтр мягче — короткое «бум»/«вау» тоже смотрим.
        """
        if not self.enabled or not text:
            return False
        max_len = int(getattr(self.config, "STYLE_MAX_LEN", 150) or 150)
        max_active = int(getattr(self.config, "STYLE_EXAMPLES_MAX", 128) or 128)
        clean = scrub_pii(text.strip())
        # Прайм по смеякам: одиночное «бум»/«вау» со смеяками тоже смотрим
        if not prefilter_ok(clean, min_len=2, max_len=max_len, allow_single=True):
            return False
        if user_id:
            try:
                if await self.db.is_opt_out(chat_id, user_id):
                    return False
            except Exception:
                return False

        async with self._lock:
            self._ensure_seeds()
            cand_path, active_path = self._paths()
            examples, pinned = self._read_active(active_path)
            if direct:
                if clean in set(examples) | set(pinned):
                    return False
                examples.append(clean)
                self._write_yaml(active_path, {"examples": examples, "pinned": pinned})
                # В банке — из кандидатов убрать дубль
                candidates = self._read_yaml_list(cand_path, "candidates")
                candidates = [c for c in candidates if c.get("text") != clean]
                self._write_yaml(cand_path, {"candidates": candidates})
                over = len(examples) > max_active
                logger.warning("Style learner: DIRECT to bank by laughs (%.40s...)", clean)
            else:
                candidates = self._read_yaml_list(cand_path, "candidates")
                known = {c.get("text") for c in candidates if isinstance(c, dict)}
                known |= set(examples) | set(pinned)
                if clean in known:
                    return False
                cid = f"c{int(time.time())}{random.randint(100, 999)}"
                candidates.append({"id": cid, "text": clean, "ts": int(time.time())})
                candidates = candidates[-200:]
                ok = self._write_yaml(cand_path, {"candidates": candidates})
                over = False
                if ok:
                    logger.info("Style learner: reacted candidate queued (%.40s...)", clean)
                return ok
        # Переполнение прямым добавлением — сразу чистим слабых, не ждём промоушна
        if over:
            await self._apply_weakest_eviction(examples, max_active)
        return True

    async def maybe_promote(self) -> int:
        """Прогнать полный батч кандидатов через style_filter и дописать ok в актив.

        Возвращает число добавленных примеров.
        """
        if not self.enabled:
            return 0
        batch_n = int(getattr(self.config, "STYLE_FILTER_BATCH", 30) or 30)
        max_active = int(getattr(self.config, "STYLE_EXAMPLES_MAX", 128) or 128)
        async with self._lock:
            self._ensure_seeds()
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
            self._ensure_seeds()
            cand_path, active_path = self._paths()
            candidates = self._read_yaml_list(cand_path, "candidates")
            done_ids = {c.get("id") for c in batch}
            candidates = [c for c in candidates if c.get("id") not in done_ids]
            self._write_yaml(cand_path, {"candidates": candidates})
            added = 0
            need_evict = False
            if ok_texts:
                examples, pinned = self._read_active(active_path)
                have = set(examples) | set(pinned)
                for t in ok_texts:
                    if t not in have:
                        examples.append(t)
                        have.add(t)
                        added += 1
                if len(examples) > max_active:
                    need_evict = True
                    evict_snapshot = list(examples)
                else:
                    self._write_yaml(active_path, {"examples": examples, "pinned": pinned})

        # Переполнение: слабых выкидываем вне лока (там сетевые вызовы),
        # иначе сбор кандидатов встанет на время фильтрации всего банка
        if need_evict:
            await self._apply_weakest_eviction(evict_snapshot, max_active)
        if added:
            logger.info("Style learner: promoted %d examples to active bank", added)
        return added

    async def _apply_weakest_eviction(self, snapshot: list[str], max_active: int) -> None:
        """Выкинуть слабые примеры из переполненного банка (см. _filter_weak).

        Пермач (pinned) не трогаем никогда.
        """
        weak = await self._filter_weak(snapshot)
        async with self._lock:
            _, active_path = self._paths()
            examples, pinned = self._read_active(active_path)
            if weak is None:
                # Фильтр лёг — честный FIFO
                examples = examples[-max_active:] if max_active > 0 else []
            else:
                dropped = len([t for t in examples if t in weak])
                examples = [t for t in examples if t not in weak]
                if dropped:
                    logger.info("Style learner: evicting %d weakest examples", dropped)
                if len(examples) > max_active:
                    examples = examples[-max_active:] if max_active > 0 else []
            self._write_yaml(active_path, {"examples": examples, "pinned": pinned})

    async def _filter_weak(self, active: list[str]) -> Optional[set[str]]:
        """Прогнать банк через style_filter. Возвращает тексты слабых или None при ошибке."""
        batch_n = int(getattr(self.config, "STYLE_FILTER_BATCH", 30) or 30)
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
            weak: set[str] = set()
            for start in range(0, len(active), batch_n):
                chunk = active[start:start + batch_n]
                lines = "\n".join(f"[e{start + i}] {t}" for i, t in enumerate(chunk))
                verdicts: Optional[StyleVerdicts] = await self.gateway.generate(
                    prompt_name="style_filter",
                    contents=[{"role": "user", "parts": [{"text": f"<candidates>\n{lines}\n</candidates>"}]}],
                    system_instruction=system_instruction,
                    priority="P2",
                    response_schema=StyleVerdicts,
                    estimated_tokens=2000,
                    lane="sys",
                )
                by_idx = {v.id: v.ok for v in (verdicts.verdicts if verdicts else [])}
                for i, t in enumerate(chunk):
                    if by_idx.get(f"e{start + i}") is False:
                        weak.add(t)
            return weak
        except Exception as e:
            logger.warning("Style learner: eviction filter failed, FIFO fallback: %s", e)
            return None
