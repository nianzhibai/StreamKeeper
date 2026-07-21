from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import FFmpegNotFoundError, FFmpegRecordingError
from .ffmpeg import FORMAT_NAMES, SOURCE_NAMES, build_ffmpeg_command, choose_source
from .models import LiveInfo, RecordingResult

logger = logging.getLogger(__name__)

ProcessFactory = Callable[..., object]
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HTTP_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_name(value: str | None, fallback: str = "douyin_live", max_length: int = 80) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", (value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip(" ._")
    cleaned = cleaned[:max_length].rstrip(" ._") or fallback
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def redact_stream_urls(message: str) -> str:
    return _HTTP_URL.sub("[stream-url]", message)


def create_output_path(
    output_dir: str | Path,
    info: LiveInfo,
    *,
    output_format: str,
    segment_seconds: int = 0,
    name: str | None = None,
    now: datetime | None = None,
) -> Path:
    anchor = sanitize_name(info.anchor_name)
    recording_started_at = now or datetime.now()
    recording_date = recording_started_at.strftime("%Y-%m-%d")
    target_dir = Path(output_dir).expanduser() / anchor / recording_date
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = recording_started_at.strftime("%Y-%m-%d_%H-%M-%S")
    prefix = sanitize_name(name, fallback=f"{anchor}_{timestamp}") if name else f"{anchor}_{timestamp}"
    suffix = f"_%03d.{output_format}" if segment_seconds else f".{output_format}"
    counter = 0
    while True:
        current_prefix = prefix if counter == 0 else f"{prefix}_{counter}"
        candidate = target_dir / f"{current_prefix}{suffix}"
        segment_prefix = f"{current_prefix}_"
        segment_suffix = f".{output_format}"
        has_existing_segments = segment_seconds and any(
            path.name.startswith(segment_prefix) and path.name.endswith(segment_suffix) for path in target_dir.iterdir()
        )
        if not candidate.exists() and not has_existing_segments:
            return candidate
        counter += 1


@dataclass(frozen=True, slots=True)
class RecorderOptions:
    output_dir: str | Path = "recordings"
    output_format: str = "ts"
    source: str = "auto"
    segment_seconds: int = 0
    segment_count: int = 0
    proxy: str | None = None
    ffmpeg: str = "ffmpeg"
    name: str | None = None
    ffmpeg_loglevel: str = "warning"

    def __post_init__(self) -> None:
        if self.output_format.lower() not in FORMAT_NAMES:
            raise ValueError(f"不支持的保存格式: {self.output_format}")
        if self.source.lower() not in SOURCE_NAMES:
            raise ValueError(f"不支持的直播源类型: {self.source}")
        if self.segment_seconds < 0:
            raise ValueError("segment_seconds 不能小于 0")
        if self.segment_count < 0:
            raise ValueError("segment_count 不能小于 0")
        if self.segment_count and not self.segment_seconds:
            raise ValueError("设置段数时，分段时长必须大于 0")


class Recorder:
    def __init__(
        self,
        options: RecorderOptions | None = None,
        *,
        process_factory: ProcessFactory | None = None,
        executable_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.options = options or RecorderOptions()
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._executable_resolver = executable_resolver or shutil.which
        self._process: asyncio.subprocess.Process | None = None
        self._current_output_path: Path | None = None
        self._progress_seconds = 0.0

    @property
    def current_output_path(self) -> Path | None:
        return self._current_output_path

    @property
    def progress_seconds(self) -> float:
        return self._progress_seconds

    def _resolve_ffmpeg(self) -> str:
        resolved = self._executable_resolver(self.options.ffmpeg)
        if not resolved:
            raise FFmpegNotFoundError(
                f"找不到 FFmpeg: {self.options.ffmpeg!r}。请先安装 FFmpeg，或通过 --ffmpeg 指定路径。"
            )
        return resolved

    async def record(self, info: LiveInfo) -> RecordingResult:
        if not info.is_live:
            raise FFmpegRecordingError("直播间当前未开播，不能启动录制")

        executable = self._resolve_ffmpeg()
        selected_source = choose_source(info, self.options.source)
        output_path = create_output_path(
            self.options.output_dir,
            info,
            output_format=self.options.output_format.lower(),
            segment_seconds=self.options.segment_seconds,
            name=self.options.name,
        )
        self._current_output_path = output_path
        self._progress_seconds = 0.0
        command = build_ffmpeg_command(
            selected_source.url,
            output_path,
            output_format=self.options.output_format,
            segment_seconds=self.options.segment_seconds,
            segment_count=self.options.segment_count,
            proxy=self.options.proxy,
            executable=executable,
            loglevel=self.options.ffmpeg_loglevel,
        )

        logger.info("开始录制：主播=%s，源=%s，文件=%s", info.anchor_name or "未知", selected_source.kind, output_path)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = await self._process_factory(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise FFmpegRecordingError(f"无法启动 FFmpeg: {exc}") from exc
        self._process = process  # type: ignore[assignment]
        stderr = getattr(process, "stderr", None)
        stderr_task = asyncio.create_task(self._consume_stderr(stderr)) if stderr is not None else None
        stdout = getattr(process, "stdout", None)
        progress_task = asyncio.create_task(self._consume_progress(stdout)) if stdout is not None else None
        progress_seconds = 0.0
        try:
            return_code = await process.wait()  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            await asyncio.shield(self.stop())
            raise
        finally:
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            if progress_task is not None:
                progress_result = await asyncio.gather(progress_task, return_exceptions=True)
                if progress_result and isinstance(progress_result[0], float):
                    progress_seconds = progress_result[0]
                    self._progress_seconds = progress_seconds
            self._process = None

        if return_code not in {0, 255}:
            raise FFmpegRecordingError(f"FFmpeg 录制失败，退出码: {return_code}")

        limit_seconds = self.options.segment_seconds * self.options.segment_count
        limit_tolerance = min(2.0, limit_seconds * 0.01)
        limit_reached = bool(limit_seconds and progress_seconds >= limit_seconds - limit_tolerance)
        logger.info("录制结束：%s", output_path)
        return RecordingResult(str(output_path), selected_source, return_code, limit_reached)

    @staticmethod
    async def _consume_stderr(stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            message = redact_stream_urls(line.decode(errors="replace").strip())
            if message:
                logger.warning("FFmpeg: %s", message)

    async def _consume_progress(self, stream: asyncio.StreamReader) -> float:
        max_seconds = 0.0
        while line := await stream.readline():
            key, separator, value = line.decode(errors="replace").strip().partition("=")
            if separator and key == "out_time_us":
                try:
                    max_seconds = max(max_seconds, int(value) / 1_000_000)
                    self._progress_seconds = max_seconds
                except ValueError:
                    continue
        return max_seconds

    async def stop(self, timeout: float = 15.0) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return

        try:
            if process.stdin is not None:
                process.stdin.write(b"q\n")
                await process.stdin.drain()
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return
        except (BrokenPipeError, ConnectionResetError):
            await process.wait()
            return
        except asyncio.TimeoutError:
            logger.warning("FFmpeg 未及时退出，准备终止进程")

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
