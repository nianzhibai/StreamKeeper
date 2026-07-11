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

from ..cloud import CloudUploadClient, CloudUploadError, QuarkClient, WoPanClient
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


UploadClientFactory = Callable[
    [UploadTarget],
    CloudUploadClient | Awaitable[CloudUploadClient],
]
ActiveDirectoriesProvider = Callable[[], set[Path]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class RecordingUploadService:
    """Upload stable recordings to every configured native cloud target daily."""

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
        self.targets = tuple(UploadTarget(name, path.rstrip("/") or "/") for name, path in settings.upload_targets)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._client_factory = client_factory or self._create_client
        self._active_directories_provider = active_directories_provider
        self._runner: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.upload_enabled

    @staticmethod
    def _credential_fingerprint(state: dict[str, str]) -> str:
        serialized = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(serialized).hexdigest()

    async def _create_client(self, target: UploadTarget) -> CloudUploadClient:
        if target.name == "quark":
            defaults = {"cookie": self.settings.quark_cookie}
            fingerprint = self._credential_fingerprint(defaults)
            credentials = await self.store.resolve_cloud_credentials("quark", fingerprint, defaults)

            async def save_quark(state: dict[str, str]) -> None:
                await self.store.save_cloud_credentials("quark", fingerprint, state)

            return QuarkClient(
                credentials["cookie"],
                root_id=self.settings.quark_root_id,
                timeout_seconds=self.settings.upload_timeout_seconds,
                on_credential_update=save_quark,
            )
        if target.name == "wopan":
            defaults = {
                "access_token": self.settings.wopan_access_token,
                "refresh_token": self.settings.wopan_refresh_token,
            }
            fingerprint = self._credential_fingerprint(defaults)
            credentials = await self.store.resolve_cloud_credentials("wopan", fingerprint, defaults)

            async def save_wopan(state: dict[str, str]) -> None:
                await self.store.save_cloud_credentials("wopan", fingerprint, state)

            return WoPanClient(
                credentials.get("access_token", ""),
                credentials.get("refresh_token", ""),
                root_id=self.settings.wopan_root_id,
                family_id=self.settings.wopan_family_id,
                timeout_seconds=self.settings.upload_timeout_seconds,
                on_credential_update=save_wopan,
            )
        raise CloudUploadError(f"不支持的网盘上传目标：{target.name}")

    async def _get_client(
        self,
        target: UploadTarget,
        clients: dict[str, CloudUploadClient],
    ) -> CloudUploadClient:
        existing = clients.get(target.name)
        if existing is not None:
            return existing
        created = self._client_factory(target)
        client = await created if inspect.isawaitable(created) else created
        clients[target.name] = client
        return client

    @staticmethod
    def seconds_until_next_run(now: datetime, hour: int) -> float:
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run < now:
            next_run += timedelta(days=1)
        return max(0.0, (next_run - now).total_seconds())

    async def startup(self) -> None:
        if not self.enabled or (self._runner is not None and not self._runner.done()):
            return
        self._runner = asyncio.create_task(self._run_forever(), name="recording-cloud-upload")

    async def shutdown(self) -> None:
        runner = self._runner
        self._runner = None
        if runner is None:
            return
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    async def _run_forever(self) -> None:
        while True:
            delay = self.seconds_until_next_run(self._clock(), self.settings.upload_hour)
            logger.info("下一次网盘归档将在 %.0f 秒后执行", delay)
            await self._sleep(delay)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("网盘归档任务执行失败，本地文件将保留到下次重试")

    async def _active_recording_directories(self) -> set[Path]:
        directories = self._active_directories_provider() if self._active_directories_provider else set()
        directories = {path.expanduser().resolve() for path in directories}
        for record in await self.store.list():
            if record.status == TaskStatus.RECORDING and record.output_path:
                directories.add(Path(record.output_path).expanduser().resolve().parent)
        return directories

    def _collect_candidates(self, active_directories: set[Path]) -> tuple[list[UploadCandidate], int, int]:
        root = self.settings.recordings_dir.resolve()
        if not root.is_dir():
            return [], 0, 0

        candidates: list[UploadCandidate] = []
        scanned_files = 0
        skipped_files = 0
        cutoff_timestamp = self._clock().timestamp() - self.settings.upload_min_age_minutes * 60
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

    async def run_once(self) -> UploadRunSummary:
        if not self.enabled:
            return UploadRunSummary()

        async with self._run_lock:
            active_directories = await self._active_recording_directories()
            candidates, scanned_files, skipped_files = await asyncio.to_thread(
                self._collect_candidates,
                active_directories,
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
                        for target in self.targets:
                            if not self._is_unchanged(candidate):
                                raise CloudUploadError("文件在上传期间发生变化")
                            remote_path = posixpath.join(
                                target.remote_root,
                                candidate.relative_path.as_posix(),
                            )
                            client = await self._get_client(target, clients)
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
