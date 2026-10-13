from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


class Clock(ABC):
    @abstractmethod
    def now_utc(self) -> datetime:
        pass

    @abstractmethod
    def now_tz(self, tz_name: str) -> datetime:
        pass

    @abstractmethod
    def time(self) -> float:
        pass


class SystemClock(Clock):
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_tz(self, tz_name: str) -> datetime:
        return datetime.now(ZoneInfo(tz_name))

    def time(self) -> float:
        return datetime.now(timezone.utc).timestamp()


class FakeClock(Clock):
    def __init__(self, current_dt: Optional[datetime] = None):
        self._dt = current_dt or datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

    def set_datetime(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._dt = dt

    def advance(self, seconds: float) -> None:
        self._dt = datetime.fromtimestamp(self._dt.timestamp() + seconds, tz=timezone.utc)

    def now_utc(self) -> datetime:
        return self._dt

    def now_tz(self, tz_name: str) -> datetime:
        return self._dt.astimezone(ZoneInfo(tz_name))

    def time(self) -> float:
        return self._dt.timestamp()


class TgSender(ABC):
    @abstractmethod
    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "HTML",
        disable_web_page_preview: bool = True
    ) -> Optional[int]:
        """Returns sent message ID or None on failure."""
        pass

    @abstractmethod
    async def leave_chat(self, chat_id: int) -> bool:
        pass

    @abstractmethod
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        pass


class FakeSender(TgSender):
    def __init__(self):
        self.sent_messages: list[dict[str, Any]] = []
        self.left_chats: list[int] = []
        self.actions: list[dict[str, Any]] = []
        self.next_message_id: int = 1000
        self.fail_send: bool = False
        self.fail_entities: bool = False

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "HTML",
        disable_web_page_preview: bool = True
    ) -> Optional[int]:
        if self.fail_send:
            raise RuntimeError("FakeSender simulated failure")
        if self.fail_entities and parse_mode == "HTML":
            raise ValueError("Bad Request: can't parse entities in message")

        msg_id = self.next_message_id
        self.next_message_id += 1
        self.sent_messages.append({
            "message_id": msg_id,
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "parse_mode": parse_mode
        })
        return msg_id

    async def leave_chat(self, chat_id: int) -> bool:
        self.left_chats.append(chat_id)
        return True

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append({"chat_id": chat_id, "action": action})


class Gateway(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt_name: str,
        contents: list[dict[str, Any]],
        system_instruction: Optional[str] = None,
        priority: str = "P0",
        response_schema: Optional[type] = None,
        estimated_tokens: int = 1000
    ) -> Any:
        pass
