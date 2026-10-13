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
