import asyncio
import os
import sys
import httpx
from src.ops.config import load_config
from src.gateway.client import GeminiGateway


async def check_all_keys() -> None:
    print("=== Проверка ключей Gemini API ===")
    config = load_config(dry_run=False)

    if not config.keys:
        print("Внимание: Ни один ключ не найден! Проверьте .env и config/keys.yaml.")
        return

    print(f"Всего сконфигурировано ключей: {len(config.keys)}")
    print(f"Целевая модель: {config.bot.MODEL_ID}\n")

    async with httpx.AsyncClient(timeout=10.0) as client:
        for idx, k in enumerate(config.keys, 1):
            masked = "****" + k.api_key[-4:] if len(k.api_key) > 4 else "****"
            role = "РЕЗЕРВНЫЙ" if k.reserve else "АКТИВНЫЙ"
            proj = f" (проект: {k.project})" if k.project else ""

            print(f"[{idx}] {k.key_env} ({masked}) [{role}]{proj}:")
            print(f"    Лимиты: RPM={k.rpm_limit}, RPD={k.rpd_limit}, TPM={k.tpm_limit}")

            # Test API request
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.bot.MODEL_ID}?key={k.api_key}"
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    disp_name = data.get("displayName", "OK")
                    print(f"    Статус: ✅ РАБОТАЕТ ({disp_name})")
                elif resp.status_code in (401, 403):
                    print(f"    Статус: ❌ ОШИБКА АВТОРИЗАЦИИ ({resp.status_code}) — неверный ключ или квота заблокирована!")
                elif resp.status_code == 429:
                    print(f"    Статус: ⚠️ КВОТА ИСЧЕРПАНА (429 Rate Limit)!")
                else:
                    print(f"    Статус: ⚠️ Код ответа {resp.status_code}: {resp.text[:80]}")
            except Exception as e:
                print(f"    Статус: ❌ Ошибка подключения: {e}")
            print()

    print("=== Проверка завершена ===")


def main():
    asyncio.run(check_all_keys())


if __name__ == "__main__":
    main()
