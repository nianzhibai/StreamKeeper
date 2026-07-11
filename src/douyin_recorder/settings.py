from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .client import DouyinClient


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    recordings_dir: Path = Path("data/recordings")
    database_path: Path = Path("data/tasks.db")
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_username: str = "admin"
    web_password: str = ""
    allow_insecure: bool = False
    web_workers: int = 1
    session_ttl_hours: int = 24
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    max_concurrent_recordings: int = 3
    fetch_timeout_seconds: int = 45
    proxy: str | None = None
    cookies: str | None = None
    cookie_file: Path | None = None
    ffmpeg: str = "ffmpeg"
    validate_binaries: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("DOUYIN_DATA_DIR", "data")).expanduser()
        recordings_dir = Path(os.getenv("DOUYIN_RECORDINGS_DIR", str(data_dir / "recordings"))).expanduser()
        database_path = Path(os.getenv("DOUYIN_DATABASE_PATH", str(data_dir / "tasks.db"))).expanduser()
        cookie_file_value = os.getenv("DOUYIN_COOKIE_FILE")
        return cls(
            data_dir=data_dir,
            recordings_dir=recordings_dir,
            database_path=database_path,
            web_host=os.getenv("DOUYIN_WEB_HOST", "127.0.0.1"),
            web_port=int(os.getenv("DOUYIN_WEB_PORT", "8000")),
            web_username=os.getenv("DOUYIN_WEB_USERNAME", "admin"),
            web_password=os.getenv("DOUYIN_WEB_PASSWORD", ""),
            allow_insecure=_env_bool("DOUYIN_ALLOW_INSECURE"),
            web_workers=int(os.getenv("WEB_CONCURRENCY", "1")),
            session_ttl_hours=int(os.getenv("DOUYIN_SESSION_TTL_HOURS", "24")),
            login_max_attempts=int(os.getenv("DOUYIN_LOGIN_MAX_ATTEMPTS", "5")),
            login_window_seconds=int(os.getenv("DOUYIN_LOGIN_WINDOW_SECONDS", "300")),
            max_concurrent_recordings=int(os.getenv("DOUYIN_MAX_CONCURRENT_RECORDINGS", "3")),
            fetch_timeout_seconds=int(os.getenv("DOUYIN_FETCH_TIMEOUT_SECONDS", "45")),
            proxy=os.getenv("DOUYIN_PROXY") or None,
            cookies=os.getenv("DOUYIN_COOKIE") or None,
            cookie_file=Path(cookie_file_value).expanduser() if cookie_file_value else None,
            ffmpeg=os.getenv("FFMPEG", "ffmpeg"),
        )

    def prepare(self) -> None:
        if not self.web_password and not self.allow_insecure:
            raise RuntimeError("必须设置 DOUYIN_WEB_PASSWORD；仅限本地调试时可设置 DOUYIN_ALLOW_INSECURE=true")
        if self.web_password and len(self.web_password) < 10:
            raise RuntimeError("DOUYIN_WEB_PASSWORD 至少需要 10 个字符")
        if self.web_workers != 1:
            raise RuntimeError("录制调度器只支持单 Web worker，请将 WEB_CONCURRENCY 设置为 1")
        if not 1 <= self.session_ttl_hours <= 24 * 30:
            raise RuntimeError("DOUYIN_SESSION_TTL_HOURS 必须在 1 到 720 之间")
        if not 1 <= self.login_max_attempts <= 100:
            raise RuntimeError("DOUYIN_LOGIN_MAX_ATTEMPTS 必须在 1 到 100 之间")
        if not 10 <= self.login_window_seconds <= 86400:
            raise RuntimeError("DOUYIN_LOGIN_WINDOW_SECONDS 必须在 10 到 86400 之间")
        if self.max_concurrent_recordings < 1:
            raise RuntimeError("DOUYIN_MAX_CONCURRENT_RECORDINGS 必须大于 0")
        if self.fetch_timeout_seconds < 5:
            raise RuntimeError("DOUYIN_FETCH_TIMEOUT_SECONDS 不能小于 5")
        if not 1 <= self.web_port <= 65535:
            raise RuntimeError("DOUYIN_WEB_PORT 必须在 1 到 65535 之间")
        if self.cookie_file and not self.cookie_file.is_file():
            raise RuntimeError(f"Cookie 文件不存在: {self.cookie_file}")
        if self.validate_binaries and not shutil.which(self.ffmpeg):
            raise RuntimeError(f"找不到 FFmpeg: {self.ffmpeg}")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def load_cookies(self) -> str | None:
        if self.cookie_file:
            value = self.cookie_file.read_text(encoding="utf-8").strip()
            return value or None
        return self.cookies

    def create_client(self) -> DouyinClient:
        return DouyinClient(proxy=self.proxy, cookies=self.load_cookies())
