from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..settings import CLOUD_ARCHIVE_ROOT, Settings


@dataclass(frozen=True, slots=True)
class CloudArchiveConfig:
    quark_enabled: bool = False
    quark_cookie: str = ""
    quark_root_id: str = "0"
    quark_upload_path: str = CLOUD_ARCHIVE_ROOT
    wopan_enabled: bool = False
    wopan_access_token: str = ""
    wopan_refresh_token: str = ""
    wopan_root_id: str = "0"
    wopan_family_id: str = ""
    wopan_upload_path: str = CLOUD_ARCHIVE_ROOT
    upload_hour: int = 1
    upload_min_age_minutes: int = 10
    upload_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        object.__setattr__(self, "quark_upload_path", CLOUD_ARCHIVE_ROOT)
        object.__setattr__(self, "wopan_upload_path", CLOUD_ARCHIVE_ROOT)

    @classmethod
    def from_settings(cls, settings: Settings) -> CloudArchiveConfig:
        return cls(
            quark_enabled=bool(settings.quark_cookie),
            quark_cookie=settings.quark_cookie,
            quark_root_id=settings.quark_root_id,
            quark_upload_path=CLOUD_ARCHIVE_ROOT,
            wopan_enabled=bool(settings.wopan_access_token or settings.wopan_refresh_token),
            wopan_access_token=settings.wopan_access_token,
            wopan_refresh_token=settings.wopan_refresh_token,
            wopan_root_id=settings.wopan_root_id,
            wopan_family_id=settings.wopan_family_id,
            wopan_upload_path=CLOUD_ARCHIVE_ROOT,
            upload_hour=settings.upload_hour,
            upload_min_age_minutes=settings.upload_min_age_minutes,
            upload_timeout_seconds=settings.upload_timeout_seconds,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CloudArchiveConfig:
        fields = cls.__dataclass_fields__
        return cls(**{name: value[name] for name in fields if name in value})

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def targets(self) -> tuple[tuple[str, str], ...]:
        targets: list[tuple[str, str]] = []
        if self.quark_enabled:
            targets.append(("quark", self.quark_upload_path))
        if self.wopan_enabled:
            targets.append(("wopan", self.wopan_upload_path))
        return tuple(targets)

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    @staticmethod
    def _validate_no_control_characters(name: str, value: str) -> None:
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"{name} 不能包含控制字符")

    def validate(self) -> None:
        if not 0 <= self.upload_hour <= 23:
            raise ValueError("每日上传小时必须在 0 到 23 之间")
        if not 0 <= self.upload_min_age_minutes <= 24 * 60:
            raise ValueError("文件稳定时间必须在 0 到 1440 分钟之间")
        if self.upload_timeout_seconds < 30:
            raise ValueError("上传网络超时不能小于 30 秒")

        for name, value in (
            ("夸克 Cookie", self.quark_cookie),
            ("夸克 Root ID", self.quark_root_id),
            ("联通 access token", self.wopan_access_token),
            ("联通 refresh token", self.wopan_refresh_token),
            ("联通 Root ID", self.wopan_root_id),
            ("联通 Family ID", self.wopan_family_id),
        ):
            self._validate_no_control_characters(name, value)
        if not self.quark_root_id.strip():
            raise ValueError("夸克 Root ID 不能为空")
        if not self.wopan_root_id.strip():
            raise ValueError("联通云盘 Root ID 不能为空")
        if self.wopan_access_token and len(self.wopan_access_token.encode()) < 16:
            raise ValueError("联通云盘 access token 长度不能小于 16 字节")
        if self.quark_enabled and not self.quark_cookie:
            raise ValueError("启用夸克上传前必须填写 Cookie")
        if self.wopan_enabled and not (self.wopan_access_token or self.wopan_refresh_token):
            raise ValueError("启用联通云盘上传前必须填写至少一个 token")
