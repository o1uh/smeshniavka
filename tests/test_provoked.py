from src.antijob.detector import ProvocationDetector as P


def test_direct_insult_reply_to_bot():
    assert P.is_provoked("Ты дочь бляди", is_reply_to_bot=True) is True
    assert P.is_provoked("ебаная тварь", is_reply_to_bot=True) is True
    assert P.is_provoked("А ещё 2 уебища где ты забыл", is_reply_to_bot=True) is True


def test_bait_patterns():
    assert P.is_provoked("а каких 2 уебищ он имеет в виду м?", is_reply_to_bot=True) is True
    assert P.is_provoked("Ты как цитатник с вк отвечаешь где ответная агрессия", is_reply_to_bot=True) is True
    assert P.is_provoked("че, аргументы кончились, на мамок перешел?", is_reply_to_bot=True) is True


def test_mention_counts_as_directed():
    assert P.is_provoked("смешнявка ты мразь", mentions_bot=True) is True
    assert P.is_provoked("@smaxshnyavka_bot ты дно", mentions_bot=True) is True


def test_legit_requests_not_provoked():
    assert P.is_provoked("сводку дай на каждого щас", is_reply_to_bot=False) is False
    assert P.is_provoked("в чём мем", is_reply_to_bot=True) is False
    assert P.is_provoked("оцени трек", is_reply_to_bot=True) is False
    assert P.is_provoked("привет как дела", is_reply_to_bot=True) is False


def test_third_party_banter_not_provoked():
    # подъёбы между участниками без обращения к боту — дефолт-режим
    assert P.is_provoked("вован ты конечно кадр", is_reply_to_bot=False) is False
    assert P.is_provoked("ну ты и клоун", is_reply_to_bot=False) is False


def test_provoked_reply_ok():
    assert P.provoked_reply_ok("закройся и сядь") is True
    assert P.provoked_reply_ok("ЗАВАЛИ УЖЕ ЕБАЛО ХУЕСОС") is True
    assert P.provoked_reply_ok("вован треки гоняет и мост ковыряет, кава стикерами спамит, а ты сидишь сопли жуешь") is False
    assert P.provoked_reply_ok("че, аргументы кончились? слабовато скулишь") is False
    assert P.provoked_reply_ok("x" * 121) is False
    assert P.provoked_reply_ok("") is False


def test_provoked_examples_pass_outgoing_guard():
    """Примеры отпора из промпта не должны резаться гардом, иначе будет вечный repair."""
    from src.persona.sanitizer import validate_persona_reply

    examples = [
        "я тя ща по ip въебу даун",
        "сукаааааааааа как же ты заебалллл",
        "ЗАВАЛИ УЖЕ ЕБАЛО ХУЕСОС",
        "ты в списке, жди",
    ]
    for ex in examples:
        ok, reason = validate_persona_reply(ex, is_media=False)
        assert ok, f"{ex!r} rejected: {reason}"
        assert P.provoked_reply_ok(ex) is True
