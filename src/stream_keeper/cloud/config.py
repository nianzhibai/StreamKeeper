from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..settings import CLOUD_ARCHIVE_ROOT, UPLOAD_MODE_SCHEDULED, UPLOAD_MODES, Settings

CLOUD_PROVIDER_ORDER = ("quark", "wopan", "baidu", "pan115", "guangya")
QR_LOGIN_PROVIDERS = frozenset({"quark", "wopan", "pan115"})


@dataclass(frozen=True, slots=True)
class CloudProviderSpec:
    name: str
    label: str
    credential_keys: tuple[str, ...]
    option_defaults: tuple[tuple[str, str], ...] = ()
    supports_qr_login: bool = False

    @property
    def default_options(self) -> dict[str, str]:
        return dict(self.option_defaults)


CLOUD_PROVIDER_SPECS: dict[str, CloudProviderSpec] = {
    "quark": CloudProviderSpec(
        name="quark",
        label="夸克网盘",
        credential_keys=("cookie",),
        option_defaults=(("root_id", "0"),),
        supports_qr_login=True,
    ),
    "wopan": CloudProviderSpec(
        name="wopan",
        label="联通云盘",
        credential_keys=("access_token", "refresh_token"),
        option_defaults=(("root_id", "0"), ("family_id", "")),
        supports_qr_login=True,
    ),
    "baidu": CloudProviderSpec(
        name="baidu",
        label="百度网盘",
        credential_keys=("access_token", "refresh_token", "client_id", "client_secret"),
    ),
    "pan115": CloudProviderSpec(
        name="pan115",
        label="115网盘",
        credential_keys=("cookie", "access_token", "refresh_token"),
        option_defaults=(("root_id", "0"),),
        supports_qr_login=True,
    ),
    "guangya": CloudProviderSpec(
        name="guangya",
        label="光鸭网盘",
        credential_keys=("access_token", "refresh_token", "client_id", "device_id"),
        option_defaults=(("root_id", ""),),
    ),
}

CLOUD_PROVIDER_LABELS = {name: spec.label for name, spec in CLOUD_PROVIDER_SPECS.items()}


@dataclass(frozen=True, slots=True)
class CloudProviderConfig:
    name: str
    enabled: bool = False
    credentials: dict[str, str] = field(default_factory=dict)
    options: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = CLOUD_PROVIDER_SPECS.get(self.name)
        if spec is None:
            raise ValueError(f"不支持的网盘类型: {self.name}")
        credentials = {key: str(value) for key, value in self.credentials.items()}
        options = spec.default_options
        options.update({key: str(value) for key, value in self.options.items()})
        object.__setattr__(self, "credentials", credentials)
        object.__setattr__(self, "options", options)

    @property
    def upload_path(self) -> str:
        return CLOUD_ARCHIVE_ROOT

    @property
    def credential_configured(self) -> bool:
        values = self.credentials
        if self.name == "quark":
            return bool(values.get("cookie"))
        if self.name == "wopan":
            return bool(values.get("access_token") or values.get("refresh_token"))
        if self.name == "baidu":
            return bool(
                values.get("access_token")
                or (values.get("refresh_token") and values.get("client_id") and values.get("client_secret"))
            )
        if self.name == "pan115":
            return bool(values.get("cookie") or values.get("access_token") or values.get("refresh_token"))
        if self.name == "guangya":
            return bool(values.get("client_id") and (values.get("access_token") or values.get("refresh_token")))
        return False

    @property
    def configured_credentials(self) -> tuple[str, ...]:
        spec = CLOUD_PROVIDER_SPECS[self.name]
        return tuple(key for key in spec.credential_keys if self.credentials.get(key))

    @staticmethod
    def _validate_no_control_characters(name: str, value: str) -> None:
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"{name} 不能包含控制字符")

    def validate(self) -> None:
        spec = CLOUD_PROVIDER_SPECS[self.name]
        unknown_credentials = set(self.credentials) - set(spec.credential_keys)
        if unknown_credentials:
            raise ValueError(f"{spec.label}包含不支持的凭据字段: {', '.join(sorted(unknown_credentials))}")
        unknown_options = set(self.options) - set(spec.default_options)
        if unknown_options:
            raise ValueError(f"{spec.label}包含不支持的配置字段: {', '.join(sorted(unknown_options))}")

        for key, value in (*self.credentials.items(), *self.options.items()):
            self._validate_no_control_characters(f"{spec.label} {key}", value)

        root_id = self.options.get("root_id")
        if self.name in {"quark", "wopan", "pan115"} and not (root_id or "").strip():
            raise ValueError(f"{spec.label} Root ID 不能为空")

        access_token = self.credentials.get("access_token", "")
        refresh_token = self.credentials.get("refresh_token", "")
        cookie = self.credentials.get("cookie", "")
        if self.name == "wopan" and access_token and len(access_token.encode()) < 16:
            raise ValueError("联通云盘 access token 长度不能小于 16 字节")
        if self.name == "pan115" and self.enabled and not cookie and not (access_token or refresh_token):
            raise ValueError("启用 115 网盘前必须填写 Cookie 或 Access/Refresh Token")
        if self.name == "baidu":
            client_id = self.credentials.get("client_id", "")
            client_secret = self.credentials.get("client_secret", "")
            if bool(client_id) != bool(client_secret):
                raise ValueError("百度网盘 Client ID 和 Client Secret 必须同时填写")
            if refresh_token and not (client_id and client_secret):
                raise ValueError("配置百度 Refresh Token 时必须填写 Client ID 和 Client Secret")
        if self.name == "guangya" and self.enabled and not self.credentials.get("client_id"):
            raise ValueError("启用光鸭网盘前必须填写 Client ID")

        if self.enabled and not self.credential_configured:
            requirements = {
                "quark": "Cookie",
                "wopan": "至少一个 Token",
                "baidu": "Access Token，或 Refresh Token 与客户端凭据",
                "pan115": "Cookie 或 Access/Refresh Token",
                "guangya": "Client ID 和 Access/Refresh Token",
            }
            raise ValueError(f"启用{spec.label}前必须填写{requirements[self.name]}")


def default_provider_configs() -> tuple[CloudProviderConfig, ...]:
    return tuple(CloudProviderConfig(name=name) for name in CLOUD_PROVIDER_ORDER)


@dataclass(frozen=True, slots=True)
class CloudArchiveConfig:
    providers: tuple[CloudProviderConfig, ...] = field(default_factory=default_provider_configs)
    upload_mode: str = UPLOAD_MODE_SCHEDULED
    upload_hour: int = 1
    upload_min_age_minutes: int = 10
    upload_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        by_name: dict[str, CloudProviderConfig] = {}
        for provider in self.providers:
            if provider.name in by_name:
                raise ValueError(f"网盘配置重复: {provider.name}")
            by_name[provider.name] = provider
        unknown = set(by_name) - set(CLOUD_PROVIDER_SPECS)
        if unknown:
            raise ValueError(f"不支持的网盘类型: {', '.join(sorted(unknown))}")
        ordered = tuple(by_name.get(name, CloudProviderConfig(name=name)) for name in CLOUD_PROVIDER_ORDER)
        object.__setattr__(self, "providers", ordered)

    @classmethod
    def from_settings(cls, settings: Settings) -> CloudArchiveConfig:
        return cls(
            providers=(
                CloudProviderConfig(
                    name="quark",
                    enabled=bool(settings.quark_cookie),
                    credentials={"cookie": settings.quark_cookie},
                    options={"root_id": settings.quark_root_id},
                ),
                CloudProviderConfig(
                    name="wopan",
                    enabled=bool(settings.wopan_access_token or settings.wopan_refresh_token),
                    credentials={
                        "access_token": settings.wopan_access_token,
                        "refresh_token": settings.wopan_refresh_token,
                    },
                    options={"root_id": settings.wopan_root_id, "family_id": settings.wopan_family_id},
                ),
                CloudProviderConfig(
                    name="baidu",
                    enabled=bool(
                        settings.baidu_access_token
                        or (settings.baidu_refresh_token and settings.baidu_client_id and settings.baidu_client_secret)
                    ),
                    credentials={
                        "access_token": settings.baidu_access_token,
                        "refresh_token": settings.baidu_refresh_token,
                        "client_id": settings.baidu_client_id,
                        "client_secret": settings.baidu_client_secret,
                    },
                ),
                CloudProviderConfig(
                    name="pan115",
                    enabled=bool(
                        settings.pan115_cookie or settings.pan115_access_token or settings.pan115_refresh_token
                    ),
                    credentials={
                        "cookie": settings.pan115_cookie,
                        "access_token": settings.pan115_access_token,
                        "refresh_token": settings.pan115_refresh_token,
                    },
                    options={"root_id": settings.pan115_root_id},
                ),
                CloudProviderConfig(
                    name="guangya",
                    enabled=bool(
                        settings.guangya_client_id and (settings.guangya_access_token or settings.guangya_refresh_token)
                    ),
                    credentials={
                        "access_token": settings.guangya_access_token,
                        "refresh_token": settings.guangya_refresh_token,
                        "client_id": settings.guangya_client_id,
                        "device_id": settings.guangya_device_id,
                    },
                    options={"root_id": settings.guangya_root_id},
                ),
            ),
            upload_mode=settings.upload_mode,
            upload_hour=settings.upload_hour,
            upload_min_age_minutes=settings.upload_min_age_minutes,
            upload_timeout_seconds=settings.upload_timeout_seconds,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CloudArchiveConfig:
        raw_providers = value.get("providers")
        providers: list[CloudProviderConfig] = []
        if isinstance(raw_providers, dict):
            for name in CLOUD_PROVIDER_ORDER:
                raw = raw_providers.get(name, {})
                if not isinstance(raw, dict):
                    continue
                credentials = raw.get("credentials", {})
                options = raw.get("options", {})
                providers.append(
                    CloudProviderConfig(
                        name=name,
                        enabled=bool(raw.get("enabled", False)),
                        credentials=credentials if isinstance(credentials, dict) else {},
                        options=options if isinstance(options, dict) else {},
                    )
                )
        else:
            # Import the pre-registry SQLite shape once, then save operations use
            # the canonical nested representation.
            providers = [
                CloudProviderConfig(
                    name="quark",
                    enabled=bool(value.get("quark_enabled", False)),
                    credentials={"cookie": str(value.get("quark_cookie", ""))},
                    options={"root_id": str(value.get("quark_root_id", "0"))},
                ),
                CloudProviderConfig(
                    name="wopan",
                    enabled=bool(value.get("wopan_enabled", False)),
                    credentials={
                        "access_token": str(value.get("wopan_access_token", "")),
                        "refresh_token": str(value.get("wopan_refresh_token", "")),
                    },
                    options={
                        "root_id": str(value.get("wopan_root_id", "0")),
                        "family_id": str(value.get("wopan_family_id", "")),
                    },
                ),
                CloudProviderConfig(
                    name="pan115",
                    enabled=bool(value.get("pan115_enabled", False)),
                    credentials={
                        "cookie": str(value.get("pan115_cookie", "")),
                        "access_token": str(value.get("pan115_access_token", "")),
                        "refresh_token": str(value.get("pan115_refresh_token", "")),
                    },
                    options={"root_id": str(value.get("pan115_root_id", "0"))},
                ),
            ]

        return cls(
            providers=tuple(providers),
            upload_mode=str(value.get("upload_mode", UPLOAD_MODE_SCHEDULED)),
            upload_hour=int(value.get("upload_hour", 1)),
            upload_min_age_minutes=int(value.get("upload_min_age_minutes", 10)),
            upload_timeout_seconds=int(value.get("upload_timeout_seconds", 300)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "providers": {
                provider.name: {
                    "enabled": provider.enabled,
                    "credentials": dict(provider.credentials),
                    "options": dict(provider.options),
                }
                for provider in self.providers
            },
            "upload_mode": self.upload_mode,
            "upload_hour": self.upload_hour,
            "upload_min_age_minutes": self.upload_min_age_minutes,
            "upload_timeout_seconds": self.upload_timeout_seconds,
        }

    def provider(self, name: str) -> CloudProviderConfig:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise ValueError(f"不支持的网盘类型: {name}")

    # Read-only aliases keep integrations written against the original Quark /
    # WoPan shape working while persistence uses the provider registry.
    @property
    def quark_enabled(self) -> bool:
        return self.provider("quark").enabled

    @property
    def quark_cookie(self) -> str:
        return self.provider("quark").credentials.get("cookie", "")

    @property
    def quark_root_id(self) -> str:
        return self.provider("quark").options["root_id"]

    @property
    def quark_upload_path(self) -> str:
        return self.provider("quark").upload_path

    @property
    def wopan_enabled(self) -> bool:
        return self.provider("wopan").enabled

    @property
    def wopan_access_token(self) -> str:
        return self.provider("wopan").credentials.get("access_token", "")

    @property
    def wopan_refresh_token(self) -> str:
        return self.provider("wopan").credentials.get("refresh_token", "")

    @property
    def wopan_root_id(self) -> str:
        return self.provider("wopan").options["root_id"]

    @property
    def wopan_family_id(self) -> str:
        return self.provider("wopan").options["family_id"]

    @property
    def wopan_upload_path(self) -> str:
        return self.provider("wopan").upload_path

    def with_provider(self, provider: CloudProviderConfig) -> CloudArchiveConfig:
        if provider.name not in CLOUD_PROVIDER_SPECS:
            raise ValueError(f"不支持的网盘类型: {provider.name}")
        return replace(
            self,
            providers=tuple(provider if current.name == provider.name else current for current in self.providers),
        )

    @property
    def targets(self) -> tuple[tuple[str, str], ...]:
        return tuple((provider.name, provider.upload_path) for provider in self.providers if provider.enabled)

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    def validate(self) -> None:
        if self.upload_mode not in UPLOAD_MODES:
            raise ValueError("上传模式必须是定时上传或录制完成后上传")
        if not 0 <= self.upload_hour <= 23:
            raise ValueError("每日上传小时必须在 0 到 23 之间")
        if not 0 <= self.upload_min_age_minutes <= 24 * 60:
            raise ValueError("文件稳定时间必须在 0 到 1440 分钟之间")
        if self.upload_timeout_seconds < 30:
            raise ValueError("上传网络超时不能小于 30 秒")
        for provider in self.providers:
            provider.validate()
