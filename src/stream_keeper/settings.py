from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .platforms import LiveStreamClient

CLOUD_ARCHIVE_ROOT = "/DouYinStreamKeeper"


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
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    web_username: str = "admin"
    web_password: str = ""
    allow_insecure: bool = False
    web_workers: int = 1
    session_ttl_hours: int = 24 * 7
    login_max_attempts: int = 3
    login_window_seconds: int = 3600
    quark_cookie: str = ""
    quark_root_id: str = "0"
    quark_upload_path: str | None = CLOUD_ARCHIVE_ROOT
    wopan_access_token: str = ""
    wopan_refresh_token: str = ""
    wopan_root_id: str = "0"
    wopan_family_id: str = ""
    wopan_upload_path: str | None = CLOUD_ARCHIVE_ROOT
    upload_hour: int = 1
    upload_min_age_minutes: int = 10
    upload_timeout_seconds: int = 300
    max_concurrent_recordings: int = 3
    fetch_timeout_seconds: int = 45
    proxy: str | None = None
    douyin_cookies: str | None = None
    douyin_cookie_file: Path | None = None
    bilibili_cookies: str | None = None
    kuaishou_cookies: str | None = None
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
            # DOUYIN_BIND_ADDRESS is the name documented in .env.example, where it
            # also drives the compose host port publish. Accept it as a fallback so
            # a direct `python -m stream_keeper` run honours the same variable
            # instead of silently staying on the default.
            web_host=os.getenv("DOUYIN_WEB_HOST") or os.getenv("DOUYIN_BIND_ADDRESS") or "0.0.0.0",
            web_port=int(os.getenv("DOUYIN_WEB_PORT", "8000")),
            web_username=os.getenv("DOUYIN_WEB_USERNAME", "admin"),
            web_password=os.getenv("DOUYIN_WEB_PASSWORD", ""),
            allow_insecure=_env_bool("DOUYIN_ALLOW_INSECURE"),
            web_workers=int(os.getenv("WEB_CONCURRENCY", "1")),
            session_ttl_hours=int(os.getenv("DOUYIN_SESSION_TTL_HOURS", str(24 * 7))),
            login_max_attempts=int(os.getenv("DOUYIN_LOGIN_MAX_ATTEMPTS", "3")),
            login_window_seconds=int(os.getenv("DOUYIN_LOGIN_WINDOW_SECONDS", "3600")),
            quark_cookie=os.getenv("DOUYIN_QUARK_COOKIE", ""),
            quark_root_id=os.getenv("DOUYIN_QUARK_ROOT_ID", "0"),
            quark_upload_path=CLOUD_ARCHIVE_ROOT,
            wopan_access_token=os.getenv("DOUYIN_WOPAN_ACCESS_TOKEN", ""),
            wopan_refresh_token=os.getenv("DOUYIN_WOPAN_REFRESH_TOKEN", ""),
            wopan_root_id=os.getenv("DOUYIN_WOPAN_ROOT_ID", "0"),
            wopan_family_id=os.getenv("DOUYIN_WOPAN_FAMILY_ID", ""),
            wopan_upload_path=CLOUD_ARCHIVE_ROOT,
            upload_hour=int(os.getenv("DOUYIN_UPLOAD_HOUR", "1")),
            upload_min_age_minutes=int(os.getenv("DOUYIN_UPLOAD_MIN_AGE_MINUTES", "10")),
            upload_timeout_seconds=int(os.getenv("DOUYIN_UPLOAD_TIMEOUT_SECONDS", "300")),
            max_concurrent_recordings=int(os.getenv("DOUYIN_MAX_CONCURRENT_RECORDINGS", "3")),
            fetch_timeout_seconds=int(os.getenv("DOUYIN_FETCH_TIMEOUT_SECONDS", "45")),
            proxy=os.getenv("DOUYIN_PROXY") or None,
            douyin_cookies=os.getenv("DOUYIN_COOKIE") or None,
            douyin_cookie_file=Path(cookie_file_value).expanduser() if cookie_file_value else None,
            bilibili_cookies=os.getenv("BILIBILI_COOKIE") or None,
            kuaishou_cookies=os.getenv("KUAISHOU_COOKIE") or None,
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
        if not 0 <= self.upload_hour <= 23:
            raise RuntimeError("DOUYIN_UPLOAD_HOUR 必须在 0 到 23 之间")
        if not 0 <= self.upload_min_age_minutes <= 24 * 60:
            raise RuntimeError("DOUYIN_UPLOAD_MIN_AGE_MINUTES 必须在 0 到 1440 之间")
        if self.upload_timeout_seconds < 30:
            raise RuntimeError("DOUYIN_UPLOAD_TIMEOUT_SECONDS 不能小于 30")
        self._validate_cloud_uploads()
        if self.max_concurrent_recordings < 1:
            raise RuntimeError("DOUYIN_MAX_CONCURRENT_RECORDINGS 必须大于 0")
        if self.fetch_timeout_seconds < 5:
            raise RuntimeError("DOUYIN_FETCH_TIMEOUT_SECONDS 不能小于 5")
        if not 1 <= self.web_port <= 65535:
            raise RuntimeError("DOUYIN_WEB_PORT 必须在 1 到 65535 之间")
        if self.douyin_cookie_file and not self.douyin_cookie_file.is_file():
            raise RuntimeError(f"Cookie 文件不存在: {self.douyin_cookie_file}")
        for name, value in (
            ("DOUYIN_COOKIE", self.load_douyin_cookies()),
            ("BILIBILI_COOKIE", self.bilibili_cookies),
            ("KUAISHOU_COOKIE", self.kuaishou_cookies),
        ):
            if value and ("\r" in value or "\n" in value):
                raise RuntimeError(f"{name} 不能包含换行符")
        if self.validate_binaries and not shutil.which(self.ffmpeg):
            raise RuntimeError(f"找不到 FFmpeg: {self.ffmpeg}")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def upload_targets(self) -> tuple[tuple[str, str], ...]:
        targets: list[tuple[str, str]] = []
        if self.quark_cookie:
            targets.append(("quark", CLOUD_ARCHIVE_ROOT))
        if self.wopan_access_token or self.wopan_refresh_token:
            targets.append(("wopan", CLOUD_ARCHIVE_ROOT))
        return tuple(targets)

    @property
    def upload_enabled(self) -> bool:
        return bool(self.upload_targets)

    def _validate_cloud_uploads(self) -> None:
        quark_configured = bool(self.quark_cookie or self.quark_root_id != "0")
        if quark_configured:
            if not self.quark_cookie:
                raise RuntimeError("启用夸克上传必须设置 DOUYIN_QUARK_COOKIE")
            if not self.quark_root_id.strip():
                raise RuntimeError("DOUYIN_QUARK_ROOT_ID 不能为空")
            if "\r" in self.quark_cookie or "\n" in self.quark_cookie:
                raise RuntimeError("DOUYIN_QUARK_COOKIE 不能包含换行符")

        wopan_configured = bool(
            self.wopan_access_token or self.wopan_refresh_token or self.wopan_family_id or self.wopan_root_id != "0"
        )
        if wopan_configured:
            if not (self.wopan_access_token or self.wopan_refresh_token):
                raise RuntimeError("启用联通云盘上传必须设置 access/refresh token 至少一个")
            if self.wopan_access_token and len(self.wopan_access_token.encode("utf-8")) < 16:
                raise RuntimeError("DOUYIN_WOPAN_ACCESS_TOKEN 长度不能小于 16 字节")
            if not self.wopan_root_id.strip():
                raise RuntimeError("DOUYIN_WOPAN_ROOT_ID 不能为空")
            for name, value in (
                ("DOUYIN_WOPAN_ACCESS_TOKEN", self.wopan_access_token),
                ("DOUYIN_WOPAN_REFRESH_TOKEN", self.wopan_refresh_token),
            ):
                if "\r" in value or "\n" in value:
                    raise RuntimeError(f"{name} 不能包含换行符")

    def load_douyin_cookies(self) -> str | None:
        if self.douyin_cookie_file:
            value = self.douyin_cookie_file.read_text(encoding="utf-8").strip()
            return value or None
        return self.douyin_cookies

    def create_client(self) -> LiveStreamClient:
        return LiveStreamClient(
            proxy=self.proxy,
            douyin_cookies=self.load_douyin_cookies(),
            bilibili_cookies=self.bilibili_cookies,
            kuaishou_cookies=self.kuaishou_cookies,
            timeout=float(self.fetch_timeout_seconds),
        )
