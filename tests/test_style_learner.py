import pytest
import yaml

from src.style.learner import StyleLearner, prefilter_ok
from src.gateway.schemas import StyleVerdicts, StyleVerdict
from src.ops.config import BotConfig


class FakeDB:
    def __init__(self, opt_out=False):
        self.opt_out = opt_out

    async def is_opt_out(self, chat_id, user_id):
        return self.opt_out


class FakeGateway:
    def __init__(self, ok_ids):
        self.ok_ids = set(ok_ids)
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return StyleVerdicts(
            verdicts=[StyleVerdict(id=cid, ok=(cid in self.ok_ids), reason="fine" if cid in self.ok_ids else "junk")
                      for cid in self._ids(kwargs)]
        )

    @staticmethod
    def _ids(kwargs):
        import re
        text = kwargs["contents"][0]["parts"][0]["text"]
        return re.findall(r"\[(c[^\]]+)\]", text)


def _learner(tmp_path, **cfg_over):
    cfg = BotConfig()
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return StyleLearner(cfg, FakeDB(), FakeGateway(ok_ids=set()), prompts_dir=str(tmp_path))


def test_prefilter():
    assert prefilter_ok("завали ебало даун") is True
    assert prefilter_ok("хуюл, спать пиздуй") is True
    assert prefilter_ok("ку") is False  # слишком коротко и без контекста
    assert prefilter_ok("/alive") is False
    assert prefilter_ok("[фото]") is False
    assert prefilter_ok("чекни https://example.com/x") is False
    assert prefilter_ok("привет @vladimir") is False
    assert prefilter_ok("напиши u123") is False
    assert prefilter_ok("x" * 151) is False


@pytest.mark.asyncio
async def test_collect_dedupe_and_opt_out(tmp_path):
    learner = _learner(tmp_path)
    assert await learner.collect_from_message(1, 5, "завали ебало даун") is True
    # дубль не берём
    assert await learner.collect_from_message(1, 5, "завали ебало даун") is False
    # opt-out не учим
    learner.db = FakeDB(opt_out=True)
    assert await learner.collect_from_message(1, 5, "иди поспи реально") is False
    data = yaml.safe_load((tmp_path / "style_examples.candidates.yaml").read_text(encoding="utf-8"))
    assert len(data["candidates"]) == 1


@pytest.mark.asyncio
async def test_promote_batch(tmp_path):
    cfg = BotConfig()
    cfg.STYLE_FILTER_BATCH = 3
    cfg.STYLE_EXAMPLES_MAX = 5
    gw = FakeGateway(ok_ids=set())
    learner = StyleLearner(cfg, FakeDB(), gw, prompts_dir=str(tmp_path))
    phrases = ["завали ебало даун", "иди поспи реально", "ты че так трясешься"]
    for p in phrases:
        assert await learner.collect_from_message(1, 5, p) is True
    # фильтр пропускает 2 из 3
    data = yaml.safe_load((tmp_path / "style_examples.candidates.yaml").read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["candidates"]]
    gw.ok_ids = {ids[0], ids[2]}
    added = await learner.maybe_promote()
    assert added == 2
    active = yaml.safe_load((tmp_path / "style_examples.yaml").read_text(encoding="utf-8"))
    assert active["examples"] == [phrases[0], phrases[2]]
    # батч съеден из кандидатов
    rest = yaml.safe_load((tmp_path / "style_examples.candidates.yaml").read_text(encoding="utf-8"))
    assert rest["candidates"] == []


@pytest.mark.asyncio
async def test_no_promote_until_batch_full(tmp_path):
    cfg = BotConfig()
    cfg.STYLE_FILTER_BATCH = 30
    gw = FakeGateway(ok_ids=set())
    learner = StyleLearner(cfg, FakeDB(), gw, prompts_dir=str(tmp_path))
    await learner.collect_from_message(1, 5, "завали ебало даун")
    assert await learner.maybe_promote() == 0
    assert gw.calls == 0


def test_style_sampling_n():
    from src.context.prompts import PromptBuilder

    builder = PromptBuilder("prompts")
    examples = [f"пример номер {i}" for i in range(20)]
    res = builder.build_system_instruction("ping", bot_name="B", now_str="", style_examples=examples, style_examples_n=8)
    assert res.count("- пример номер") == 8
    # без n — все как раньше, детерминированно
    res_all = builder.build_system_instruction("ping", bot_name="B", now_str="", style_examples=examples)
    assert res_all.count("- пример номер") == 20
