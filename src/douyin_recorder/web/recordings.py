from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import time

from fastapi import HTTPException

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
                        size=None,
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

    @staticmethod
    def _source_signature(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="录像文件不存在") from exc
        return stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _cache_key(relative_path: str, signature: tuple[int, int]) -> str:
        value = f"{relative_path}\0{signature[0]}\0{signature[1]}".encode()
        return hashlib.sha256(value).hexdigest()

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

    async def get(self, source: Path, relative_path: str) -> Path:
        signature = await asyncio.to_thread(self._source_signature, source)
        key = self._cache_key(relative_path, signature)
        target = self.directory / f"{key}.mp4"
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            if await asyncio.to_thread(self._is_cached, target):
                return target

            executable = resolve_ffmpeg(self.ffmpeg)
            await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
            temporary = self.directory / f".{key}.{secrets.token_hex(6)}.mp4"
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *build_remux_command(executable, source, temporary),
                    stdout=subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await process.communicate()
                if process.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
                    if b"No space left on device" in stderr:
                        raise HTTPException(status_code=507, detail="磁盘空间不足，无法准备播放缓存")
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
