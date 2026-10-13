import json
from pathlib import Path
from typing import Any, Optional


class RuntimeChatResolver:
    """Manages ALLOWED_CHAT_ID with support for runtime migration override from data/runtime_state.json."""

    def __init__(self, initial_chat_id: int, state_file: str = "data/runtime_state.json"):
        self.initial_chat_id = initial_chat_id
        self.state_file = Path(state_file)
        self._current_chat_id = initial_chat_id
        self._load_state()

    def _load_state(self) -> None:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if "allowed_chat_id" in data:
                    self._current_chat_id = int(data["allowed_chat_id"])
            except Exception:
                pass

    def get_allowed_chat_id(self) -> int:
        return self._current_chat_id

    def set_allowed_chat_id(self, new_id: int) -> None:
        self._current_chat_id = new_id
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"allowed_chat_id": new_id}, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def is_allowed(self, chat_id: int) -> bool:
        if self._current_chat_id == 0:
            return True  # Discover mode / not yet configured
        return self._current_chat_id == chat_id


def check_media_caps(
    media_kind: str,
    file_size_bytes: int,
    duration_seconds: Optional[int],
    limits_config: dict[str, Any]
) -> tuple[bool, Optional[str]]:
    """
    Validates file size and duration against limits.yaml BEFORE download per R7.
    Returns (is_allowed, rejection_reason).
    """
    if media_kind == "doc" or media_kind == "document":
        return False, "Документы не принимаются для анализа."

    cap = limits_config.get(media_kind, {})
    if not cap:
        return True, None

    if cap.get("allowed") is False:
        return False, f"Тип медиа '{media_kind}' запрещён для скачивания."

    max_bytes = cap.get("max_bytes")
    if max_bytes and file_size_bytes > max_bytes:
        mb = max_bytes / (1024 * 1024)
        return False, f"Файл {file_size_bytes} байт превышает лимит {mb:.1f} МБ."

    max_seconds = cap.get("max_seconds")
    if max_seconds and duration_seconds and duration_seconds > max_seconds:
        # Note: for video/video_note R7 specifies cutting to first 60s, but if hard cap is exceeded:
        if media_kind in ("voice", "animation") and duration_seconds > max_seconds:
            return False, f"Длительность {duration_seconds}с превышает лимит {max_seconds}с."

    return True, None
