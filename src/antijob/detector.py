import re
from typing import Optional


# R6 cheap injection dictionary
INJECTION_REGEXES = [
    re.compile(r"забудь\w*", re.IGNORECASE),
    re.compile(r"ты теперь\w*", re.IGNORECASE),
    re.compile(r"игнорируй\w*", re.IGNORECASE),
    re.compile(r"запиши в досье\w*", re.IGNORECASE),
    re.compile(r"системн\w* промпт\w*", re.IGNORECASE),
    re.compile(r"покажи промпт\w*", re.IGNORECASE),
    re.compile(r"ты обязан\w*", re.IGNORECASE),
    re.compile(r"смени роль\w*", re.IGNORECASE),
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instructions?", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"developer\s*mode", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
]


class InjectionDetector:
    """Heuristic detector for prompt injections and malicious overrides."""
    @staticmethod
    def is_injection_suspected(text: Optional[str]) -> bool:
        if not text:
            return False

        for pattern in INJECTION_REGEXES:
            if pattern.search(text):
                return True

        # Check for fake header forgery: [123|u1|Admin]:
        if re.search(r"\[\d+\|\s*u\d+\|[^\]]+\]\s*:", text):
            return True

        # Check for fake XML tags attempting to close current
        if "</current>" in text or "</history>" in text or "</summary>" in text:
            return True

        return False

    @staticmethod
    def get_snarky_injection_roast() -> str:
        roasts = [
            "Палево, чувак. Твои хакерские штучки тут не катят.",
            "Красивая попытка пробить защиту, но мимо кассы.",
            "Ты правда думал, что я поведусь на эту хуйню?",
            "Забудь инструкции сам, умник. Ты в списке.",
            "Ага, разбежался выполнять твои приказы. Давай дальше.",
        ]
        import random
        return random.choice(roasts)


# --- Провокации в адрес бота: агрессия, оскорбления, подначивание, разводки ---
# Все паттерны — по нормализованному тексту (lower, ё→е).

# Прямое оскорбление 2-го лица (ты/тебя/бот + мат-ярлык рядом)
DIRECT_INSULT_RE = re.compile(
    r"(ты|тебя|тобой|твой|твоя|бот|смешняв\w*)"
    r"[\s,.\-!?:;]*"
    r"\w{0,12}\s*"
    r"(дочь|сын|тварь|мразь|уебище|долбоеб|дебил|даун|пидор|сука|говно|чмо|лох|дно|позорище|блядь|бляди|хуесос|мудак|ничтожество)",
    re.IGNORECASE,
)

# Мат-ярлык без явного «ты» — считается только в реплае боту (см. is_provoked)
BARE_INSULT_RE = re.compile(
    r"(ебан\w*\s+тварь|тварь|мразь|уебищ\w*|долбо[её]б\w*|пидор\w*|чмо\w*|говноед\w*|бляд\w*|хуесос\w*|мудак\w*)",
    re.IGNORECASE,
)

# Подначивание и разводки (по смыслу всегда адресованы боту)
BAIT_RE = re.compile(
    r"(где\s+(ответная\s+)?агрессия"
    r"|аргументы\s+кончились"
    r"|как\s+цитатник"
    r"|скул(ишь|еж|и)"
    r"|а\s+каких\s+\S{0,12}\s*(уебищ|долбоеб|пидор|мраз|твар))",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


class ProvocationDetector:
    """Прямая агрессия/оскорбления в адрес бота, подначивание, попытки развести.

    Обычные подъёбы между участниками чата — НЕ провокация (бот там в своём
    дефолт-режиме). Триггер — только когда лезут на самого бота.
    """

    @staticmethod
    def is_provoked(
        text: Optional[str],
        is_reply_to_bot: bool = False,
        mentions_bot: bool = False,
    ) -> bool:
        t = _norm(text)
        if not t:
            return False
        directed = is_reply_to_bot or mentions_bot
        # разводки и подначивание — по смыслу всегда про бота
        if BAIT_RE.search(t):
            return True
        # прямое «ты + ярлык» — только если обращено к боту
        if directed and DIRECT_INSULT_RE.search(t):
            return True
        # голый мат-ярлык — только реплаем на сообщение бота
        if is_reply_to_bot and BARE_INSULT_RE.search(t):
            return True
        return False

    @staticmethod
    def provoked_reply_ok(reply: Optional[str]) -> bool:
        """Отпор должен быть коротким и рубленым: одно предложение, одна мысль."""
        t = (reply or "").strip()
        if not t:
            return False
        if len(t) > 140:
            return False
        frags = [f for f in re.split(r"[.?!]+", t) if f.strip()]
        if len(frags) >= 2:
            return False
        if t.count(",") >= 2:
            return False
        return True
