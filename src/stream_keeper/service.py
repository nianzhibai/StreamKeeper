from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from .errors import FFmpegRecordingError, LiveFetchError, SourceUnavailableError
from .models import LiveInfo, RecordingResult
from .recorder import Recorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceResult:
    live_info: LiveInfo
    recording: RecordingResult | None = None


class LiveInfoClient(Protocol):
    async def fetch(self, url: str, quality: str = "OD") -> LiveInfo: ...


class RecordingService:
    def __init__(self, client: LiveInfoClient, recorder: Recorder) -> None:
        self.client = client
        self.recorder = recorder

    async def run(
        self,
        url: str,
        *,
        quality: str = "OD",
        monitor: bool = False,
        interval: float = 180.0,
    ) -> ServiceResult:
        if interval <= 0:
            raise ValueError("interval 必须大于 0")

        while True:
            try:
                info = await self.client.fetch(url, quality)
            except LiveFetchError:
                if not monitor:
                    raise
                logger.exception("检查直播状态失败，%.0f 秒后重试", interval)
                await asyncio.sleep(interval)
                continue

            if not info.is_live:
                logger.info("主播 %s 当前未开播", info.anchor_name or "未知")
                if not monitor:
                    return ServiceResult(info)
                logger.info("%.0f 秒后再次检查", interval)
                await asyncio.sleep(interval)
                continue

            try:
                recording = await self.recorder.record(info)
            except (FFmpegRecordingError, SourceUnavailableError):
                if not monitor:
                    raise
                logger.exception("本次录制异常结束，%.0f 秒后重新检查", interval)
                await asyncio.sleep(interval)
                continue

            if not monitor:
                return ServiceResult(info, recording)

            logger.info("直播流已结束，%.0f 秒后重新检查", interval)
            await asyncio.sleep(interval)
