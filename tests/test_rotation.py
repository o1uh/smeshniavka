import asyncio
import time
import pytest
import httpx
from pydantic import BaseModel

from src.ops.config import BotConfig, AppConfig, PersonaConfig, KeyConfig
from src.gateway.client import GeminiGateway, KeyBucket


class DummySchema(BaseModel):
    message: str


def make_test_config(keys_configs: list[KeyConfig]) -> AppConfig:
    bot = BotConfig(
        MODEL_ID="gemini-2.5-flash",
        MAX_CONCURRENT=4,
        RPM_P0_RESERVE=2,
        BUDGET_P2_MAX=70,
        BUDGET_P1_MAX=90
    )
    persona = PersonaConfig()
    return AppConfig(
        bot=bot,
        persona=persona,
        keys=keys_configs,
        limits={},
        gen={"test_prompt": {"temperature": 1.0, "max_output_tokens": 100}},
        tg_token="fake_token"
    )


@pytest.mark.asyncio
async def test_immediate_429_failover_and_minute_cooldown():
    # Setup two active keys
    k1 = KeyConfig(key_env="KEY_1", api_key="key1_secret", rpm_limit=15, rpd_limit=500)
    k2 = KeyConfig(key_env="KEY_2", api_key="key2_secret", rpm_limit=15, rpd_limit=500)
    app_cfg = make_test_config([k1, k2])

    call_counts = {"key1_secret": 0, "key2_secret": 0}

    def handler(request: httpx.Request):
        url = str(request.url)
        if "key=key1_secret" in url:
            call_counts["key1_secret"] += 1
            # Return 429 PerMinute
            return httpx.Response(
                429,
                json={"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}
            )
        elif "key=key2_secret" in url:
            call_counts["key2_secret"] += 1
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "Success from key 2"}]}}]}
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gw = GeminiGateway(app_cfg, http_client=client)

        res = await gw.generate(
            prompt_name="test_prompt",
            contents=[{"role": "user", "parts": [{"text": "hello"}]}],
            priority="P0"
        )

        assert res == "Success from key 2"
        # Key 1 was called and failed, Key 2 succeeded in the same generate() call!
        assert call_counts["key1_secret"] == 1
        assert call_counts["key2_secret"] == 1

        # Key 1 must be in cooldown now
        key1_bucket = gw.keys[0]
        assert key1_bucket.is_in_cooldown(time.time())
        assert key1_bucket.cooldown_until > time.time()


@pytest.mark.asyncio
async def test_budget_thresholds():
    # 1 active key with limit 100
    k1 = KeyConfig(key_env="KEY_1", api_key="k1", rpm_limit=15, rpd_limit=100)
    app_cfg = make_test_config([k1])

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
    async with httpx.AsyncClient(transport=transport) as client:
        gw = GeminiGateway(app_cfg, http_client=client)

        # Set rpd_used to 70 (70%): P2 should be rejected
        gw.keys[0].rpd_used = 70
        res_p2 = await gw.generate("test_prompt", [], priority="P2")
        assert res_p2 is None  # Blocked by budget

        # But P1 and P0 should still be allowed
        res_p1 = await gw.generate("test_prompt", [], priority="P1")
        assert res_p1 == "ok"

        # Set rpd_used to 90 (90%): P1 should be rejected
        gw.keys[0].rpd_used = 90
        res_p1_blocked = await gw.generate("test_prompt", [], priority="P1")
        assert res_p1_blocked is None

        # P0 should still work up to 100%
        res_p0 = await gw.generate("test_prompt", [], priority="P0")
        assert res_p0 == "ok"


@pytest.mark.asyncio
async def test_blocked_does_not_trigger_cooldown():
    k1 = KeyConfig(key_env="KEY_1", api_key="k1", rpm_limit=15, rpd_limit=100)
    app_cfg = make_test_config([k1])

    def handler(request: httpx.Request):
        # Simulated Gemini prompt safety block
        return httpx.Response(
            200,
            json={"promptFeedback": {"blockReason": "SAFETY"}}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gw = GeminiGateway(app_cfg, http_client=client)

        res = await gw.generate("test_prompt", [], priority="P0")
        assert res == {"is_blocked": True}

        # Key should NOT be in cooldown!
        assert not gw.keys[0].is_in_cooldown(time.time())


@pytest.mark.asyncio
async def test_all_keys_in_cooldown_returns_degraded_immediately():
    k1 = KeyConfig(key_env="KEY_1", api_key="k1", rpm_limit=15, rpd_limit=100)
    app_cfg = make_test_config([k1])

    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        gw = GeminiGateway(app_cfg, http_client=client)
        gw.keys[0].cooldown_until = time.time() + 300.0

        # P0 request should immediately return degraded
        start_t = time.time()
        res = await gw.generate("test_prompt", [], priority="P0")
        elapsed = time.time() - start_t

        assert res == {"is_degraded": True}
        assert elapsed < 0.2  # Immediate response without waiting
