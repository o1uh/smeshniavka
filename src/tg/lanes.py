"""Three-lane worker pool: chat / track / media.

Зачем: один медленный разбор гифки/видео не должен тормозить обычное
общение. Каждый лейн — отдельная FIFO-очередь с одним consumer'ом,
поэтому:
- chat  — дефолтное общение (текст, proactive, who),
- track — только музыкальные треки (audio),
- media — все остальные медиа (photo/video/voice/video_note/animation/sticker).

Переполнение: выкидываем самое старое, но никогда не дропаем текущий
обрабатываемый элемент. Бэкпрешер виден через qsize() в логах.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from src.ops.log import logger

LANE_CHAT = "chat"
LANE_TRACK = "track"
LANE_MEDIA = "media"

ALL_LANES = (LANE_CHAT, LANE_TRACK, LANE_MEDIA)

# audio — отдельный лейн, всё остальное с файлом — media.
TRACK_KINDS = {"audio"}
MEDIA_KINDS = {"photo", "video", "voice", "video_note", "animation", "sticker"}


def route_lane(target_kind: Optional[str]) -> str:
    """Выбирает воркер по типу вложения (уже с учётом реплая)."""
    if target_kind in TRACK_KINDS:
        return LANE_TRACK
    if target_kind in MEDIA_KINDS:
        return LANE_MEDIA
    return LANE_CHAT


@dataclass
class LaneJob:
    lane: str
    label: str
    coro_factory: Callable[[], Awaitable[None]]
    enqueued_at: float = 0.0


class LaneWorker:
    """Один consumer с FIFO-очередью."""

    def __init__(self, name: str, max_queue: int = 30):
        self.name = name
        self.max_queue = max_queue
        self.queue: asyncio.Queue[LaneJob] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.dropped = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"lane-{self.name}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def qsize(self) -> int:
        return self.queue.qsize()

    async def submit(self, job: LaneJob) -> bool:
        """Положить задачу. При переполнении — выкинуть самую старую."""
        if self.queue.qsize() >= self.max_queue:
            try:
                old = self.queue.get_nowait()
                self.dropped += 1
                logger.warning(
                    "Lane %s overflow: dropped oldest job %s (total dropped=%d)",
                    self.name, old.label, self.dropped,
                )
            except asyncio.QueueEmpty:
                pass
        await self.queue.put(job)
        return True

    async def _loop(self) -> None:
        while self._running:
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                await job.coro_factory()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Lane %s job %s failed: %s", self.name, job.label, e)
            finally:
                try:
                    self.queue.task_done()
                except ValueError:
                    pass


@dataclass
class LanePool:
    """Три воркера: chat / track / media."""

    chat: LaneWorker = field(default_factory=lambda: LaneWorker(LANE_CHAT))
    track: LaneWorker = field(default_factory=lambda: LaneWorker(LANE_TRACK))
    media: LaneWorker = field(default_factory=lambda: LaneWorker(LANE_MEDIA))

    def start_all(self) -> None:
        self.chat.start()
        self.track.start()
        self.media.start()

    async def stop_all(self) -> None:
        await self.chat.stop()
        await self.track.stop()
        await self.media.stop()

    def get(self, lane: str) -> LaneWorker:
        if lane == LANE_TRACK:
            return self.track
        if lane == LANE_MEDIA:
            return self.media
        return self.chat

    def sizes(self) -> dict[str, Any]:
        return {
            LANE_CHAT: self.chat.qsize(),
            LANE_TRACK: self.track.qsize(),
            LANE_MEDIA: self.media.qsize(),
        }
