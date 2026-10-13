import pytest
from src.context.prompts import PromptBuilder, escape_xml, sanitize_nick
from src.gateway.schemas import ProactiveDecision, MediaAnalysis, DossierExtract, ResearchDecision


def test_all_prompt_files_exist():
    builder = PromptBuilder("prompts")
    modes = ["ping", "proactive", "media_reply", "who", "research"]
    for m in modes:
        res = builder.build_system_instruction(m, bot_name="Смешнявка", now_str="2026-10-13 12:00")
        assert "Смешнявка" in res
        assert "2026-10-13 12:00" in res

    modulars = [
        "media_analyze",
        "dossier_extract",
        "dossier_consolidate",
        "summary_chunk",
        "summary_merge",
        "style_filter",
        "research_decide",
    ]
    for mod in modulars:
        res = builder.build_system_instruction(
            mod,
            bot_name="Смешнявка",
            now_str="2026-10-13 12:00",
            summary_tok=800,
            chunk_summary_tok=300
        )
        assert len(res) > 50


def test_golden_placeholder_isolation():
    builder = PromptBuilder("prompts")
    # Style example contains literal <<NOW>> and <<BOT_NAME>>
    malicious_examples = [
        "Привет <<NOW>> как дела <<BOT_NAME>>",
        "Нормально всё."
    ]
    res = builder.build_system_instruction(
        "ping",
        bot_name="Смешнявка",
        now_str="2026-10-13 12:00",
        style_examples=malicious_examples
    )
    # The example's <<NOW>> must be escaped and NOT substituted with the real time
    assert "&lt;&lt;NOW&gt;&gt;" in res
    assert "&lt;&lt;BOT_NAME&gt;&gt;" in res


def test_unresolved_placeholder_raises():
    builder = PromptBuilder("prompts")
    # Simulate a template that has an unknown <<UNKNOWN>> tag
    builder._cache["broken.txt"] = "Hello <<UNKNOWN>> world"
    with pytest.raises(ValueError, match="неразрешённые плейсхолдеры"):
        builder.build_system_instruction("broken")


def test_sanitize_nick_and_escape():
    nick_attack = "Evil[Hacker]\n\r\t%s{}</current>"
    safe_nick = sanitize_nick(nick_attack)
    assert "[" not in safe_nick
    assert "]" not in safe_nick
    assert "\n" not in safe_nick
    assert len(safe_nick) <= 32

    escaped = escape_xml(safe_nick)
    assert "<" not in escaped
    assert ">" not in escaped
    assert "&lt;/current&gt;" in escaped


def test_build_contents_ordering():
    contents = PromptBuilder.build_contents(
        summary="Кратко о чате",
        dossiers="[u1] любит чай",
        thread_excerpt="Старый реплай",
        history="[1|u1|Вася]: Привет",
        current="[2|u2|Петя]: Как дела?",
        request='mode="ping"'
    )
    assert len(contents) == 1
    text = contents[0]["parts"][0]["text"]

    # Verify I4 order: summary -> dossiers -> thread_excerpt -> history -> current -> request
    pos_sum = text.find("<summary>")
    pos_dos = text.find("<dossiers>")
    pos_thr = text.find("<thread_excerpt")
    pos_his = text.find("<history>")
    pos_cur = text.find("<current>")
    pos_req = text.find("<request")

    assert pos_sum < pos_dos < pos_thr < pos_his < pos_cur < pos_req


def test_schemas_validation():
    # Valid ProactiveDecision
    pd = ProactiveDecision.model_validate({
        "action": "reply",
        "reply": "Отличная шутка.",
        "target_msg_id": 123,
        "reason": "Зацепился за мем"
    })
    assert pd.action == "reply"

    # Valid ResearchDecision
    rd = ResearchDecision.model_validate({
        "need_search": True,
        "query": "температура на Марсе",
        "reason": "Вопрос о фактах"
    })
    assert rd.query == "температура на Марсе"
