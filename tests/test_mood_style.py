import pytest
import yaml

from src.persona.mood import detect_mood
from src.persona.sanitizer import has_comma_glue, is_repetitive
from src.research.search import build_media_search_query
from src.style.learner import StyleLearner, sample_for_prompt, render_entry, norm_entry
from src.ops.config import BotConfig


# --- 7. Настроение чата ---

def test_mood_serious_question():
    assert detect_mood("дик, объясни как работает мост") == "serious"
    assert detect_mood("подскажи по досье, что знаешь про вована") == "serious"


def test_mood_banter_fight():
    assert detect_mood("ты еблан", ["ты хуесос", "ахаха"]) == "banter"
    assert detect_mood("ахаха", ["😂", "🤣", "ору"]) == "banter"


def test_mood_neutral_default():
    assert detect_mood("привет") == "neutral"
    assert detect_mood("скинь трек") == "neutral"
    # мат один без угара вокруг — не бантер автоматом
    assert detect_mood("бля, забыл") == "neutral"


# --- 6. Склейки через запятую ---

def test_comma_glue():
    assert has_comma_glue("трек скинул, сам-то понял че высрал") is True
    assert has_comma_glue("ну ты и кадр, иди поспи реально") is True
    assert has_comma_glue("а, ну понятно, да") is True  # две запятые — склейка
    # короткое без склейки — норм
    assert has_comma_glue("завали ебало") is False
    assert has_comma_glue("иди поспи реально") is False
    # длинное объяснение — не трогаем
    assert has_comma_glue("трек скинул, сам-то понял че высрал, " + "а" * 200) is False


# --- 1. Анти-гиперфикс ---

def test_repetitive_detects_same_punch():
    prev = ["ты треки кидаешь как с помойки подобрал"]
    assert is_repetitive("треки с помойки подобрал что ли", prev) is True
    assert is_repetitive("иди поспи реально", prev) is False
    assert is_repetitive("завали", prev) is False  # слишком коротко — не судим


# --- 2. Запрос из разбора медиа ---

def test_media_query_track():
    item = {"kind": "audio", "track_meta": {"artist": "Король и Шут", "title": "Лесник"},
            "summary": "панк", "ocr_text": None}
    q = build_media_search_query(item)
    assert q is not None and "Лесник" in q


def test_media_query_meme_ocr():
    item = {"kind": "photo", "summary": "мем про город",
            "ocr_text": "в иркутске что-то случилось народ в панике"}
    q = build_media_search_query(item)
    assert q is not None
    assert "мем" not in q.lower()
    assert "иркутске" in q.lower()


def test_media_query_none_without_media():
    assert build_media_search_query(None) is None


# --- 4. Банк с контекстом и взвешенный сэмплинг ---

def test_render_entry_with_ctx():
    e = {"text": "завали", "ctx": "ты тупой", "laughs": 3, "ts": 1}
    assert render_entry(e) == "завали (на: «ты тупой»)"
    assert render_entry({"text": "бум", "ctx": None, "laughs": 0, "ts": 0}) == "бум"


def test_norm_entry_legacy_string():
    assert norm_entry("бум") == {"text": "бум", "ctx": None, "laughs": 0, "ts": 0}
    assert norm_entry({"text": "x", "ctx": "y", "laughs": 5, "ts": 7}) == \
        {"text": "x", "ctx": "y", "laughs": 5, "ts": 7}


def test_sample_prime_first():
    bank = [
        {"text": f"обычный {i}", "ctx": None, "laughs": 0, "ts": i}
        for i in range(20)
    ] + [{"text": "прайм хит", "ctx": "на что-то", "laughs": 5, "ts": 99}]
    picks = sample_for_prompt(bank, ["бум"], 8)
    assert len(picks) == 8
    assert "бум" in picks  # пермач всегда
    assert any("прайм хит" in p for p in picks)  # прайм в приоритете


class FakeDB:
    async def is_opt_out(self, chat_id, user_id):
        return False


class FakeGateway:
    async def generate(self, **kwargs):
        from src.gateway.schemas import StyleVerdicts
        return StyleVerdicts(verdicts=[])


def _learner(tmp_path, **cfg_over):
    cfg = BotConfig()
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return StyleLearner(cfg, FakeDB(), FakeGateway(), prompts_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_collect_stores_ctx(tmp_path):
    learner = _learner(tmp_path)
    assert await learner.collect_reacted(1, 7, "завали", direct=True, ctx="ты тупой", laughs=4) is True
    data = yaml.safe_load((tmp_path / "style_examples.yaml").read_text(encoding="utf-8"))
    entry = data["examples"][0]
    assert entry["text"] == "завали"
    assert entry["ctx"] == "ты тупой"
    assert entry["laughs"] == 4


@pytest.mark.asyncio
async def test_ensure_capacity_noop_when_fits(tmp_path):
    learner = _learner(tmp_path, STYLE_EXAMPLES_MAX=128)
    assert await learner.ensure_capacity() is False


@pytest.mark.asyncio
async def test_ai_eviction_may_drop_nothing(tmp_path):
    """ИИ может не вытеснить ничего — банк пухнет, жёсткий потолок держит."""
    (tmp_path / "style_examples.yaml").write_text(
        yaml.safe_dump({"examples": [
            {"text": "хит", "ctx": None, "laughs": 5, "ts": 1},
            {"text": "второй", "ctx": None, "laughs": 0, "ts": 2},
            {"text": "третий", "ctx": None, "laughs": 0, "ts": 3},
        ], "pinned": ["бум"]}, allow_unicode=True),
        encoding="utf-8",
    )
    learner = _learner(tmp_path, STYLE_EXAMPLES_MAX=2)

    async def _empty_plan(active, proposal, max_active):
        return set()  # ИИ решил: всё живое, выкидывать нечего

    learner._plan_eviction = _empty_plan
    await learner.ensure_capacity()
    data = yaml.safe_load((tmp_path / "style_examples.yaml").read_text(encoding="utf-8"))
    texts = [e["text"] for e in data["examples"]]
    assert texts == ["хит", "второй", "третий"]  # ничего не выкинули
    assert data["pinned"] == ["бум"]


# --- 9. Алиас «дик»: точное слово, без ложных пингов ---

def test_dik_alias_exact_word():
    from src.persona.reply import PingDetector
    from src.ops.config import BotConfig
    cfg = BotConfig()
    cfg.BOT_ALIASES = ["дик", "дика", "дику", "диком", "дике"]
    det = PingDetector("dik_bot", 999, cfg)
    base = {"user_id": 1, "reply_to_user_id": None}
    assert det.is_ping({**base, "text": "дик, оцени трек"}) is True
    assert det.is_ping({**base, "text": "дика, че скажешь"}) is True
    assert det.is_ping({**base, "text": "дикий трек"}) is False
    assert det.is_ping({**base, "text": "индикатор горит"}) is False


# --- 3. Тишина без LLM ---

def test_silent_audio_item():
    from unittest.mock import MagicMock
    from src.media.analyzer import MediaAnalyzer
    analyzer = MediaAnalyzer(bot_config=MagicMock(), db_manager=MagicMock(),
                             gateway=MagicMock(), prompt_builder=MagicMock())
    item = analyzer._silent_audio_item("uniq1", -91.0)
    assert item["score100"] == 0
    assert item["kind"] == "audio"
    assert "тишина" in item["summary"]

    # скор 0 проходит в ответ (не режется как null)
    out = analyzer.verify_and_patch_reply_score("полная тишина", {**item, "score100": 0})
    assert "0/100" in out
