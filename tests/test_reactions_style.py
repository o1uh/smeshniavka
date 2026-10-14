import asyncio
import pytest
import yaml

from src.tg.reactions import ReactionsTracker
from src.style.learner import StyleLearner
from src.gateway.schemas import StyleVerdicts, StyleVerdict
from src.ops.config import BotConfig
from src.db.contracts import FakeSender
from src.persona.reply import TgSendQueue


def test_laugh_threshold_fires_once():
    tr = ReactionsTracker(threshold=4)
    for uid in (1, 2, 3):
        assert tr.handle(1, 10, uid, {"😁"}) is False
    assert tr.handle(1, 10, 4, {"😁"}) is True
    # повторный апдейт — уже прайм, молчим
    assert tr.handle(1, 10, 5, {"😂"}) is False
    assert tr.laugh_count(1, 10) == 5


def test_removal_and_non_laugh_ignored():
    tr = ReactionsTracker(threshold=2)
    assert tr.handle(1, 10, 1, {"😁"}) is False
    assert tr.handle(1, 10, 2, {"👍"}) is False  # не смеяка — не считаем
    assert tr.laugh_count(1, 10) == 1
    assert tr.handle(1, 10, 1, set()) is False  # снял реакцию
    assert tr.laugh_count(1, 10) == 0
    assert tr.handle(1, 10, 2, {"🤣"}) is False
    assert tr.handle(1, 10, 3, {"😆"}) is True


def test_anonymous_skipped():
    tr = ReactionsTracker(threshold=1)
    assert tr.handle(1, 10, None, {"😁"}) is False


class FakeDB:
    def __init__(self, opt_out=False):
        self.opt_out = opt_out

    async def is_opt_out(self, chat_id, user_id):
        return self.opt_out


class FakeGateway:
    """ok_map: id -> ok. Неизвестным id отвечает ok=True."""

    def __init__(self, ok_map=None, fail=False):
        self.ok_map = dict(ok_map or {})
        self.fail = fail
        self.calls = 0

    async def generate(self, **kwargs):
        import re
        self.calls += 1
        if self.fail:
            raise RuntimeError("bridge down")
        text = kwargs["contents"][0]["parts"][0]["text"]
        ids = re.findall(r"\[([^\]]+)\]", text)
        return StyleVerdicts(
            verdicts=[StyleVerdict(id=i, ok=self.ok_map.get(i, True), reason="fine" if self.ok_map.get(i, True) else "junk")
                      for i in ids]
        )


def _learner(tmp_path, gw, max_active=5, batch=10):
    cfg = BotConfig()
    cfg.STYLE_FILTER_BATCH = batch
    cfg.STYLE_EXAMPLES_MAX = max_active
    return StyleLearner(cfg, FakeDB(), gw, prompts_dir=str(tmp_path))


def _write_active(tmp_path, examples):
    (tmp_path / "style_examples.yaml").write_text(
        yaml.safe_dump({"examples": examples}, allow_unicode=True), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_overflow_evicts_weakest_not_oldest(tmp_path):
    _write_active(tmp_path, ["сильный старый хит", "слабый мусор", "свежий хит"])
    gw = FakeGateway(ok_map={"e0": True, "e1": False, "e2": True})
    learner = _learner(tmp_path, gw, max_active=2, batch=10)
    # переполнение 3 > 2: слабый вылетает, сильный старый остаётся
    await learner._apply_weakest_eviction(["сильный старый хит", "слабый мусор", "свежий хит"], 2)
    active = yaml.safe_load((tmp_path / "style_examples.yaml").read_text(encoding="utf-8"))["examples"]
    assert "слабый мусор" not in active
    assert "сильный старый хит" in active
    assert len(active) == 2


@pytest.mark.asyncio
async def test_eviction_fifo_fallback_on_filter_fail(tmp_path):
    _write_active(tmp_path, ["первый", "второй", "третий"])
    learner = _learner(tmp_path, FakeGateway(fail=True), max_active=2, batch=10)
    await learner._apply_weakest_eviction(["первый", "второй", "третий"], 2)
    active = yaml.safe_load((tmp_path / "style_examples.yaml").read_text(encoding="utf-8"))["examples"]
    assert active == ["второй", "третий"]


@pytest.mark.asyncio
async def test_prime_goes_first(tmp_path):
    learner = _learner(tmp_path, FakeGateway(), max_active=32, batch=30)
    assert await learner.collect_from_message(1, 5, "обычная фраза из чата") is True
    assert await learner.collect_prime(1, 7, "хуюл") is True
    data = yaml.safe_load((tmp_path / "style_examples.candidates.yaml").read_text(encoding="utf-8"))
    assert data["candidates"][0]["text"] == "хуюл"
    assert data["candidates"][0].get("prime") is True


@pytest.mark.asyncio
async def test_send_queue_on_sent_callback():
    sender = FakeSender()
    cfg = BotConfig()
    cfg.SHADOW_MODE = False  # в shadow отправки нет — колбэка тоже
    q = TgSendQueue(sender, cfg)
    got = []

    async def _cb(chat_id, message_id, text, reply_to):
        got.append((chat_id, message_id, text, reply_to))

    q.on_sent = _cb
    q.start()
    await q.enqueue(chat_id=1, text="хуюл", reply_to_message_id=10, priority="P0")
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.05)
    await q.stop()
    assert got and got[0][0] == 1 and got[0][2] == "хуюл" and got[0][3] == 10
    assert isinstance(got[0][1], int)
