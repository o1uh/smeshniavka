"""Lane worker pool: chat / track x2 / media / sys.

Зачем: один медленный разбор гифки/видео не должен тормозить обычное
общение. Каждый лейн — отдельная FIFO-очередь:
- chat  — дефолтное общение (текст, proactive, who),
- track — только музыкальные треки (audio), ДВА воркера (треки идут
  пачками, один consumer захлебывается),
- media — все остальные медиа (photo/video/voice/video_note/animation/sticker),
- sys   — фон (досье, обучение банка стиля, чистки).

Переполнение: выкидываем самое старое, но никогда не дропаем текущий
обрабатываемый элемент. Бэкпрешер виден через qsize() в логах.
"""
import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from src.ops.log import logger

LANE_CHAT = "chat"
LANE_TRACK = "track"
LANE_MEDIA = "media"
LANE_SYS = "sys"

ALL_LANES = (LANE_CHAT, LANE_TRACK, LANE_MEDIA, LANE_SYS)

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
    """Воркеры: chat / track x2 / media / sys."""

    chat: LaneWorker = field(default_factory=lambda: LaneWorker(LANE_CHAT))
    track: LaneWorker = field(default_factory=lambda: LaneWorker(f"{LANE_TRACK}1"))
    track2: LaneWorker = field(default_factory=lambda: LaneWorker(f"{LANE_TRACK}2"))
    media: LaneWorker = field(default_factory=lambda: LaneWorker(LANE_MEDIA))
    sys: LaneWorker = field(default_factory=lambda: LaneWorker(LANE_SYS))

    # round-robin по трековым воркерам (итeрируется только при submit)
    _track_cycle: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._track_cycle = itertools.cycle((self.track, self.track2))

    def _next_track(self) -> LaneWorker:
        assert self._track_cycle is not None
        return next(self._track_cycle)

    def start_all(self) -> None:
        self.chat.start()
        self.track.start()
        self.track2.start()
        self.media.start()
        self.sys.start()

    async def stop_all(self) -> None:
        await self.chat.stop()
        await self.track.stop()
        await self.track2.stop()
        await self.media.stop()
        await self.sys.stop()

    def get(self, lane: str) -> LaneWorker:
        if lane == LANE_TRACK:
            return self._next_track()
        if lane == LANE_MEDIA:
            return self.media
        if lane == LANE_SYS:
            return self.sys
        return self.chat

    def sizes(self) -> dict[str, Any]:
        return {
            LANE_CHAT: self.chat.qsize(),
            LANE_TRACK: self.track.qsize() + self.track2.qsize(),
            LANE_MEDIA: self.media.qsize(),
            LANE_SYS: self.sys.qsize(),
        }
