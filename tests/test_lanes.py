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
        track=LaneWorker("track1", max_queue=10),
        track2=LaneWorker("track2", max_queue=10),
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


@pytest.mark.asyncio
async def test_track_round_robin_two_workers():
    """Два трековых воркера делят очередь: треки идут пачками, не в один consumer."""
    from src.tg.lanes import LANE_SYS
    pool = LanePool(
        chat=LaneWorker("chat", max_queue=10),
        track=LaneWorker("track1", max_queue=10),
        track2=LaneWorker("track2", max_queue=10),
        media=LaneWorker("media", max_queue=10),
        sys=LaneWorker("sys", max_queue=10),
    )
    w1 = pool.get(LANE_TRACK)
    w2 = pool.get(LANE_TRACK)
    w3 = pool.get(LANE_TRACK)
    assert {w1.name, w2.name} == {"track1", "track2"}
    assert w3.name == w1.name  # round-robin по кругу
    assert pool.get(LANE_SYS).name == "sys"
    assert set(pool.sizes().keys()) == {"chat", "track", "media", "sys"}


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


def test_gateway_track_two_slots():
    """Трековый лейн держит 2 параллельных трека (2 воркера)."""
    cfg = _cfg()
    gw = AntigravityGateway(cfg)
    assert gw.track_max == 2
    assert gw.chat_max == 1
    assert gw._lane_limit("track") == 2


@pytest.mark.asyncio
async def test_is_admin_check():
    from unittest.mock import AsyncMock, MagicMock
    from src.tg.bot import FunnyBotApp

    app = MagicMock(spec=FunnyBotApp)
    app.bot_cfg = MagicMock()
    app.bot_cfg.OWNER_IDS = [12345]
    app.bot = MagicMock()

    # 1. Owner
    res = await FunnyBotApp._is_admin(app, chat_id=-1001, user_id=12345)
    assert res is True

    # 2. None
    res_none = await FunnyBotApp._is_admin(app, chat_id=-1001, user_id=None)
    assert res_none is False

    # 3. Chat admin
    member_admin = MagicMock()
    member_admin.status = "administrator"
    app.bot.get_chat_member = AsyncMock(return_value=member_admin)
    res_admin = await FunnyBotApp._is_admin(app, chat_id=-1001, user_id=999)
    assert res_admin is True

    # 4. Regular member
    member_user = MagicMock()
    member_user.status = "member"
    app.bot.get_chat_member = AsyncMock(return_value=member_user)
    res_user = await FunnyBotApp._is_admin(app, chat_id=-1001, user_id=888)
    assert res_user is False
