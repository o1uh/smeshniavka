import asyncio
import time
import pytest
import httpx
from pydantic import BaseModel

from src.ops.config import BotConfig, AppConfig, PersonaConfig, KeyConfig
from src.gateway.client import AntigravityGateway, GeminiGateway, KeyBucket


class DummySchema(BaseModel):
    message: str


def make_test_config() -> AppConfig:
    bot = BotConfig(
        MODEL_ID="fast",
        BRIDGE_URL="http://127.0.0.1:8000",
        MAX_CONCURRENT=4,
        RPM_P0_RESERVE=2,
        BUDGET_P2_MAX=70,
        BUDGET_P1_MAX=90
    )
    persona = PersonaConfig()
    return AppConfig(
        bot=bot,
        persona=persona,
        keys=[],
        limits={},
        gen={"test_prompt": {"temperature": 0.7, "max_output_tokens": 100}},
        tg_token="fake_token"
    )


@pytest.mark.asyncio
async def test_bridge_successful_generation():
    app_cfg = make_test_config()

    def handler(request: httpx.Request):
        url = str(request.url)
        if "/v1/chat/completions" in url:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Success from bridge"},
                            "finish_reason": "stop"
                        }
                    ],
                    "usage": {"total_tokens": 42}
                }
            )
        elif "/health" in url:
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gw = AntigravityGateway(app_cfg, http_client=client)

        res = await gw.generate(
            prompt_name="test_prompt",
            contents=[{"role": "user", "parts": [{"text": "hello"}]}],
            priority="P0",
            session_id="tg_12345"
        )

        assert res == "Success from bridge"
        assert gw.requests_count == 1
        assert gw.is_healthy is True


@pytest.mark.asyncio
async def test_bridge_structured_output():
    app_cfg = make_test_config()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '```json\n{"message": "structured_hello"}\n```'},
                        "finish_reason": "stop"
                    }
                ]
            }
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gw = AntigravityGateway(app_cfg, http_client=client)

        res = await gw.generate(
            prompt_name="test_prompt",
            contents=[{"role": "user", "parts": [{"text": "give json"}]}],
            priority="P0",
            response_schema=DummySchema
        )

        assert isinstance(res, DummySchema)
        assert res.message == "structured_hello"


@pytest.mark.asyncio
async def test_bridge_blocked_by_safety():
    app_cfg = make_test_config()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "SAFETY"
                    }
                ]
            }
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        gw = AntigravityGateway(app_cfg, http_client=client)

        res = await gw.generate("test_prompt", [], priority="P0")
        assert res == {"is_blocked": True}


@pytest.mark.asyncio
async def test_bridge_error_returns_degraded_for_p0():
    app_cfg = make_test_config()

    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Bridge Error"))
    async with httpx.AsyncClient(transport=transport) as client:
        gw = AntigravityGateway(app_cfg, http_client=client)

        # P0 request on bridge error returns degraded status dict
        res_p0 = await gw.generate("test_prompt", [], priority="P0")
        assert res_p0 == {"is_degraded": True}
        assert gw.is_healthy is False

        # Non-P0 request on bridge error returns None
        res_p1 = await gw.generate("test_prompt", [], priority="P1")
        assert res_p1 is None
