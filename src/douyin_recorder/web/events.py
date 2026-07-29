from __future__ import annotations

import logging

from .schemas import EventCategory, EventLevel
from .store import TaskStore

logger = logging.getLogger(__name__)

# One week of a busy server still fits, and the trim on write keeps SQLite small.
EVENT_RETENTION = 1000
MESSAGE_MAX_LENGTH = 200
DETAIL_MAX_LENGTH = 500


def clean_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(str(value).split())
    return collapsed[:limit] or None


class EventLog:
    """The curated activity log shown on the Web UI.

    Only events that tell the operator whether recording still works belong
    here — routine polling, FFmpeg chatter and HTTP noise stay in the Python
    log, so a glance at the page is enough to judge the service.
    """

    def __init__(self, store: TaskStore, *, retention: int = EVENT_RETENTION) -> None:
        self.store = store
        self.retention = retention

    async def record(
        self,
        category: EventCategory,
        level: EventLevel,
        message: str,
        detail: str | None = None,
    ) -> None:
        text = clean_text(message, MESSAGE_MAX_LENGTH)
        if text is None:
            return
        try:
            await self.store.append_event(
                category,
                level,
                text,
                clean_text(detail, DETAIL_MAX_LENGTH),
                retention=self.retention,
            )
        except Exception:  # A log entry must never take down the work it describes.
            logger.warning("写入运行事件失败：%s", text, exc_info=True)

    async def info(self, category: EventCategory, message: str, detail: str | None = None) -> None:
        await self.record(category, "info", message, detail)

    async def success(self, category: EventCategory, message: str, detail: str | None = None) -> None:
        await self.record(category, "success", message, detail)

    async def warning(self, category: EventCategory, message: str, detail: str | None = None) -> None:
        await self.record(category, "warning", message, detail)

    async def error(self, category: EventCategory, message: str, detail: str | None = None) -> None:
        await self.record(category, "error", message, detail)
