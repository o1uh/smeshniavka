from src.antijob.detector import ProvocationDetector as P


def test_direct_insult_reply_to_bot():
    assert P.is_provoked("Ты дочь бляди", is_reply_to_bot=True) is True
    assert P.is_provoked("ебаная тварь", is_reply_to_bot=True) is True
    assert P.is_provoked("А ещё 2 уебища где ты забыл", is_reply_to_bot=True) is True
    # расширенное покрытие
    assert P.is_provoked("ты тупой", is_reply_to_bot=True) is True
    assert P.is_provoked("ты еблан", is_reply_to_bot=True) is True
    assert P.is_provoked("дик, иди нахуй", mentions_bot=True) is True
    assert P.is_provoked("дик завали ебало", mentions_bot=True) is True


def test_bait_patterns():
    assert P.is_provoked("а каких 2 уебищ он имеет в виду м?", is_reply_to_bot=True) is True
    assert P.is_provoked("Ты как цитатник с вк отвечаешь где ответная агрессия", is_reply_to_bot=True) is True
    assert P.is_provoked("че, аргументы кончились, на мамок перешел?", is_reply_to_bot=True) is True
    assert P.is_provoked("че молчишь, скулишь?", is_reply_to_bot=True) is True


def test_mention_counts_as_directed():
    assert P.is_provoked("смешнявка ты мразь", mentions_bot=True) is True
    assert P.is_provoked("@smaxshnyavka_bot ты дно", mentions_bot=True) is True
    assert P.is_provoked("дик ты даун", mentions_bot=True) is True


def test_tell_off_needs_directed():
    # посылы без обращения к боту — не провокация
    assert P.is_provoked("иди нахуй", is_reply_to_bot=False, mentions_bot=False) is False
    assert P.is_provoked("завали ебало", is_reply_to_bot=False, mentions_bot=False) is False
    # а в адрес бота — провокация
    assert P.is_provoked("иди нахуй", is_reply_to_bot=True) is True


def test_legit_requests_not_provoked():
    assert P.is_provoked("сводку дай на каждого щас", is_reply_to_bot=False) is False
    assert P.is_provoked("в чём мем", is_reply_to_bot=True) is False
    assert P.is_provoked("оцени трек", is_reply_to_bot=True) is False
    assert P.is_provoked("привет как дела", is_reply_to_bot=True) is False
    assert P.is_provoked("дик, оцени трек", mentions_bot=True) is False


def test_third_party_banter_not_provoked():
    # подъёбы между участниками без обращения к боту — дефолт-режим
    assert P.is_provoked("вован ты конечно кадр", is_reply_to_bot=False) is False
    assert P.is_provoked("ну ты и клоун", is_reply_to_bot=False) is False


def test_provoked_reply_ok():
    assert P.provoked_reply_ok("завали") is True
    assert P.provoked_reply_ok("чепушила заткнись") is True
    assert P.provoked_reply_ok("ЗАВАЛИ УЖЕ ЕБАЛО") is True
    # заезженные концепции запрещены
    assert P.provoked_reply_ok("я тя ща по ip въебу даун") is False
    assert P.provoked_reply_ok("я тебя вычислю") is False
    # вопросы — не отпор
    assert P.provoked_reply_ok("че, аргументы кончились? слабовато скулишь") is False
    assert P.provoked_reply_ok("ты че самый умный?") is False
    # длиннота и многословие — не отпор
    assert P.provoked_reply_ok("вован треки гоняет и мост ковыряет, кава стикерами спамит, а ты сидишь сопли жуешь") is False
    assert P.provoked_reply_ok("x" * 121) is False
    assert P.provoked_reply_ok("") is False


def test_provoked_short_replies_pass_outgoing_guard():
    """Короткий отпор не должен резаться гардом, иначе будет вечный repair."""
    from src.persona.sanitizer import validate_persona_reply

    examples = [
        "завали",
        "чепушила заткнись",
        "ЗАВАЛИ УЖЕ ЕБАЛО",
    ]
    for ex in examples:
        ok, reason = validate_persona_reply(ex, is_media=False)
        assert ok, f"{ex!r} rejected: {reason}"
        assert P.provoked_reply_ok(ex) is True
