from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import logging
import posixpath
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..cloud import CloudArchiveConfig, CloudUploadClient, CloudUploadError, create_cloud_client
from ..models import RecordingResult
from ..settings import UPLOAD_MODE_RECORDING_COMPLETED, UPLOAD_MODE_SCHEDULED, Settings
from .events import EventLog
from .recordings import RecordingPreviewCache
from .schemas import TaskStatus
from .store import TaskStore

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset({".flv", ".mkv", ".mp4", ".ts"})
# One broken credential fails every file; the run summary covers the rest.
MAX_FAILURE_EVENTS_PER_RUN = 5
TRIGGER_LABELS = {
    "manual": "手动",
    "scheduled": "定时",
    "recording_completed": "录制完成后自动",
}
_SEGMENT_PLACEHOLDER = re.compile(r"%(?:0(?P<width>[1-9]\d*))?d")


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


@dataclass(frozen=True, slots=True)
class ArchiveCandidateResult:
    """The useful work completed before one recording's archive stopped."""

    uploaded_copies: int = 0
    error: Exception | None = None


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


@dataclass(frozen=True, slots=True)
class UploadRequest:
    trigger: str
    config: CloudArchiveConfig
    recording_outputs: tuple[str, ...] | None = None


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
        sleep: Sleep = asyncio.sleep,
        events: EventLog | None = None,
        preview_cache: RecordingPreviewCache | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.events = events or EventLog(store)
        self.preview_cache = preview_cache
        self._seed_config = CloudArchiveConfig.from_settings(settings)
        self._seed_config.validate()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._client_factory = client_factory
        self._active_directories_provider = active_directories_provider
        self._config: CloudArchiveConfig | None = None
        self._config_lock = asyncio.Lock()
        self._trigger_lock = asyncio.Lock()
        # One archive run owns candidate discovery, cloud clients, and local
        # cleanup from start to finish.  This also keeps direct run_once callers
        # from selecting the same file concurrently.
        self._upload_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._active_run: asyncio.Task[None] | None = None
        self._requests: deque[UploadRequest] = deque()
        self._last_execution: UploadExecution | None = None
        self._shutting_down = False

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
        if config.enabled and config.upload_mode == UPLOAD_MODE_SCHEDULED:
            self._runner = asyncio.create_task(self._run_forever(), name="recording-cloud-upload-scheduler")

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
        if not config.enabled or config.upload_mode != UPLOAD_MODE_SCHEDULED:
            return None
        now = self._clock()
        return now + timedelta(seconds=self.seconds_until_next_run(now, config.upload_hour))

    async def startup(self) -> None:
        self._shutting_down = False
        config = await self.initialize_config()
        if (
            config.enabled
            and config.upload_mode == UPLOAD_MODE_SCHEDULED
            and (self._runner is None or self._runner.done())
        ):
            self._runner = asyncio.create_task(self._run_forever(), name="recording-cloud-upload-scheduler")

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._trigger_lock:
            self._requests.clear()
            active_run = self._active_run
            self._active_run = None
        tasks = [task for task in (self._runner, active_run) if task is not None]
        self._runner = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_forever(self) -> None:
        while True:
            config = await self.get_config()
            if not config.enabled or config.upload_mode != UPLOAD_MODE_SCHEDULED:
                return
            delay = self.seconds_until_next_run(self._clock(), config.upload_hour)
            logger.info("下一次网盘归档将在 %.0f 秒后执行", delay)
            await self._sleep(delay)
            await self.trigger("scheduled")

    def _start_request_worker_locked(self) -> None:
        if self._shutting_down or self.running or not self._requests:
            return
        self._active_run = asyncio.create_task(
            self._run_requests(),
            name="recording-cloud-upload-worker",
        )

    async def trigger(self, trigger: str = "manual") -> bool:
        config = await self.get_config()
        if not config.enabled:
            raise CloudUploadError("请先启用至少一个网盘上传目标")
        async with self._trigger_lock:
            if self.running or self._requests or self._shutting_down:
                return False
            self._requests.append(UploadRequest(trigger=trigger, config=config))
            self._start_request_worker_locked()
            return True

    async def recording_completed(self, result: RecordingResult) -> bool:
        """Queue exactly one completed recording batch for automatic upload.

        Completion notifications are retained while another archive request is
        running, so simultaneous live endings cannot silently lose an upload.
        """
        config = await self.get_config()
        if not config.enabled or config.upload_mode != UPLOAD_MODE_RECORDING_COMPLETED:
            return False
        request = UploadRequest(
            trigger="recording_completed",
            config=config,
            recording_outputs=(result.output_path,),
        )
        async with self._trigger_lock:
            if self._shutting_down:
                return False
            if request in self._requests:
                return True
            self._requests.append(request)
            self._start_request_worker_locked()
        return True

    async def _run_requests(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                async with self._trigger_lock:
                    if self._shutting_down or not self._requests:
                        return
                    request = self._requests.popleft()
                await self._execute_run(
                    request.trigger,
                    request.config,
                    recording_outputs=request.recording_outputs,
                )
        finally:
            async with self._trigger_lock:
                if self._active_run is current_task:
                    self._active_run = None
                    self._start_request_worker_locked()

    async def _execute_run(
        self,
        trigger: str,
        config: CloudArchiveConfig,
        *,
        recording_outputs: tuple[str, ...] | None = None,
    ) -> None:
        started_at = self._clock()
        self._last_execution = UploadExecution(trigger=trigger, status="running", started_at=started_at)
        label = TRIGGER_LABELS.get(trigger, trigger)
        targets = "、".join(name for name, _path in config.targets)
        await self.events.info("upload", f"开始{label}归档到网盘", f"上传目标：{targets}" if targets else None)
        try:
            summary = await self.run_once(config, recording_outputs=recording_outputs)
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
            await self.events.error("upload", f"{label}归档执行失败，本地文件保持不变", message)
            return
        self._last_execution = UploadExecution(
            trigger=trigger,
            status="partial" if summary.failed_files else "success",
            started_at=started_at,
            finished_at=self._clock(),
            summary=summary,
        )
        detail = (
            f"扫描 {summary.scanned_files} 个文件 · 跳过 {summary.skipped_files} 个 · "
            f"上传 {summary.uploaded_copies} 个副本 · 清理本地 {summary.deleted_files} 个"
        )
        if summary.failed_files:
            await self.events.warning(
                "upload",
                f"{label}归档完成，但有 {summary.failed_files} 个文件失败",
                detail,
            )
        else:
            await self.events.success("upload", f"{label}归档完成", detail)

    async def _create_client(self, target: UploadTarget, config: CloudArchiveConfig) -> CloudUploadClient:
        provider = config.provider(target.name)
        defaults = dict(provider.credentials)
        fingerprint = self._fingerprint(defaults)
        credentials = await self.store.resolve_cloud_credentials(target.name, fingerprint, defaults)

        credential_state = dict(credentials)

        async def save_credentials(state: dict[str, str]) -> None:
            # Refresh callbacks may only return rotated fields. Keep the latest
            # complete state across multiple refreshes by this client.
            credential_state.update(state)
            await self.store.save_cloud_credentials(target.name, fingerprint, dict(credential_state))

        return create_cloud_client(
            provider,
            credentials,
            timeout_seconds=config.upload_timeout_seconds,
            on_credential_update=save_credentials,
        )

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

    @staticmethod
    def _segment_filename_pattern(template_name: str) -> re.Pattern[str] | None:
        matches = list(_SEGMENT_PLACEHOLDER.finditer(template_name))
        if not matches:
            return None
        parts: list[str] = []
        offset = 0
        for match in matches:
            parts.append(re.escape(template_name[offset : match.start()]))
            width = int(match.group("width") or 0)
            parts.append(rf"[0-9]{{{width},}}" if width else r"[0-9]+")
            offset = match.end()
        parts.append(re.escape(template_name[offset:]))
        return re.compile("".join(parts))

    def _expand_recording_output(self, output_path: str) -> list[Path]:
        """Resolve a recorder output path or FFmpeg segment template safely."""
        root = self.settings.recordings_dir.resolve()
        template = Path(output_path).expanduser()
        if not template.is_absolute():
            template = template.resolve()
        parent = template.parent.resolve()
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise CloudUploadError("录制完成路径不在录像目录内") from exc

        pattern = self._segment_filename_pattern(template.name)
        if pattern is None:
            return [parent / template.name]
        try:
            return sorted(path for path in parent.iterdir() if pattern.fullmatch(path.name))
        except OSError as exc:
            raise CloudUploadError("无法读取录制完成目录") from exc

    def _collect_completed_candidates(
        self,
        recording_outputs: tuple[str, ...],
    ) -> tuple[list[UploadCandidate], int, int]:
        """Collect only files produced by completed recorder invocations.

        A successful recorder exit is the stability boundary, so these explicit
        files do not wait for the age window used by whole-library scans.
        """
        root = self.settings.recordings_dir.resolve()
        candidates: list[UploadCandidate] = []
        seen: set[Path] = set()
        scanned_files = 0
        skipped_files = 0
        for output_path in recording_outputs:
            try:
                paths = self._expand_recording_output(output_path)
            except (CloudUploadError, OSError, ValueError) as exc:
                skipped_files += 1
                logger.warning("跳过不可用的录制完成路径 %s：%s", output_path, exc)
                continue
            if not paths:
                skipped_files += 1
                logger.warning("录制完成路径没有匹配到录像文件：%s", output_path)
                continue
            for path in paths:
                try:
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                        skipped_files += 1
                        continue
                    resolved = path.resolve()
                    resolved.relative_to(root)
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    stat = resolved.stat()
                    scanned_files += 1
                    if stat.st_size <= 0:
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
                    logger.warning("扫描录制完成文件时跳过不可用路径：%s", path)
        return sorted(candidates, key=lambda candidate: candidate.path), scanned_files, skipped_files

    def _collect_candidates(
        self,
        active_directories: set[Path],
        min_age_minutes: int,
    ) -> tuple[list[UploadCandidate], int, int]:
        """Scan the whole recording library for uploadable recordings.

        Remote paths always stay relative to the recording root so a directory
        scan lands in the same cloud layout as the archive.
        """
        root = self.settings.recordings_dir.resolve()
        if not root.is_dir():
            return [], 0, 0

        candidates: list[UploadCandidate] = []
        scanned_files = 0
        skipped_files = 0
        cutoff_timestamp = self._clock().timestamp() - min_age_minutes * 60
        for path in sorted(root.rglob("*")):
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

    def _delete_archived_recording(self, path: Path, active_directories: set[Path]) -> int:
        """Delete one verified recording and prune its empty parents.

        The recording root is application-owned and must always remain available
        for future recordings.  Every directory below it is removed only when
        the operating system confirms that it is empty, so unrelated files,
        remaining recordings, and known active recording branches stop the walk
        without being touched.
        """
        root = self.settings.recordings_dir.resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CloudUploadError("录像路径不在录像目录内") from exc

        resolved.unlink()
        removed_directories = 0
        directory = resolved.parent
        while directory != root:
            if any(
                directory == active or directory in active.parents or active in directory.parents
                for active in active_directories
            ):
                break
            try:
                directory.rmdir()
            except FileNotFoundError:
                # A concurrent cleanup has already handled this branch.
                break
            except OSError as exc:
                # A non-empty directory is the normal stopping condition.  Do
                # not turn an otherwise successful archive into a failure when
                # directory cleanup is unavailable, but keep it observable.
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    logger.warning("已删除本地录像，但无法清理目录 %s：%s", directory, exc)
                break
            removed_directories += 1
            directory = directory.parent
        return removed_directories

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
    ) -> ArchiveCandidateResult:
        """Upload one recording to every target, then delete the local copy."""
        uploaded_copies = 0
        try:
            if not self._is_unchanged(candidate):
                raise CloudUploadError("文件在扫描后发生变化")
            for target in targets:
                if not self._is_unchanged(candidate):
                    raise CloudUploadError("文件在上传期间发生变化")
                remote_path = posixpath.join(target.remote_root, candidate.relative_path.as_posix())
                client = await self._get_client(target, config, clients)
                if await client.upload_verified(candidate.path, remote_path):
                    uploaded_copies += 1
                logger.info("录像已在 %s 上确认：%s", target.name, remote_path)
            if not self._is_unchanged(candidate):
                raise CloudUploadError("文件在上传完成后发生变化")
            active_directories = await self._active_recording_directories()
            removed_directories = await asyncio.to_thread(
                self._delete_archived_recording,
                candidate.path,
                active_directories,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ArchiveCandidateResult(uploaded_copies=uploaded_copies, error=exc)

        logger.info("所有目标上传成功，已删除本地录像：%s", candidate.path)
        if removed_directories:
            logger.info("已清理 %d 个空的本地录像目录：%s", removed_directories, candidate.path.parent)
        # The browser-playback remux is derived from a file that no longer
        # exists, so it can never be served again. Dropping it here keeps the
        # cache from holding a full-size copy of an archived recording.
        if self.preview_cache is not None:
            try:
                discarded = await self.preview_cache.discard(candidate.relative_path.as_posix())
            except Exception:
                logger.exception("本地录像已归档，但清理播放缓存失败：%s", candidate.relative_path)
            else:
                if discarded:
                    logger.info("已清理 %d 个播放缓存文件：%s", discarded, candidate.relative_path.as_posix())
        return ArchiveCandidateResult(uploaded_copies=uploaded_copies)

    async def run_once(
        self,
        config: CloudArchiveConfig | None = None,
        *,
        recording_outputs: tuple[str, ...] | None = None,
    ) -> UploadRunSummary:
        config = config or await self.get_config()
        targets = tuple(UploadTarget(name, path.rstrip("/") or "/") for name, path in config.targets)
        if not targets:
            return UploadRunSummary()

        async with self._upload_lock:
            if recording_outputs is None:
                active_directories = await self._active_recording_directories()
                candidates, scanned_files, skipped_files = await asyncio.to_thread(
                    self._collect_candidates,
                    active_directories,
                    config.upload_min_age_minutes,
                )
            else:
                candidates, scanned_files, skipped_files = await asyncio.to_thread(
                    self._collect_completed_candidates,
                    recording_outputs,
                )
            uploaded_copies = 0
            deleted_files = 0
            failed_files = 0
            clients: dict[str, CloudUploadClient] = {}
            try:
                for candidate in candidates:
                    result = await self._archive_candidate(candidate, targets, config, clients)
                    uploaded_copies += result.uploaded_copies
                    if result.error is not None:
                        failed_files += 1
                        logger.error("录像归档失败，保留本地文件 %s：%s", candidate.path, result.error)
                        if failed_files <= MAX_FAILURE_EVENTS_PER_RUN:
                            await self.events.error(
                                "upload",
                                f"{candidate.path.name} 归档失败，已保留本地文件",
                                str(result.error),
                            )
                    else:
                        deleted_files += 1
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
