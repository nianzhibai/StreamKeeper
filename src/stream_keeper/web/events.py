from __future__ import annotations

import logging
import os

from ..settings import ENV_PREFIX
from .schemas import EventCategory, EventLevel
from .store import TaskStore

logger = logging.getLogger(__name__)


def _retention_from_env() -> int:
    """Rows kept in the log. Larger than it used to be because the page can now
    page back through history instead of only showing the newest screenful."""
    try:
        value = int(os.getenv(f"{ENV_PREFIX}EVENT_RETENTION", "5000"))
    except ValueError:
        return 5000
    return min(max(value, 100), 100_000)


EVENT_RETENTION = _retention_from_env()
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
        *,
        task_id: str | None = None,
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
                task_id=task_id,
            )
        except Exception:  # A log entry must never take down the work it describes.
            logger.warning("写入运行事件失败：%s", text, exc_info=True)

    async def info(
        self, category: EventCategory, message: str, detail: str | None = None, *, task_id: str | None = None
    ) -> None:
        await self.record(category, "info", message, detail, task_id=task_id)

    async def success(
        self, category: EventCategory, message: str, detail: str | None = None, *, task_id: str | None = None
    ) -> None:
        await self.record(category, "success", message, detail, task_id=task_id)

    async def warning(
        self, category: EventCategory, message: str, detail: str | None = None, *, task_id: str | None = None
    ) -> None:
        await self.record(category, "warning", message, detail, task_id=task_id)

    async def error(
        self, category: EventCategory, message: str, detail: str | None = None, *, task_id: str | None = None
    ) -> None:
        await self.record(category, "error", message, detail, task_id=task_id)
