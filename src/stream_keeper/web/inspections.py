from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..models import LiveInfo

MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class InspectionHandoff:
    url: str
    quality: str
    info: LiveInfo
    expires_at: float


class InspectionHandoffStore:
    """Short-lived, one-time handoff from room inspection to task startup.

    Full stream metadata stays in server memory. The browser receives only an
    opaque token, which can be consumed once for the exact normalized room URL
    and requested quality that produced it.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        max_entries: int = 256,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("检测结果有效期必须大于 0")
        if max_entries <= 0:
            raise ValueError("检测结果缓存容量必须大于 0")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, InspectionHandoff] = {}

    def _prune(self, now: float) -> None:
        for token, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                self._entries.pop(token, None)

    def issue(self, url: str, quality: str, info: LiveInfo) -> str:
        now = self._clock()
        self._prune(now)
        while len(self._entries) >= self._max_entries:
            self._entries.pop(next(iter(self._entries)))
        token = secrets.token_urlsafe(24)
        while token in self._entries:
            token = secrets.token_urlsafe(24)
        self._entries[token] = InspectionHandoff(
            url=url,
            quality=quality,
            info=info,
            expires_at=now + self._ttl_seconds,
        )
        return token

    def consume(self, token: str | None, *, url: str, quality: str) -> LiveInfo | None:
        if not token:
            return None
        now = self._clock()
        self._prune(now)
        entry = self._entries.pop(token, None)
        if entry is None or entry.url != url or entry.quality != quality:
            return None
        return entry.info
