from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

LATEST_RELEASE_URL = "https://api.github.com/repos/nianzhibai/StreamKeeper/releases/latest"
RELEASE_PAGE_PREFIX = "https://github.com/nianzhibai/StreamKeeper/releases/tag/"
_VERSION_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateCheckError(Exception):
    """Raised when the latest stable release cannot be determined safely."""


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str
    checked_at: datetime


def _version_tuple(value: str, *, label: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value if value.startswith("v") else f"v{value}")
    if match is None:
        raise UpdateCheckError(f"{label}版本号格式无效")
    return tuple(int(part) for part in match.groups())


class UpdateChecker:
    def __init__(
        self,
        current_version: str,
        *,
        cache_seconds: float = 300,
        timeout_seconds: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.current_version = current_version
        self.cache_seconds = cache_seconds
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._cache: tuple[float, UpdateCheckResult] | None = None
        self._lock = asyncio.Lock()

    async def check(self) -> UpdateCheckResult:
        now = time.monotonic()
        if self._cache is not None and now - self._cache[0] < self.cache_seconds:
            return self._cache[1]

        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            result = await self._fetch()
            self._cache = (now, result)
            return result

    async def _fetch(self) -> UpdateCheckResult:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"StreamKeeper/{self.current_version}",
                },
            ) as client:
                response = await client.get(LATEST_RELEASE_URL)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpdateCheckError("无法连接 GitHub 检查更新") from exc

        tag_name = payload.get("tag_name") if isinstance(payload, dict) else None
        if not isinstance(tag_name, str):
            raise UpdateCheckError("GitHub Release 缺少版本号")
        current = _version_tuple(self.current_version, label="当前")
        latest = _version_tuple(tag_name, label="最新")
        latest_version = tag_name.removeprefix("v")
        return UpdateCheckResult(
            current_version=self.current_version,
            latest_version=latest_version,
            update_available=latest > current,
            release_url=f"{RELEASE_PAGE_PREFIX}{tag_name}",
            checked_at=datetime.now(timezone.utc),
        )
