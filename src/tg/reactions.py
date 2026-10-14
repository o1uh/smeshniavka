"""Смеяки как сигнал качества: 4+ distinct-пользователей со смеющимися
реакциями на сообщении — прайм-кандидат в банк стиля.

Учёт ведём сами (in-memory): апдейт message_reaction несёт изменение
одного юзера, а не итоги по сообщению. Порог — по distinct-юзерам со
смеякой, снятие реакции вычитает. Срабатывание — один раз на сообщение.
"""
from collections import OrderedDict
from typing import Optional


class ReactionsTracker:
    """Считает смеющиеся реакции по сообщениям, отдаёт прайм-сигнал."""

    def __init__(
        self,
        laugh_emojis: Optional[set[str]] = None,
        threshold: int = 4,
        max_tracked: int = 1000,
    ):
        self.laugh = set(laugh_emojis) if laugh_emojis else {"😁", "😂", "🤣", "😆", "😹", "🤭"}
        self.threshold = max(1, threshold)
        self.max_tracked = max(100, max_tracked)
        # (chat_id, message_id) -> {user_id: set(emoji)}
        self._tally: OrderedDict[tuple[int, int], dict[int, set[str]]] = OrderedDict()
        self._primed: set[tuple[int, int]] = set()

    def laugh_count(self, chat_id: int, message_id: int) -> int:
        users = self._tally.get((chat_id, message_id), {})
        return sum(1 for emojis in users.values() if emojis & self.laugh)

    def handle(
        self,
        chat_id: int,
        message_id: int,
        user_id: Optional[int],
        emojis: set[str],
    ) -> bool:
        """Применить изменение реакций юзера. True — только что набрался порог."""
        if user_id is None:
            return False  # анонимку не считаем (не дедуплицировать)
        key = (chat_id, message_id)
        users = self._tally.get(key)
        if users is None:
            users = {}
            self._tally[key] = users
            if len(self._tally) > self.max_tracked:
                old_key, _ = self._tally.popitem(last=False)
                self._primed.discard(old_key)
        else:
            self._tally.move_to_end(key)
        if emojis:
            users[user_id] = set(emojis)
        else:
            users.pop(user_id, None)
        if not users:
            self._tally.pop(key, None)
        if key in self._primed:
            return False
        if self.laugh_count(chat_id, message_id) >= self.threshold:
            self._primed.add(key)
            return True
        return False
