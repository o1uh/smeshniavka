import asyncio
import pytest

from src.tg.lanes import LanePool, LaneWorker, LaneJob, route_lane, LANE_CHAT, LANE_TRACK, LANE_MEDIA
from src.media.analyzer import contains_forbidden_score
from src.gateway.client import AntigravityGateway
from src.ops.config import BotConfig, AppConfig, PersonaConfig


def test_route_lane():
    assert route_lane(None) == LANE_CHAT
    assert route_lane("audio") == LANE_TRACK
    assert route_lane("photo") == LANE_MEDIA
    assert route_lane("video") == LANE_MEDIA
    assert route_lane("animation") == LANE_MEDIA
    assert route_lane("voice") == LANE_MEDIA
    assert route_lane("sticker") == LANE_MEDIA


def test_forbidden_score_only_non_audio():
    assert contains_forbidden_score("норм трек 85/100", "audio") is False
    assert contains_forbidden_score("шизокроссовер 70/100", "photo") is True
    assert contains_forbidden_score("без оценки", "photo") is False
    assert contains_forbidden_score("видос 10/100", "video") is True
    assert contains_forbidden_score("войс 50/100", "voice") is True


@pytest.mark.asyncio
async def test_lane_worker_fifo_and_overflow():
    worker = LaneWorker("test", max_queue=2)
    order = []

    async def _mk(i):
        async def _run():
            order.append(i)
        return _run

    worker.start()
    for i in range(3):
        await worker.submit(LaneJob(lane="test", label=f"j{i}", coro_factory=await _mk(i)))
    # overflow capacity is 2 -> oldest dropped, 2 jobs run
    await asyncio.sleep(0.3)
    await worker.stop()
    assert worker.dropped == 1
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_lane_pool_isolation():
    """Треки и медиа не блокируют друг друга: разные очереди/consumer'ы."""
    pool = LanePool(
        chat=LaneWorker("chat", max_queue=10),
        track=LaneWorker("track", max_queue=10),
        media=LaneWorker("media", max_queue=10),
    )
    pool.start_all()
    done = []

    async def _fast(name):
        async def _run():
            done.append(name)
        return _run

    await pool.get(LANE_CHAT).submit(LaneJob(lane=LANE_CHAT, label="c", coro_factory=await _fast("chat")))
    await pool.get(LANE_TRACK).submit(LaneJob(lane=LANE_TRACK, label="t", coro_factory=await _fast("track")))
    await pool.get(LANE_MEDIA).submit(LaneJob(lane=LANE_MEDIA, label="m", coro_factory=await _fast("media")))
    await asyncio.sleep(0.3)
    await pool.stop_all()
    assert sorted(done) == ["chat", "media", "track"]


def _cfg() -> AppConfig:
    bot = BotConfig(MODEL_ID="fast", MAX_CONCURRENT=4)
    return AppConfig(bot=bot, persona=PersonaConfig(), keys=[], limits={}, gen={}, tg_token="x")


def test_gateway_lane_mapping():
    cfg = _cfg()
    gw = AntigravityGateway(cfg)
    assert gw.lane_for_prompt("persona_ping") == "chat"
    assert gw.lane_for_prompt("persona_who") == "chat"
    assert gw.lane_for_prompt("proactive") == "chat"
    assert gw.lane_for_prompt("research_decide") == "chat"
    assert gw.lane_for_prompt("persona_media_reply") == "media"
    assert gw.lane_for_prompt("media_analyze") == "media"
    assert gw.lane_for_prompt("dossier_extract") == "sys"
    assert gw.lane_for_prompt("style_filter") == "sys"
    assert gw.lane_for_prompt("summary_chunk") == "sys"
    # явный lane побеждает
    assert gw.lane_for_prompt("media_analyze", lane="track") == "track"
    assert gw.lane_for_prompt("persona_media_reply", lane="track") == "track"
    assert gw.lane_for_prompt("persona_ping", lane="sys") == "sys"
