from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import posixpath
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..cloud import (
    CloudArchiveConfig,
    CloudUploadClient,
    CloudUploadError,
    QuarkClient,
    UploadStage,
    WoPanClient,
)
from ..settings import Settings
from .schemas import TaskStatus
from .store import TaskStore

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset({".flv", ".mkv", ".mp4", ".ts"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
JOB_RETENTION_SECONDS = 300
MAX_JOBS = 100
SPEED_WINDOW_SECONDS = 5.0
# A rapid-upload hit jumps straight to 100%; refusing to divide by a near-zero
# span keeps that from surfacing as an absurd rate.
MIN_SPEED_SPAN_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class UploadTarget:
    name: str
    remote_root: str


@dataclass(frozen=True, slots=True)
class UploadCandidate:
    path: Path
    relative_path: Path
    size: int
    mtime_ns: int


@dataclass(slots=True)
class UploadJob:
    """Live state of one recording moving to the cloud.

    Manual uploads register the job so the Web UI can poll it; a scheduled run
    keeps an unregistered job per file purely to carry the counters, so both
    paths share one implementation.
    """

    path: str
    name: str
    size: int
    created_at: datetime
    status: str = "queued"
    stage: str = ""
    target: str = ""
    target_index: int = 0
    target_count: int = 0
    uploaded_bytes: int = 0
    uploaded_copies: int = 0
    deleted: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    monotonic: Callable[[], float] = time.monotonic
    transferred_bytes: int = 0
    samples: deque[tuple[float, int]] = field(default_factory=deque, repr=False)
    on_change: Callable[[], None] | None = field(default=None, repr=False)

    def notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def report(self, stage: UploadStage, uploaded: int) -> None:
        self.stage = stage
        value = max(0, min(uploaded, self.size))
        delta = value - self.uploaded_bytes
        self.uploaded_bytes = value
        self.notify()
        # Only the streaming stage moves real bytes: a dedup hit reports the whole
        # file at once, and a retried part rewinds instead of advancing.
        if stage != "uploading" or delta <= 0:
            return
        self.transferred_bytes += delta
        now = self.monotonic()
        self.samples.append((now, self.transferred_bytes))
        while len(self.samples) > 2 and now - self.samples[0][0] > SPEED_WINDOW_SECONDS:
            self.samples.popleft()

    @property
    def speed_bytes_per_second(self) -> int:
        if self.status != "running" or len(self.samples) < 2:
            return 0
        (first_at, first_bytes), (last_at, last_bytes) = self.samples[0], self.samples[-1]
        # Counting the idle tail decays a stalled transfer toward zero instead of
        # freezing on the last burst.
        span = self.monotonic() - first_at
        if span < MIN_SPEED_SPAN_SECONDS or self.monotonic() - last_at > SPEED_WINDOW_SECONDS:
            return 0
        return max(0, round((last_bytes - first_bytes) / span))


@dataclass(frozen=True, slots=True)
class UploadRunSummary:
    scanned_files: int = 0
    skipped_files: int = 0
    uploaded_copies: int = 0
    deleted_files: int = 0
    failed_files: int = 0


@dataclass(frozen=True, slots=True)
class UploadExecution:
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    summary: UploadRunSummary | None = None
    error: str | None = None


UploadClientFactory = Callable[
    [UploadTarget],
    CloudUploadClient | Awaitable[CloudUploadClient],
]
ActiveDirectoriesProvider = Callable[[], set[Path]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class RecordingUploadService:
    """Schedule and run native cloud uploads using persistent Web configuration."""

    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        *,
        client_factory: UploadClientFactory | None = None,
        active_directories_provider: ActiveDirectoriesProvider | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self._seed_config = CloudArchiveConfig.from_settings(settings)
        self._seed_config.validate()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._monotonic = monotonic
        self._sleep = sleep
        self._client_factory = client_factory
        self._active_directories_provider = active_directories_provider
        self._config: CloudArchiveConfig | None = None
        self._config_lock = asyncio.Lock()
        self._trigger_lock = asyncio.Lock()
        # Serializes every upload so manual jobs and scheduled runs never share
        # a cloud client or refresh the same credential concurrently.
        self._upload_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._active_run: asyncio.Task[None] | None = None
        self._last_execution: UploadExecution | None = None
        self._jobs: dict[str, UploadJob] = {}
        self._job_worker: asyncio.Task[None] | None = None
        self._active_job: tuple[UploadJob, asyncio.Task[None]] | None = None
        self._subscribers: set[asyncio.Event] = set()

    @staticmethod
    def _fingerprint(value: dict[str, object]) -> str:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(serialized).hexdigest()

    async def initialize_config(self) -> CloudArchiveConfig:
        async with self._config_lock:
            if self._config is not None:
                return self._config
            defaults = self._seed_config.to_dict()
            raw = await self.store.sync_cloud_upload_config(self._fingerprint(defaults), defaults)
            config = CloudArchiveConfig.from_dict(raw)
            config.validate()
            self._config = config
            return config

    async def get_config(self) -> CloudArchiveConfig:
        return await self.initialize_config()

    async def reconfigure(self, config: CloudArchiveConfig | None = None) -> None:
        if config is None:
            raw = await self.store.get_cloud_upload_config()
            if raw is None:
                config = await self.initialize_config()
            else:
                config = CloudArchiveConfig.from_dict(raw)
        config.validate()
        async with self._config_lock:
            self._config = config

        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        if config.enabled:
            self._runner = asyncio.create_task(self._run_forever(), name="recording-cloud-upload")

    @property
    def running(self) -> bool:
        return self._active_run is not None and not self._active_run.done()

    @property
    def last_execution(self) -> UploadExecution | None:
        return self._last_execution

    @staticmethod
    def seconds_until_next_run(now: datetime, hour: int) -> float:
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run < now:
            next_run += timedelta(days=1)
        return max(0.0, (next_run - now).total_seconds())

    async def next_run_at(self) -> datetime | None:
        config = await self.get_config()
        if not config.enabled:
            return None
        now = self._clock()
        return now + timedelta(seconds=self.seconds_until_next_run(now, config.upload_hour))

    async def startup(self) -> None:
        config = await self.initialize_config()
        if config.enabled and (self._runner is None or self._runner.done()):
            self._runner = asyncio.create_task(self._run_forever(), name="recording-cloud-upload")

    async def shutdown(self) -> None:
        job_task = self._active_job[1] if self._active_job is not None else None
        tasks = [task for task in (self._runner, self._active_run, self._job_worker, job_task) if task is not None]
        self._runner = None
        self._active_run = None
        self._job_worker = None
        self._active_job = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_forever(self) -> None:
        while True:
            config = await self.get_config()
            delay = self.seconds_until_next_run(self._clock(), config.upload_hour)
            logger.info("下一次网盘归档将在 %.0f 秒后执行", delay)
            await self._sleep(delay)
            await self.trigger("scheduled")

    async def trigger(self, trigger: str = "manual") -> bool:
        config = await self.get_config()
        if not config.enabled:
            raise CloudUploadError("请先启用至少一个网盘上传目标")
        async with self._trigger_lock:
            if self.running:
                return False
            self._active_run = asyncio.create_task(
                self._execute_run(trigger, config),
                name=f"recording-cloud-upload-{trigger}",
            )
            return True

    async def _execute_run(self, trigger: str, config: CloudArchiveConfig) -> None:
        started_at = self._clock()
        self._last_execution = UploadExecution(trigger=trigger, status="running", started_at=started_at)
        try:
            summary = await self.run_once(config)
        except asyncio.CancelledError:
            self._last_execution = UploadExecution(
                trigger=trigger,
                status="cancelled",
                started_at=started_at,
                finished_at=self._clock(),
                error="任务已取消，本地文件保持不变",
            )
            raise
        except Exception as exc:
            message = (" ".join(str(exc).split()) or type(exc).__name__)[:500]
            self._last_execution = UploadExecution(
                trigger=trigger,
                status="failed",
                started_at=started_at,
                finished_at=self._clock(),
                error=message,
            )
            logger.exception("网盘归档任务执行失败，本地文件保持不变")
            return
        self._last_execution = UploadExecution(
            trigger=trigger,
            status="partial" if summary.failed_files else "success",
            started_at=started_at,
            finished_at=self._clock(),
            summary=summary,
        )

    async def _create_client(self, target: UploadTarget, config: CloudArchiveConfig) -> CloudUploadClient:
        if target.name == "quark":
            defaults = {"cookie": config.quark_cookie}
            fingerprint = self._fingerprint(defaults)
            credentials = await self.store.resolve_cloud_credentials("quark", fingerprint, defaults)

            async def save_quark(state: dict[str, str]) -> None:
                await self.store.save_cloud_credentials("quark", fingerprint, state)

            return QuarkClient(
                credentials["cookie"],
                root_id=config.quark_root_id,
                timeout_seconds=config.upload_timeout_seconds,
                on_credential_update=save_quark,
            )
        if target.name == "wopan":
            defaults = {
                "access_token": config.wopan_access_token,
                "refresh_token": config.wopan_refresh_token,
            }
            fingerprint = self._fingerprint(defaults)
            credentials = await self.store.resolve_cloud_credentials("wopan", fingerprint, defaults)

            async def save_wopan(state: dict[str, str]) -> None:
                await self.store.save_cloud_credentials("wopan", fingerprint, state)

            return WoPanClient(
                credentials.get("access_token", ""),
                credentials.get("refresh_token", ""),
                root_id=config.wopan_root_id,
                family_id=config.wopan_family_id,
                timeout_seconds=config.upload_timeout_seconds,
                on_credential_update=save_wopan,
            )
        raise CloudUploadError(f"不支持的网盘上传目标：{target.name}")

    async def _get_client(
        self,
        target: UploadTarget,
        config: CloudArchiveConfig,
        clients: dict[str, CloudUploadClient],
    ) -> CloudUploadClient:
        existing = clients.get(target.name)
        if existing is not None:
            return existing
        if self._client_factory is None:
            client = await self._create_client(target, config)
        else:
            created = self._client_factory(target)
            client = await created if inspect.isawaitable(created) else created
        clients[target.name] = client
        return client

    async def _active_recording_directories(self) -> set[Path]:
        directories = self._active_directories_provider() if self._active_directories_provider else set()
        directories = {path.expanduser().resolve() for path in directories}
        for record in await self.store.list():
            if record.status == TaskStatus.RECORDING and record.output_path:
                directories.add(Path(record.output_path).expanduser().resolve().parent)
        return directories

    def _collect_candidates(
        self,
        active_directories: set[Path],
        min_age_minutes: int,
        base: Path | None = None,
    ) -> tuple[list[UploadCandidate], int, int]:
        """Scan ``base`` (the whole library by default) for uploadable recordings.

        Remote paths always stay relative to the recording root so a directory
        scan lands in the same cloud layout as a full run.
        """
        root = self.settings.recordings_dir.resolve()
        scan_root = base or root
        if not root.is_dir() or not scan_root.is_dir():
            return [], 0, 0

        candidates: list[UploadCandidate] = []
        scanned_files = 0
        skipped_files = 0
        cutoff_timestamp = self._clock().timestamp() - min_age_minutes * 60
        for path in sorted(scan_root.rglob("*")):
            try:
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                scanned_files += 1
                resolved = path.resolve()
                stat = resolved.stat()
                is_active = any(
                    directory == resolved.parent or directory in resolved.parents for directory in active_directories
                )
                if is_active or stat.st_size <= 0 or stat.st_mtime > cutoff_timestamp:
                    skipped_files += 1
                    continue
                candidates.append(
                    UploadCandidate(
                        path=resolved,
                        relative_path=resolved.relative_to(root),
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                    )
                )
            except (FileNotFoundError, OSError, ValueError):
                skipped_files += 1
                logger.warning("扫描录像文件时跳过不可用路径：%s", path)
        return candidates, scanned_files, skipped_files

    @staticmethod
    def _is_unchanged(candidate: UploadCandidate) -> bool:
        try:
            stat = candidate.path.stat()
        except OSError:
            return False
        return stat.st_size == candidate.size and stat.st_mtime_ns == candidate.mtime_ns

    @staticmethod
    async def _close_clients(clients: dict[str, CloudUploadClient]) -> None:
        unique_clients = {id(client): client for client in clients.values()}.values()
        results = await asyncio.gather(*(client.aclose() for client in unique_clients), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("关闭网盘客户端失败：%s", result)

    async def _archive_candidate(
        self,
        candidate: UploadCandidate,
        targets: tuple[UploadTarget, ...],
        config: CloudArchiveConfig,
        clients: dict[str, CloudUploadClient],
        record: UploadJob,
    ) -> None:
        """Upload one recording to every target, then delete the local copy.

        Counters and live progress land on ``record`` while the upload runs, so a
        file that fails halfway still reports the copies that made it through.
        """
        record.target_count = len(targets)
        if not self._is_unchanged(candidate):
            raise CloudUploadError("文件在扫描后发生变化")
        for index, target in enumerate(targets):
            if not self._is_unchanged(candidate):
                raise CloudUploadError("文件在上传期间发生变化")
            record.target = target.name
            record.target_index = index
            record.stage = "preparing"
            record.uploaded_bytes = 0
            record.notify()
            remote_path = posixpath.join(target.remote_root, candidate.relative_path.as_posix())
            client = await self._get_client(target, config, clients)
            if await client.upload_verified(candidate.path, remote_path, progress=record.report):
                record.uploaded_copies += 1
            logger.info("录像已在 %s 上确认：%s", target.name, remote_path)
        if not self._is_unchanged(candidate):
            raise CloudUploadError("文件在上传完成后发生变化")
        await asyncio.to_thread(candidate.path.unlink)
        record.deleted = True
        logger.info("所有目标上传成功，已删除本地录像：%s", candidate.path)

    async def run_once(self, config: CloudArchiveConfig | None = None) -> UploadRunSummary:
        config = config or await self.get_config()
        targets = tuple(UploadTarget(name, path.rstrip("/") or "/") for name, path in config.targets)
        if not targets:
            return UploadRunSummary()

        active_directories = await self._active_recording_directories()
        candidates, scanned_files, skipped_files = await asyncio.to_thread(
            self._collect_candidates,
            active_directories,
            config.upload_min_age_minutes,
        )
        uploaded_copies = 0
        deleted_files = 0
        failed_files = 0
        clients: dict[str, CloudUploadClient] = {}
        async with self._upload_lock:
            try:
                for candidate in candidates:
                    record = UploadJob(
                        path=candidate.relative_path.as_posix(),
                        name=candidate.path.name,
                        size=candidate.size,
                        created_at=self._clock(),
                        monotonic=self._monotonic,
                    )
                    try:
                        await self._archive_candidate(candidate, targets, config, clients, record)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed_files += 1
                        logger.error("录像归档失败，保留本地文件 %s：%s", candidate.path, exc)
                    else:
                        deleted_files += 1
                    uploaded_copies += record.uploaded_copies
            finally:
                await self._close_clients(clients)

        summary = UploadRunSummary(
            scanned_files=scanned_files,
            skipped_files=skipped_files,
            uploaded_copies=uploaded_copies,
            deleted_files=deleted_files,
            failed_files=failed_files,
        )
        logger.info("网盘归档完成：%s", summary)
        return summary

    def _resolve_recording(self, relative_path: str) -> Path:
        root = self.settings.recordings_dir.resolve()
        resolved = root.joinpath(relative_path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CloudUploadError("录像路径不在录像目录内") from exc
        if resolved.suffix.lower() not in VIDEO_EXTENSIONS:
            raise CloudUploadError("只能上传录像视频文件")
        return resolved

    def _resolve_directory(self, relative_path: str) -> Path:
        root = self.settings.recordings_dir.resolve()
        if not relative_path:
            return root
        resolved = root.joinpath(relative_path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CloudUploadError("录像路径不在录像目录内") from exc
        if not resolved.is_dir():
            raise CloudUploadError("录像目录不存在")
        return resolved

    def _build_candidate(self, relative_path: str) -> UploadCandidate:
        resolved = self._resolve_recording(relative_path)
        try:
            if resolved.is_symlink() or not resolved.is_file():
                raise CloudUploadError("录像文件不存在")
            stat = resolved.stat()
        except OSError as exc:
            raise CloudUploadError("录像文件不存在或无法读取") from exc
        if stat.st_size <= 0:
            raise CloudUploadError("录像文件为空，无法上传")
        return UploadCandidate(
            path=resolved,
            relative_path=resolved.relative_to(self.settings.recordings_dir.resolve()),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )

    def _prune_jobs(self) -> None:
        now = self._clock()
        for path, job in list(self._jobs.items()):
            if job.finished_at is not None and (now - job.finished_at).total_seconds() > JOB_RETENTION_SECONDS:
                del self._jobs[path]
        while len(self._jobs) > MAX_JOBS:
            stale = next((path for path, job in self._jobs.items() if job.status not in ACTIVE_JOB_STATUSES), None)
            if stale is None:
                return
            del self._jobs[stale]

    def jobs(self) -> list[UploadJob]:
        self._prune_jobs()
        return list(self._jobs.values())

    def subscribe(self) -> asyncio.Event:
        """Register a listener for job changes, pre-set so it gets a snapshot at once."""
        event = asyncio.Event()
        event.set()
        self._subscribers.add(event)
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        self._subscribers.discard(event)

    def _notify(self) -> None:
        for event in self._subscribers:
            event.set()

    def _register_job(self, candidate: UploadCandidate, target_count: int) -> UploadJob | None:
        """Queue one recording, or return None when it is already queued or running."""
        path = candidate.relative_path.as_posix()
        existing = self._jobs.get(path)
        if existing is not None and existing.status in ACTIVE_JOB_STATUSES:
            return None
        # Re-inserting keeps the newest attempt last so the list reads chronologically.
        self._jobs.pop(path, None)
        job = UploadJob(
            path=path,
            name=candidate.path.name,
            size=candidate.size,
            created_at=self._clock(),
            target_count=target_count,
            monotonic=self._monotonic,
            on_change=self._notify,
        )
        self._jobs[path] = job
        self._notify()
        return job

    async def enqueue_file(self, relative_path: str) -> UploadJob:
        config = await self.get_config()
        if not config.enabled:
            raise CloudUploadError("请先启用至少一个网盘上传目标")
        candidate = await asyncio.to_thread(self._build_candidate, relative_path)

        active_directories = await self._active_recording_directories()
        if any(
            directory == candidate.path.parent or directory in candidate.path.parents
            for directory in active_directories
        ):
            raise CloudUploadError("该录像所在目录正在录制，请先停止任务再上传")

        self._prune_jobs()
        job = self._register_job(candidate, len(config.targets))
        if job is None:
            raise CloudUploadError("该录像已在上传队列中")
        self._ensure_job_worker()
        return job

    async def collect_manual_candidates(self, relative_path: str = "") -> list[UploadCandidate]:
        """List every recording under ``relative_path`` that a manual upload would take.

        Unlike the scheduled run this ignores the stability window, because the
        click itself is the intent; the per-file change guards still apply.
        """
        base = await asyncio.to_thread(self._resolve_directory, relative_path)
        active_directories = await self._active_recording_directories()
        candidates, _scanned, _skipped = await asyncio.to_thread(
            self._collect_candidates,
            active_directories,
            0,
            base,
        )
        return candidates

    async def enqueue_directory(self, relative_path: str = "") -> list[UploadJob]:
        config = await self.get_config()
        if not config.enabled:
            raise CloudUploadError("请先启用至少一个网盘上传目标")
        candidates = await self.collect_manual_candidates(relative_path)
        if not candidates:
            raise CloudUploadError("没有可上传的录像")

        self._prune_jobs()
        jobs = [job for candidate in candidates if (job := self._register_job(candidate, len(config.targets)))]
        if not jobs:
            raise CloudUploadError("这些录像都已在上传队列中")
        self._ensure_job_worker()
        return jobs

    def cancel_all(self) -> int:
        return sum(1 for path in list(self._jobs) if self.cancel_file(path))

    def cancel_file(self, relative_path: str) -> bool:
        job = self._jobs.get(relative_path)
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return False
        if job.status == "queued":
            self._finish_job(job, "cancelled", "上传已取消")
            return True
        active = self._active_job
        if active is None or active[0] is not job:
            return False
        active[1].cancel()
        return True

    def _finish_job(self, job: UploadJob, status: str, error: str | None) -> None:
        job.status = status
        job.error = error
        job.stage = ""
        job.finished_at = self._clock()
        if status == "success":
            job.uploaded_bytes = job.size
            job.target_index = max(0, job.target_count - 1)
        self._notify()

    def _ensure_job_worker(self) -> None:
        if self._job_worker is None or self._job_worker.done():
            self._job_worker = asyncio.create_task(self._process_jobs(), name="recording-manual-upload")

    async def _process_jobs(self) -> None:
        """Drain queued manual uploads one at a time.

        The final lookup and the return are not separated by an await, so a job
        enqueued after it either finds this worker still live or starts a new one.
        """
        while True:
            job = next((item for item in self._jobs.values() if item.status == "queued"), None)
            if job is None:
                return
            task = asyncio.create_task(self._run_job(job), name="recording-manual-upload-file")
            self._active_job = (job, task)
            try:
                # Cancelling one job must not take the worker down with it.
                await asyncio.gather(task, return_exceptions=True)
            finally:
                self._active_job = None

    async def _run_job(self, job: UploadJob) -> None:
        config = await self.get_config()
        targets = tuple(UploadTarget(name, path.rstrip("/") or "/") for name, path in config.targets)
        if not targets:
            self._finish_job(job, "failed", "没有已启用的网盘上传目标")
            return

        async with self._upload_lock:
            if job.status != "queued":
                return
            job.status = "running"
            job.started_at = self._clock()
            job.target_count = len(targets)
            self._notify()
            clients: dict[str, CloudUploadClient] = {}
            try:
                candidate = await asyncio.to_thread(self._build_candidate, job.path)
                job.size = candidate.size
                await self._archive_candidate(candidate, targets, config, clients, job)
            except asyncio.CancelledError:
                self._finish_job(job, "cancelled", "上传已取消，本地文件保持不变")
                raise
            except Exception as exc:
                message = (" ".join(str(exc).split()) or type(exc).__name__)[:500]
                self._finish_job(job, "failed", message)
                logger.error("手动上传录像失败，保留本地文件 %s：%s", job.path, message)
            else:
                self._finish_job(job, "success", None)
            finally:
                await self._close_clients(clients)
