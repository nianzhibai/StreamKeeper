from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..client import DouyinClient
from ..recorder import Recorder, RecorderOptions
from ..settings import Settings
from .schemas import TaskRecord, TaskStatus
from .store import TaskStore

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], DouyinClient]
RecorderFactory = Callable[[RecorderOptions], Recorder]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


class TaskScheduler:
    """Owns all background monitor and FFmpeg tasks for one server process."""

    def __init__(
        self,
        store: TaskStore,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        recorder_factory: RecorderFactory | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._client_factory = client_factory or settings.create_client
        self._recorder_factory = recorder_factory or Recorder
        self._recording_slots = asyncio.Semaphore(settings.max_concurrent_recordings)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._recorders: dict[str, Recorder] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    @property
    def active_task_count(self) -> int:
        return sum(not task.done() for task in self._workers.values())

    @property
    def recording_task_count(self) -> int:
        return len(self._recorders)

    def recording_output_directories(self) -> set[Path]:
        directories: set[Path] = set()
        for recorder in self._recorders.values():
            output_path = getattr(recorder, "current_output_path", None)
            if output_path is not None:
                directories.add(Path(output_path).expanduser().resolve().parent)
        return directories

    async def startup(self) -> None:
        self._shutting_down = False
        await self.store.recover_interrupted()
        for record in await self.store.list_enabled():
            await self.start(record.id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def start(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            record = await self.store.get(task_id)
            if record is None:
                return None
            existing = self._workers.get(task_id)
            if existing and not existing.done():
                return record
            if existing:
                self._workers.pop(task_id, None)
            record = await self.store.update_runtime(
                task_id,
                enabled=True,
                status=TaskStatus.WAITING,
                status_message="任务已启动，等待检查",
            )
            self._workers[task_id] = asyncio.create_task(
                self._run_worker(task_id),
                name=f"douyin-recording-{task_id}",
            )
            return record

    async def stop(self, task_id: str, *, disable: bool = True) -> TaskRecord | None:
        record = await self.store.get(task_id)
        if record is None:
            return None
        if disable:
            await self.store.update_runtime(task_id, enabled=False)
        async with self._lock:
            worker = self._workers.get(task_id)
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if disable:
            return await self.store.update_runtime(
                task_id,
                status=TaskStatus.STOPPED,
                status_message="任务已停止",
                is_live=False,
            )
        return await self.store.get(task_id)

    async def restart(self, task_id: str) -> TaskRecord | None:
        record = await self.store.get(task_id)
        if record is None:
            return None
        should_restart = record.enabled
        await self.stop(task_id, disable=not should_restart)
        return await self.start(task_id) if should_restart else await self.store.get(task_id)

    async def _record_failure(self, record: TaskRecord, exc: BaseException) -> bool:
        await self.store.update_runtime(
            record.id,
            status=TaskStatus.ERROR,
            status_message=_error_message(exc),
            is_live=False,
        )
        if record.monitor:
            logger.warning("任务 %s 失败，%s 秒后重试: %s", record.id, record.interval_seconds, exc)
            await asyncio.sleep(record.interval_seconds)
            return True
        await self.store.update_runtime(record.id, enabled=False)
        return False

    async def _run_worker(self, task_id: str) -> None:
        current_worker = asyncio.current_task()
        try:
            client = self._client_factory()
            while not self._shutting_down:
                record = await self.store.get(task_id)
                if record is None or not record.enabled:
                    return

                await self.store.update_runtime(
                    task_id,
                    status=TaskStatus.CHECKING,
                    status_message="正在检查直播状态",
                    last_checked_at=_now(),
                )
                try:
                    info = await asyncio.wait_for(
                        client.fetch(record.url, record.quality),
                        timeout=self.settings.fetch_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not await self._record_failure(record, exc):
                        return
                    continue

                await self.store.update_runtime(
                    task_id,
                    anchor_name=info.anchor_name,
                    live_title=info.title,
                    is_live=info.is_live,
                    last_checked_at=_now(),
                )
                if not info.is_live:
                    if not record.monitor:
                        await self.store.update_runtime(
                            task_id,
                            enabled=False,
                            status=TaskStatus.STOPPED,
                            status_message="当前未开播，单次任务已结束",
                        )
                        return
                    await self.store.update_runtime(
                        task_id,
                        status=TaskStatus.WAITING,
                        status_message=f"未开播，{record.interval_seconds} 秒后重试",
                    )
                    await asyncio.sleep(record.interval_seconds)
                    continue

                await self.store.update_runtime(
                    task_id,
                    status=TaskStatus.QUEUED,
                    status_message="已开播，等待录制空位",
                )
                try:
                    async with self._recording_slots:
                        latest = await self.store.get(task_id)
                        if latest is None or not latest.enabled:
                            return
                        options = RecorderOptions(
                            output_dir=self.settings.recordings_dir,
                            output_format=latest.output_format,
                            source=latest.source,
                            segment_seconds=latest.segment_seconds,
                            segment_count=latest.segment_count,
                            proxy=self.settings.proxy,
                            ffmpeg=self.settings.ffmpeg,
                        )
                        recorder = self._recorder_factory(options)
                        self._recorders[task_id] = recorder
                        await self.store.update_runtime(
                            task_id,
                            status=TaskStatus.RECORDING,
                            status_message="正在录制",
                            started_at=_now(),
                        )
                        try:
                            result = await recorder.record(info)
                        finally:
                            self._recorders.pop(task_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not await self._record_failure(record, exc):
                        return
                    continue

                if latest.segment_count:
                    status_message = (
                        f"已录制 {latest.segment_count} 段，任务自动停止"
                        if result.limit_reached
                        else f"直播已结束，未满 {latest.segment_count} 段，任务自动停止"
                    )
                    await self.store.update_runtime(
                        task_id,
                        enabled=False,
                        status=TaskStatus.STOPPED,
                        status_message=status_message,
                        is_live=False,
                        output_path=result.output_path,
                    )
                    return

                if not record.monitor:
                    await self.store.update_runtime(
                        task_id,
                        enabled=False,
                        status=TaskStatus.STOPPED,
                        status_message="录制完成，单次任务已结束",
                        is_live=False,
                        output_path=result.output_path,
                    )
                    return

                await self.store.update_runtime(
                    task_id,
                    status=TaskStatus.WAITING,
                    status_message=f"直播流结束，{record.interval_seconds} 秒后重新检查",
                    is_live=False,
                    output_path=result.output_path,
                )
                await asyncio.sleep(record.interval_seconds)
        except asyncio.CancelledError:
            record = await self.store.get(task_id)
            if record is not None:
                await self.store.update_runtime(
                    task_id,
                    status=TaskStatus.WAITING if record.enabled else TaskStatus.STOPPED,
                    status_message="服务停止，等待下次启动恢复" if record.enabled else "任务已停止",
                    is_live=False,
                )
            raise
        except Exception as exc:
            logger.exception("任务 %s 的调度器异常退出", task_id)
            record = await self.store.get(task_id)
            if record is not None:
                await self.store.update_runtime(
                    task_id,
                    enabled=False,
                    status=TaskStatus.ERROR,
                    status_message=f"调度器异常: {_error_message(exc)}",
                    is_live=False,
                )
        finally:
            self._recorders.pop(task_id, None)
            async with self._lock:
                if self._workers.get(task_id) is current_worker:
                    self._workers.pop(task_id, None)
