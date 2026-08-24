from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import time

from fastapi import HTTPException

from ..errors import InsufficientDiskSpaceError
from ..storage import ensure_disk_reserve, wait_until_disk_reserve_reached
from .schemas import RecordingDirectoryView, RecordingEntry

RECORDING_MEDIA_TYPES = {
    ".flv": "video/x-flv",
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".ts": "video/mp2t",
}
_DIRECT_PLAYBACK_EXTENSIONS = frozenset({".mp4"})
_PREVIEW_CACHE_MAX_FILES = 8
_PREVIEW_CACHE_MAX_BYTES = 10 * 1024**3
_PREVIEW_CACHE_MAX_AGE_SECONDS = 24 * 3600
_CACHE_HASH_LENGTH = 32


def _invalid_path() -> HTTPException:
    return HTTPException(status_code=400, detail="录像路径无效")


def resolve_recording_path(root: Path, value: str, *, allow_root: bool = False) -> tuple[Path, str]:
    """Resolve an untrusted POSIX-style relative path inside the recording root."""
    if not value:
        if allow_root:
            return root.resolve(), ""
        raise _invalid_path()
    if len(value) > 4096 or "\x00" in value or "\\" in value:
        raise _invalid_path()

    relative = PurePosixPath(value)
    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise _invalid_path()

    root = root.resolve()
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise _invalid_path()

    resolved = candidate.resolve(strict=False)
    try:
        normalized = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise _invalid_path() from exc
    return resolved, normalized


def _directory_total_bytes(directory: Path) -> int:
    """Recursive size of every regular file below one directory.

    Computed on each listing so folder sizes always match the disk, including
    recordings still being written. Entries that vanish mid-walk (a recorder
    or archive job moving files) are skipped, like in ``_list_directory``.
    """
    total = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _list_directory(root: Path, relative_path: str) -> RecordingDirectoryView:
    root = root.resolve()
    directory, normalized = resolve_recording_path(root, relative_path, allow_root=True)
    if not directory.exists() or not directory.is_dir():
        raise HTTPException(status_code=404, detail="录像目录不存在")

    entries: list[RecordingEntry] = []
    try:
        children = tuple(directory.iterdir())
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法读取录像目录") from exc

    for child in children:
        try:
            if child.is_symlink():
                continue
            stat = child.stat()
            child_path = child.relative_to(root).as_posix()
            modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if child.is_dir():
                entries.append(
                    RecordingEntry(
                        name=child.name,
                        path=child_path,
                        kind="directory",
                        size=_directory_total_bytes(child),
                        modified_at=modified_at,
                        extension=None,
                        playback_mode=None,
                        playable=False,
                    )
                )
                continue

            extension = child.suffix.lower()
            if not child.is_file() or extension not in RECORDING_MEDIA_TYPES:
                continue
            entries.append(
                RecordingEntry(
                    name=child.name,
                    path=child_path,
                    kind="video",
                    size=stat.st_size,
                    modified_at=modified_at,
                    extension=extension.removeprefix("."),
                    playback_mode="direct" if extension in _DIRECT_PLAYBACK_EXTENSIONS else "remux",
                    playable=stat.st_size > 0,
                )
            )
        except OSError:
            # A recorder or archive job can move a file while the directory is being listed.
            continue

    entries.sort(key=lambda entry: (entry.kind != "directory", entry.name.casefold()))
    return RecordingDirectoryView(path=normalized, entries=entries)


async def list_recording_directory(root: Path, relative_path: str) -> RecordingDirectoryView:
    return await asyncio.to_thread(_list_directory, root, relative_path)


def get_recording_file(root: Path, relative_path: str) -> tuple[Path, str]:
    path, normalized = resolve_recording_path(root, relative_path)
    if not path.exists() or not path.is_file() or path.suffix.lower() not in RECORDING_MEDIA_TYPES:
        raise HTTPException(status_code=404, detail="录像文件不存在")
    return path, normalized


def _delete_recording_file(root: Path, relative_path: str) -> str:
    path, normalized = get_recording_file(root, relative_path)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="录像文件不存在") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法删除录像文件") from exc
    return normalized


def resolve_ffmpeg(executable: str) -> str:
    resolved = shutil.which(executable)
    if not resolved:
        raise HTTPException(status_code=503, detail="服务器未安装 FFmpeg，无法进行无损封装播放")
    return resolved


def build_remux_command(executable: str, source: Path, destination: Path) -> list[str]:
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(destination),
    ]


class RecordingPreviewCache:
    def __init__(
        self,
        directory: Path,
        ffmpeg: str,
        *,
        max_files: int = _PREVIEW_CACHE_MAX_FILES,
        max_bytes: int = _PREVIEW_CACHE_MAX_BYTES,
        max_age_seconds: int = _PREVIEW_CACHE_MAX_AGE_SECONDS,
    ) -> None:
        self.directory = directory
        self.ffmpeg = ffmpeg
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._generation_lock = asyncio.Lock()

    @staticmethod
    def _source_signature(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="录像文件不存在") from exc
        return stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _path_prefix(relative_path: str) -> str:
        return hashlib.sha256(relative_path.encode()).hexdigest()[:_CACHE_HASH_LENGTH]

    @classmethod
    def _cache_key(cls, relative_path: str, signature: tuple[int, int]) -> str:
        """Name a cache entry ``<path>-<signature>``.

        The signature half keeps a re-recorded file from being served its
        predecessor's remux; the path half lets ``discard`` find the entry once
        the source is gone and its size and mtime can no longer be read.
        """
        value = f"{signature[0]}\0{signature[1]}".encode()
        signature_hash = hashlib.sha256(value).hexdigest()[:_CACHE_HASH_LENGTH]
        return f"{cls._path_prefix(relative_path)}-{signature_hash}"

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _is_cached(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _cleanup(self, current: Path) -> None:
        now = time()
        cached: list[tuple[Path, int, float]] = []
        try:
            children = tuple(self.directory.iterdir())
        except OSError:
            return

        for path in children:
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.name.startswith("."):
                if now - stat.st_mtime > 3600:
                    self._unlink(path)
                continue
            if path.suffix != ".mp4" or not path.is_file():
                continue
            if path != current and now - stat.st_mtime > self.max_age_seconds:
                self._unlink(path)
                continue
            cached.append((path, stat.st_size, stat.st_mtime))

        total_bytes = sum(item[1] for item in cached)
        file_count = len(cached)
        for path, size, _modified_at in sorted(cached, key=lambda item: item[2]):
            if file_count <= self.max_files and total_bytes <= self.max_bytes:
                break
            if path == current:
                continue
            if path.exists():
                self._unlink(path)
            if path.exists():
                continue
            file_count -= 1
            total_bytes -= size

    def _discard_sync(self, relative_path: str) -> int:
        """Drop every finished remux of one recording; never raises."""
        prefix = f"{self._path_prefix(relative_path)}-"
        removed = 0
        try:
            children = tuple(self.directory.iterdir())
        except OSError:
            return 0
        for path in children:
            # In-flight temporaries are dot-prefixed and stay untouched: the
            # remux that owns one already re-checks the source and cleans up.
            if path.suffix != ".mp4" or not path.name.startswith(prefix):
                continue
            self._unlink(path)
            if not path.exists():
                removed += 1
        return removed

    async def discard(self, relative_path: str) -> int:
        return await asyncio.to_thread(self._discard_sync, relative_path)

    def _total_bytes_sync(self) -> int:
        total = 0
        try:
            children = tuple(self.directory.iterdir())
        except OSError:
            return 0
        for path in children:
            # Dot-prefixed in-flight temporaries are transient and stay out of the total.
            if path.name.startswith(".") or path.suffix != ".mp4":
                continue
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    async def total_bytes(self) -> int:
        """Disk footprint of the finished remuxes. Blocking scan: runs in a thread."""
        return await asyncio.to_thread(self._total_bytes_sync)

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    @classmethod
    async def _communicate_with_disk_guard(
        cls,
        process: asyncio.subprocess.Process,
        directory: Path,
    ) -> tuple[bytes, bool]:
        communication = asyncio.create_task(process.communicate())
        disk_guard = asyncio.create_task(wait_until_disk_reserve_reached(directory))
        done, _pending = await asyncio.wait(
            {communication, disk_guard},
            return_when=asyncio.FIRST_COMPLETED,
        )
        reserve_reached = disk_guard in done and communication not in done
        if reserve_reached:
            await cls._stop_process(process)
        _stdout, stderr = await communication
        if not disk_guard.done():
            disk_guard.cancel()
        await asyncio.gather(disk_guard, return_exceptions=True)
        return stderr, reserve_reached

    async def get(self, source: Path, relative_path: str) -> Path:
        signature = await asyncio.to_thread(self._source_signature, source)
        key = self._cache_key(relative_path, signature)
        target = self.directory / f"{key}.mp4"
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            if await asyncio.to_thread(self._is_cached, target):
                return target

            async with self._generation_lock:
                if await asyncio.to_thread(self._is_cached, target):
                    return target

                executable = resolve_ffmpeg(self.ffmpeg)
                await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(self._cleanup, target)
                try:
                    await asyncio.to_thread(
                        ensure_disk_reserve,
                        self.directory,
                        required_bytes=signature[0],
                    )
                except InsufficientDiskSpaceError as exc:
                    raise HTTPException(status_code=507, detail=str(exc)) from exc

                temporary = self.directory / f".{key}.{secrets.token_hex(6)}.mp4"
                process: asyncio.subprocess.Process | None = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        *build_remux_command(executable, source, temporary),
                        stdout=subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stderr, reserve_reached = await self._communicate_with_disk_guard(process, self.directory)
                    if reserve_reached or b"No space left on device" in stderr:
                        raise HTTPException(status_code=507, detail="磁盘空间已达到 1 GB 保留水位")
                    if process.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
                        raise HTTPException(status_code=422, detail="录像无法无损封装为浏览器可播放的 MP4")
                    if await asyncio.to_thread(self._source_signature, source) != signature:
                        raise HTTPException(status_code=409, detail="录像仍在写入，请稍后重试")
                    await asyncio.to_thread(temporary.replace, target)
                finally:
                    if process is not None:
                        await self._stop_process(process)
                    await asyncio.to_thread(self._unlink, temporary)

            await asyncio.to_thread(self._cleanup, target)
            return target


async def delete_recording_file(
    root: Path,
    relative_path: str,
    preview_cache: RecordingPreviewCache,
) -> None:
    """Delete a recording and every completed browser-playback derivative.

    Removing the source first prevents an in-flight remux from publishing a new
    cache entry: it rechecks the source before replacing its temporary output.
    The cache key is based on the normalized relative path, so all cached
    generations of the same recording are discarded together.
    """
    normalized = await asyncio.to_thread(_delete_recording_file, root, relative_path)
    await preview_cache.discard(normalized)


def _delete_recording_directory(root: Path, relative_path: str) -> list[str]:
    root = root.resolve()
    directory, _ = resolve_recording_path(root, relative_path)
    if not directory.exists() or not directory.is_dir():
        raise HTTPException(status_code=404, detail="录像目录不存在")
    videos: list[str] = []
    for path in directory.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() in RECORDING_MEDIA_TYPES:
            videos.append(path.relative_to(root).as_posix())
    try:
        shutil.rmtree(directory)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="录像目录不存在") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法删除录像目录") from exc
    return videos


async def delete_recording_directory(
    root: Path,
    relative_path: str,
    preview_cache: RecordingPreviewCache,
) -> int:
    """Delete a directory tree and every completed remux of the recordings in it.

    Same ordering as the single-file delete: the sources go first so an
    in-flight remux cannot publish a new cache entry afterwards.
    """
    videos = await asyncio.to_thread(_delete_recording_directory, root, relative_path)
    for video in videos:
        await preview_cache.discard(video)
    return len(videos)
