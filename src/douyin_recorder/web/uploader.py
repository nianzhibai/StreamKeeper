from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import posixpath
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..cloud import CloudArchiveConfig, CloudUploadClient, CloudUploadError, QuarkClient, WoPanClient
from ..settings import Settings
from .schemas import TaskStatus
from .store import TaskStore

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset({".flv", ".mkv", ".mp4", ".ts"})


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
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self._seed_config = CloudArchiveConfig.from_settings(settings)
        self._seed_config.validate()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._client_factory = client_factory
        self._active_directories_provider = active_directories_provider
        self._config: CloudArchiveConfig | None = None
        self._config_lock = asyncio.Lock()
        self._trigger_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._active_run: asyncio.Task[None] | None = None
        self._last_execution: UploadExecution | None = None

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
        tasks = [task for task in (self._runner, self._active_run) if task is not None]
        self._runner = None
        self._active_run = None
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
    ) -> tuple[list[UploadCandidate], int, int]:
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

    @staticmethod
    async def _close_clients(clients: dict[str, CloudUploadClient]) -> None:
        unique_clients = {id(client): client for client in clients.values()}.values()
        results = await asyncio.gather(*(client.aclose() for client in unique_clients), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("关闭网盘客户端失败：%s", result)

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
        try:
            for candidate in candidates:
                try:
                    if not self._is_unchanged(candidate):
                        raise CloudUploadError("文件在扫描后发生变化")
                    for target in targets:
                        if not self._is_unchanged(candidate):
                            raise CloudUploadError("文件在上传期间发生变化")
                        remote_path = posixpath.join(
                            target.remote_root,
                            candidate.relative_path.as_posix(),
                        )
                        client = await self._get_client(target, config, clients)
                        if await client.upload_verified(candidate.path, remote_path):
                            uploaded_copies += 1
                        logger.info("录像已在 %s 上确认：%s", target.name, remote_path)
                    if not self._is_unchanged(candidate):
                        raise CloudUploadError("文件在上传完成后发生变化")
                    await asyncio.to_thread(candidate.path.unlink)
                    deleted_files += 1
                    logger.info("所有目标上传成功，已删除本地录像：%s", candidate.path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_files += 1
                    logger.error("录像归档失败，保留本地文件 %s：%s", candidate.path, exc)
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
