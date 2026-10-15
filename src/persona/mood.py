"""Настроение чата: serious (обсуждают по делу) vs banter (срач/угар).

Дешёвая кодовая эвристика без LLM-вызовов: смотрим текст пинга и пару
последних сообщений. Ошибка в сторону neutral — тогда работает дефолт.
"""
import re
from typing import Optional

# Просят объяснить/разобрать/подсказать — серьёзный разговор
SERIOUS_RE = re.compile(
    r"(объясни|разъясни|поясни|растолкуй|подскажи|посоветуй|разбери|"
    r"как\s+работает|как\s+сделать|как\s+настроить|почему\s+так|"
    r"что\s+значит|что\s+такое|кто\s+такой|правда\s+ли|"
    r"подробно|детально|пласт|гайд|инструкци|помоги|вопрос\s+есть)",
    re.IGNORECASE,
)

# Мат и наезды — маркер угара/срача
MAT_RE = re.compile(
    r"(хуй|пизд|еба|бля|сука|мраз|тварь|уебищ|долбоеб|дебил|даун|"
    r"пидор|чмо|говно|лох|мудак|хуесос|еблан|завали|иди\s+нахуй|пошел)",
    re.IGNORECASE,
)

LAUGH_RE = re.compile(r"[😁😂🤣😆😹🤭]|ахаха|лол|кек|ору", re.IGNORECASE)


def detect_mood(text: Optional[str], recent_texts: Optional[list[str]] = None) -> str:
    """Возвращает 'serious' | 'banter' | 'neutral'.

    recent_texts — последние несколько сообщений чата (уже без разметки).
    """
    t = (text or "").strip()
    recent = list(recent_texts or [])[-5:]
    blob = " ".join(recent)

    mat_hits = len(MAT_RE.findall(t)) + len(MAT_RE.findall(blob))
    laugh_hits = len(LAUGH_RE.findall(t)) + len(LAUGH_RE.findall(blob))
    serious_hit = bool(SERIOUS_RE.search(t))

    # Угар перевешивает: мат в пинге или срач вокруг
    if mat_hits >= 2 or (mat_hits >= 1 and laugh_hits >= 1):
        return "banter"
    if laugh_hits >= 3:
        return "banter"
    # Серьёзно — только если просят по делу И вокруг не срач
    if serious_hit and mat_hits == 0:
        return "serious"
    # Длинный вопрос без мата и смеха — скорее по делу
    if len(t) > 120 and mat_hits == 0 and laugh_hits == 0 and "?" in t:
        return "serious"
    return "neutral"
