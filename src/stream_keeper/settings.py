from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .platforms import LiveStreamClient

CLOUD_ARCHIVE_ROOT = "/DouYinStreamKeeper"
UPLOAD_MODE_SCHEDULED = "scheduled"
UPLOAD_MODE_RECORDING_COMPLETED = "recording_completed"
UPLOAD_MODES = frozenset({UPLOAD_MODE_SCHEDULED, UPLOAD_MODE_RECORDING_COMPLETED})
ENV_PREFIX = "STREAM_KEEPER_"
DEFAULT_MAX_CONCURRENT_RECORDINGS = 3
MAX_RECORDING_CONCURRENCY = 100


def _env_name(suffix: str) -> str:
    return f"{ENV_PREFIX}{suffix}"


def _env(suffix: str, default: str = "") -> str:
    """Read only the canonical application namespace; legacy aliases are intentionally unsupported."""
    return os.getenv(_env_name(suffix), default)


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    recordings_dir: Path = Path("data/recordings")
    database_path: Path = Path("data/tasks.db")
    web_host: str = "0.0.0.0"
    web_port: int = 8000
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
    baidu_access_token: str = ""
    baidu_refresh_token: str = ""
    baidu_client_id: str = ""
    baidu_client_secret: str = ""
    pan115_cookie: str = ""
    pan115_access_token: str = ""
    pan115_refresh_token: str = ""
    pan115_root_id: str = "0"
    guangya_access_token: str = ""
    guangya_refresh_token: str = ""
    guangya_client_id: str = ""
    guangya_device_id: str = ""
    guangya_root_id: str = ""
    upload_mode: str = UPLOAD_MODE_SCHEDULED
    upload_hour: int = 1
    upload_min_age_minutes: int = 5
    upload_timeout_seconds: int = 300
    max_concurrent_recordings: int = DEFAULT_MAX_CONCURRENT_RECORDINGS
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
        data_dir = Path(_env("DATA_DIR", "data")).expanduser()
        recordings_dir = Path(_env("RECORDINGS_DIR", str(data_dir / "recordings"))).expanduser()
        database_path = Path(_env("DATABASE_PATH", str(data_dir / "tasks.db"))).expanduser()
        cookie_file_value = _env("DOUYIN_COOKIE_FILE")
        return cls(
            data_dir=data_dir,
            recordings_dir=recordings_dir,
            database_path=database_path,
            # BIND_ADDRESS also drives the Compose host port publish. Honour it
            # when running the Python entrypoint directly unless WEB_HOST is set.
            web_host=_env("WEB_HOST") or _env("BIND_ADDRESS") or "0.0.0.0",
            web_port=int(_env("WEB_PORT", "8000")),
            web_workers=int(_env("WEB_WORKERS", "1")),
            session_ttl_hours=int(_env("SESSION_TTL_HOURS", str(24 * 7))),
            login_max_attempts=int(_env("LOGIN_MAX_ATTEMPTS", "3")),
            login_window_seconds=int(_env("LOGIN_WINDOW_SECONDS", "3600")),
            quark_cookie=_env("QUARK_COOKIE"),
            quark_root_id=_env("QUARK_ROOT_ID", "0"),
            quark_upload_path=CLOUD_ARCHIVE_ROOT,
            wopan_access_token=_env("WOPAN_ACCESS_TOKEN"),
            wopan_refresh_token=_env("WOPAN_REFRESH_TOKEN"),
            wopan_root_id=_env("WOPAN_ROOT_ID", "0"),
            wopan_family_id=_env("WOPAN_FAMILY_ID"),
            wopan_upload_path=CLOUD_ARCHIVE_ROOT,
            baidu_access_token=_env("BAIDU_ACCESS_TOKEN"),
            baidu_refresh_token=_env("BAIDU_REFRESH_TOKEN"),
            baidu_client_id=_env("BAIDU_CLIENT_ID"),
            baidu_client_secret=_env("BAIDU_CLIENT_SECRET"),
            pan115_cookie=_env("115_COOKIE"),
            pan115_access_token=_env("115_ACCESS_TOKEN"),
            pan115_refresh_token=_env("115_REFRESH_TOKEN"),
            pan115_root_id=_env("115_ROOT_ID", "0"),
            guangya_access_token=_env("GUANGYA_ACCESS_TOKEN"),
            guangya_refresh_token=_env("GUANGYA_REFRESH_TOKEN"),
            guangya_client_id=_env("GUANGYA_CLIENT_ID"),
            guangya_device_id=_env("GUANGYA_DEVICE_ID"),
            guangya_root_id=_env("GUANGYA_ROOT_ID"),
            upload_mode=_env("UPLOAD_MODE", UPLOAD_MODE_SCHEDULED).strip().lower(),
            upload_hour=int(_env("UPLOAD_HOUR", "1")),
            upload_min_age_minutes=int(_env("UPLOAD_MIN_AGE_MINUTES", "5")),
            upload_timeout_seconds=int(_env("UPLOAD_TIMEOUT_SECONDS", "300")),
            max_concurrent_recordings=int(
                _env("MAX_CONCURRENT_RECORDINGS", str(DEFAULT_MAX_CONCURRENT_RECORDINGS))
            ),
            fetch_timeout_seconds=int(_env("FETCH_TIMEOUT_SECONDS", "45")),
            proxy=_env("PROXY") or None,
            douyin_cookies=_env("DOUYIN_COOKIE") or None,
            douyin_cookie_file=Path(cookie_file_value).expanduser() if cookie_file_value else None,
            bilibili_cookies=_env("BILIBILI_COOKIE") or None,
            kuaishou_cookies=_env("KUAISHOU_COOKIE") or None,
            ffmpeg=_env("FFMPEG", "ffmpeg"),
        )

    def prepare(self) -> None:
        if self.web_workers != 1:
            raise RuntimeError("录制调度器只支持单 Web worker，请将 STREAM_KEEPER_WEB_WORKERS 设置为 1")
        if not 1 <= self.session_ttl_hours <= 24 * 30:
            raise RuntimeError("STREAM_KEEPER_SESSION_TTL_HOURS 必须在 1 到 720 之间")
        if not 1 <= self.login_max_attempts <= 100:
            raise RuntimeError("STREAM_KEEPER_LOGIN_MAX_ATTEMPTS 必须在 1 到 100 之间")
        if not 10 <= self.login_window_seconds <= 86400:
            raise RuntimeError("STREAM_KEEPER_LOGIN_WINDOW_SECONDS 必须在 10 到 86400 之间")
        if self.upload_mode not in UPLOAD_MODES:
            raise RuntimeError("STREAM_KEEPER_UPLOAD_MODE 必须是 scheduled 或 recording_completed")
        if not 0 <= self.upload_hour <= 23:
            raise RuntimeError("STREAM_KEEPER_UPLOAD_HOUR 必须在 0 到 23 之间")
        if not 0 <= self.upload_min_age_minutes <= 24 * 60:
            raise RuntimeError("STREAM_KEEPER_UPLOAD_MIN_AGE_MINUTES 必须在 0 到 1440 之间")
        if self.upload_timeout_seconds < 30:
            raise RuntimeError("STREAM_KEEPER_UPLOAD_TIMEOUT_SECONDS 不能小于 30")
        self._validate_cloud_uploads()
        if not 1 <= self.max_concurrent_recordings <= MAX_RECORDING_CONCURRENCY:
            raise RuntimeError(
                f"STREAM_KEEPER_MAX_CONCURRENT_RECORDINGS 必须在 1 到 {MAX_RECORDING_CONCURRENCY} 之间"
            )
        if self.fetch_timeout_seconds < 5:
            raise RuntimeError("STREAM_KEEPER_FETCH_TIMEOUT_SECONDS 不能小于 5")
        if not 1 <= self.web_port <= 65535:
            raise RuntimeError("STREAM_KEEPER_WEB_PORT 必须在 1 到 65535 之间")
        if self.douyin_cookie_file and not self.douyin_cookie_file.is_file():
            raise RuntimeError(f"Cookie 文件不存在: {self.douyin_cookie_file}")
        for name, value in (
            ("STREAM_KEEPER_DOUYIN_COOKIE", self.load_douyin_cookies()),
            ("STREAM_KEEPER_BILIBILI_COOKIE", self.bilibili_cookies),
            ("STREAM_KEEPER_KUAISHOU_COOKIE", self.kuaishou_cookies),
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
        if self.baidu_access_token or (self.baidu_refresh_token and self.baidu_client_id and self.baidu_client_secret):
            targets.append(("baidu", CLOUD_ARCHIVE_ROOT))
        if self.pan115_cookie or self.pan115_access_token or self.pan115_refresh_token:
            targets.append(("pan115", CLOUD_ARCHIVE_ROOT))
        if self.guangya_client_id and (self.guangya_access_token or self.guangya_refresh_token):
            targets.append(("guangya", CLOUD_ARCHIVE_ROOT))
        return tuple(targets)

    @property
    def upload_enabled(self) -> bool:
        return bool(self.upload_targets)

    def _validate_cloud_uploads(self) -> None:
        quark_configured = bool(self.quark_cookie or self.quark_root_id != "0")
        if quark_configured:
            if not self.quark_cookie:
                raise RuntimeError("启用夸克上传必须设置 STREAM_KEEPER_QUARK_COOKIE")
            if not self.quark_root_id.strip():
                raise RuntimeError("STREAM_KEEPER_QUARK_ROOT_ID 不能为空")

        wopan_configured = bool(
            self.wopan_access_token or self.wopan_refresh_token or self.wopan_family_id or self.wopan_root_id != "0"
        )
        if wopan_configured:
            if not (self.wopan_access_token or self.wopan_refresh_token):
                raise RuntimeError("启用联通云盘上传必须设置 access/refresh token 至少一个")
            if self.wopan_access_token and len(self.wopan_access_token.encode("utf-8")) < 16:
                raise RuntimeError("STREAM_KEEPER_WOPAN_ACCESS_TOKEN 长度不能小于 16 字节")
            if not self.wopan_root_id.strip():
                raise RuntimeError("STREAM_KEEPER_WOPAN_ROOT_ID 不能为空")

        if not self.pan115_root_id.strip():
            raise RuntimeError("STREAM_KEEPER_115_ROOT_ID 不能为空")
        if self.pan115_cookie and ("\r" in self.pan115_cookie or "\n" in self.pan115_cookie):
            raise RuntimeError("STREAM_KEEPER_115_COOKIE 不能包含换行符")
        if bool(self.baidu_client_id) != bool(self.baidu_client_secret):
            raise RuntimeError("STREAM_KEEPER_BAIDU_CLIENT_ID 和 STREAM_KEEPER_BAIDU_CLIENT_SECRET 必须同时填写")
        if self.baidu_refresh_token and not (self.baidu_client_id and self.baidu_client_secret):
            raise RuntimeError(
                "配置 STREAM_KEEPER_BAIDU_REFRESH_TOKEN 时必须同时配置 Client ID 和 Client Secret"
            )
        guangya_configured = bool(self.guangya_access_token or self.guangya_refresh_token or self.guangya_client_id)
        if guangya_configured and not (
            self.guangya_client_id and (self.guangya_access_token or self.guangya_refresh_token)
        ):
            raise RuntimeError("启用光鸭网盘必须配置 Client ID 和 Access/Refresh Token")

        for name, value in (
            ("STREAM_KEEPER_QUARK_COOKIE", self.quark_cookie),
            ("STREAM_KEEPER_WOPAN_ACCESS_TOKEN", self.wopan_access_token),
            ("STREAM_KEEPER_WOPAN_REFRESH_TOKEN", self.wopan_refresh_token),
            ("STREAM_KEEPER_BAIDU_ACCESS_TOKEN", self.baidu_access_token),
            ("STREAM_KEEPER_BAIDU_REFRESH_TOKEN", self.baidu_refresh_token),
            ("STREAM_KEEPER_BAIDU_CLIENT_ID", self.baidu_client_id),
            ("STREAM_KEEPER_BAIDU_CLIENT_SECRET", self.baidu_client_secret),
            ("STREAM_KEEPER_115_COOKIE", self.pan115_cookie),
            ("STREAM_KEEPER_115_ACCESS_TOKEN", self.pan115_access_token),
            ("STREAM_KEEPER_115_REFRESH_TOKEN", self.pan115_refresh_token),
            ("STREAM_KEEPER_GUANGYA_ACCESS_TOKEN", self.guangya_access_token),
            ("STREAM_KEEPER_GUANGYA_REFRESH_TOKEN", self.guangya_refresh_token),
            ("STREAM_KEEPER_GUANGYA_CLIENT_ID", self.guangya_client_id),
            ("STREAM_KEEPER_GUANGYA_DEVICE_ID", self.guangya_device_id),
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
