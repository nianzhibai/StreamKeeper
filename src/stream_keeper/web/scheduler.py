from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..platforms import LiveStreamClient
from ..recorder import Recorder, RecorderOptions
from ..settings import Settings
from .events import EventLog
from .schemas import TaskRecord, TaskStatus
from .store import TaskStore

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], LiveStreamClient]
RecorderFactory = Callable[[RecorderOptions], Recorder]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


def _display_name(label: str | None, anchor_name: str | None, url: str) -> str:
    return (label or anchor_name or url)[:60]


def _task_name(record: TaskRecord) -> str:
    return _display_name(record.label, record.anchor_name, record.url)


def _duration_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, remainder = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分 {remainder} 秒"
    return f"{remainder} 秒"


class TaskScheduler:
    """Owns all background monitor and FFmpeg tasks for one server process."""

    def __init__(
        self,
        store: TaskStore,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        recorder_factory: RecorderFactory | None = None,
        events: EventLog | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.events = events or EventLog(store)
        self._client_factory = client_factory or settings.create_client
        self._recorder_factory = recorder_factory or Recorder
        self._recording_slots = asyncio.Semaphore(settings.max_concurrent_recordings)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._recorders: dict[str, Recorder] = {}
        # Tasks whose last check failed, so a stuck room reports once instead of
        # once per retry interval.
        self._failing: set[str] = set()
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

    def enrich_record(self, record: TaskRecord) -> TaskRecord:
        if record.status != TaskStatus.RECORDING:
            return record
        recorder = self._recorders.get(record.id)
        elapsed = float(getattr(recorder, "progress_seconds", 0.0) or 0.0) if recorder else 0.0
        if elapsed <= 0 and record.started_at is not None:
            started = record.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (_now() - started).total_seconds())

        segment_index: int | None = None
        segment_progress: float | None = None
        if record.segment_seconds > 0:
            segment_index = int(elapsed // record.segment_seconds) + 1
            if record.segment_count > 0:
                segment_index = min(segment_index, record.segment_count)
            segment_elapsed = elapsed % record.segment_seconds
            segment_progress = min(1.0, segment_elapsed / record.segment_seconds)
        return record.model_copy(
            update={
                "recording_elapsed_seconds": round(elapsed, 1),
                "recording_segment_index": segment_index,
                "recording_segment_progress": (None if segment_progress is None else round(segment_progress, 4)),
            }
        )

    def enrich_records(self, records: list[TaskRecord]) -> list[TaskRecord]:
        return [self.enrich_record(record) for record in records]

    async def startup(self) -> None:
        self._shutting_down = False
        await self.store.recover_interrupted()
        restored = await self.store.list_enabled()
        for record in restored:
            # One summary event reads better than one line per restored task.
            await self.start(record.id, announce=False)
        if restored:
            await self.events.info(
                "task",
                f"服务启动后已恢复 {len(restored)} 个值守任务",
                "、".join(_task_name(record) for record in restored),
            )

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def start(self, task_id: str, *, announce: bool = True) -> TaskRecord | None:
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
                name=f"stream-recording-{task_id}",
            )
        if announce and record is not None:
            detail = (
                f"每 {record.interval_seconds} 秒检查一次开播状态"
                if record.monitor
                else "仅检查一次，录制结束后自动停止"
            )
            await self.events.info("task", f"「{_task_name(record)}」已启动", detail, task_id=record.id)
        return record

    async def stop(self, task_id: str, *, disable: bool = True, announce: bool = True) -> TaskRecord | None:
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
        if not disable:
            return await self.store.get(task_id)
        if announce:
            await self.events.info("task", f"「{_task_name(record)}」已停止", task_id=record.id)
        return await self.store.update_runtime(
            task_id,
            status=TaskStatus.STOPPED,
            status_message="任务已停止",
            is_live=False,
        )

    async def restart(self, task_id: str) -> TaskRecord | None:
        record = await self.store.get(task_id)
        if record is None:
            return None
        should_restart = record.enabled
        await self.stop(task_id, disable=not should_restart, announce=False)
        if not should_restart:
            return await self.store.get(task_id)
        await self.events.info(
            "task",
            f"「{_task_name(record)}」配置已更新，正在按新配置重新录制",
            task_id=record.id,
        )
        return await self.start(task_id, announce=False)

    async def _record_failure(self, record: TaskRecord, exc: BaseException, *, stage: str) -> bool:
        message = _error_message(exc)
        if record.id not in self._failing:
            self._failing.add(record.id)
            retry = f"，将每 {record.interval_seconds} 秒重试" if record.monitor else "，任务已停止"
            await self.events.error("task", f"「{_task_name(record)}」{stage}失败{retry}", message, task_id=record.id)
        await self.store.update_runtime(
            record.id,
            status=TaskStatus.ERROR,
            status_message=message,
            is_live=False,
        )
        if record.monitor:
            logger.warning("任务 %s 失败，%s 秒后重试: %s", record.id, record.interval_seconds, exc)
            await asyncio.sleep(record.interval_seconds)
            return True
        await self.store.update_runtime(record.id, enabled=False)
        return False

    async def _clear_failure(self, record: TaskRecord) -> None:
        if record.id in self._failing:
            self._failing.discard(record.id)
            await self.events.success("task", f"「{_task_name(record)}」已恢复正常", task_id=record.id)

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
                    if not await self._record_failure(record, exc, stage="检查开播状态"):
                        return
                    continue

                await self.store.update_runtime(
                    task_id,
                    anchor_name=info.anchor_name,
                    live_title=info.title,
                    is_live=info.is_live,
                    last_checked_at=_now(),
                )
                await self._clear_failure(record)
                name = _display_name(record.label, info.anchor_name, record.url)
                if not info.is_live:
                    if not record.monitor:
                        # The final status is what every watcher polls on, so the log
                        # entry lands first: a shutdown racing the last write would
                        # otherwise cancel the worker before the event is stored.
                        await self.events.info("task", f"「{name}」当前未开播，单次任务已结束", task_id=task_id)
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
                        started_at = _now()
                        await self.store.update_runtime(
                            task_id,
                            status=TaskStatus.RECORDING,
                            status_message="正在录制",
                            started_at=started_at,
                        )
                        await self.events.success(
                            "task",
                            f"「{name}」已开播，开始录制",
                            " · ".join(
                                part
                                for part in (info.title, f"清晰度 {latest.quality}", latest.output_format.upper())
                                if part
                            ),
                            task_id=task_id,
                        )
                        try:
                            result = await recorder.record(info)
                        finally:
                            self._recorders.pop(task_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not await self._record_failure(record, exc, stage="录制"):
                        return
                    continue

                recorded = f"本次录制 {_duration_text((_now() - started_at).total_seconds())}"
                # TaskConfig makes a finite cap a one-shot task.  Keep the
                # explicit branch for its user-facing completion message and
                # as a defensive final boundary for persisted legacy rows.
                if latest.segment_count:
                    status_message = (
                        f"已录制 {latest.segment_count} 段，任务自动停止"
                        if result.limit_reached
                        else f"直播已结束，未满 {latest.segment_count} 段，任务自动停止"
                    )
                    await self.events.success("task", f"「{name}」{status_message}", recorded, task_id=task_id)
                    await self.store.update_runtime(
                        task_id,
                        enabled=False,
                        status=TaskStatus.STOPPED,
                        status_message=status_message,
                        is_live=False,
                        output_path=result.output_path,
                    )
                    return

                if not latest.monitor:
                    await self.events.success("task", f"「{name}」录制完成，单次任务已结束", recorded, task_id=task_id)
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
                    status_message=f"直播流结束，{latest.interval_seconds} 秒后重新检查",
                    is_live=False,
                    output_path=result.output_path,
                )
                await self.events.success(
                    "task",
                    f"「{name}」直播结束，本次录制完成",
                    f"{recorded}，{latest.interval_seconds} 秒后重新检查是否开播",
                    task_id=task_id,
                )
                await asyncio.sleep(latest.interval_seconds)
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
                await self.events.error(
                    "task",
                    f"「{_task_name(record)}」调度异常退出，任务已停止",
                    _error_message(exc),
                    task_id=task_id,
                )
        finally:
            self._failing.discard(task_id)
            self._recorders.pop(task_id, None)
            async with self._lock:
                if self._workers.get(task_id) is current_worker:
                    self._workers.pop(task_id, None)
