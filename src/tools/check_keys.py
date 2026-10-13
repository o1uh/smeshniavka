import asyncio
import os
import sys
import httpx
from src.ops.config import load_config
from src.gateway.client import AntigravityGateway


async def check_all() -> None:
    print("=== Проверка подключения к Antigravity Bridge ===")
    config = load_config(dry_run=False)

    gw = AntigravityGateway(config)
    resolved_url = await gw._resolve_bridge_url()

    print(f"Целевой URL моста: {resolved_url}")
    print(f"Целевая модель: {config.bot.MODEL_ID}\n")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Health check
        print("[1] Проверка /health:")
        try:
            resp = await client.get(f"{resolved_url}/health")
            if resp.status_code == 200:
                data = resp.json()
                print(f"    Статус: ✅ OK (сервис: {data.get('service')}, uptime: {data.get('uptime_seconds')}с)")
                print(f"    Модель по умолчанию: {data.get('default_model')}")
                print(f"    Сессий активно: {data.get('active_sessions')}, warm pool: {data.get('warm_pool_size')}")
            else:
                print(f"    Статус: ⚠️ Код ответа {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            print(f"    Статус: ❌ Ошибка подключения к мосту: {e}")
            return

        # 2. Models list
        print("\n[2] Доступные модели (/v1/models):")
        try:
            resp = await client.get(f"{resolved_url}/v1/models")
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                print(f"    Всего моделей/алиасов: {len(models)}")
                for m in models[:6]:
                    fast_tag = "⚡ FAST" if m.get("is_fast") else "  SLOW"
                    print(f"    [{fast_tag}] {m['id']:<24} -> {m.get('description', '')[:45]}")
            else:
                print(f"    Статус: ⚠️ Код ответа {resp.status_code}")
        except Exception as e:
            print(f"    Ошибка: {e}")

        # 3. Test generation through Gateway
        print("\n[3] Тестовая генерация ответа через Gateway:")
        try:
            test_contents = [{"role": "user", "parts": [{"text": "Скажи привет одним коротким предложением."}]}]
            answer = await gw.generate(
                prompt_name="test_check",
                contents=test_contents,
                priority="P0",
                estimated_tokens=50
            )
            print(f"    Ответ от Antigravity Bridge: ✅ {answer.strip() if isinstance(answer, str) else answer}")
        except Exception as e:
            print(f"    ❌ Ошибка генерации: {e}")

    print("\n=== Проверка завершена ===")


def main():
    asyncio.run(check_all())


if __name__ == "__main__":
    main()
