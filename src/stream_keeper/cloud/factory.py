from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from .baidu import BaiduNetdiskClient
from .base import CloudUploadClient, CloudUploadError, CredentialUpdate
from .config import CloudProviderConfig
from .guangya import GuangYaPanClient
from .pan115 import Pan115Client
from .pan115_cookie import Pan115CookieClient
from .quark import QuarkClient
from .wopan import WoPanClient

_Sleep = Callable[[float], Awaitable[None]]


def create_cloud_client(
    provider: CloudProviderConfig,
    credentials: dict[str, str],
    *,
    timeout_seconds: int,
    on_credential_update: CredentialUpdate | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: _Sleep = asyncio.sleep,
) -> CloudUploadClient:
    """Build a provider client from the registry's normalized configuration."""

    values = credentials
    if provider.name == "quark":
        return QuarkClient(
            values.get("cookie", ""),
            root_id=provider.options.get("root_id", "0"),
            timeout_seconds=timeout_seconds,
            on_credential_update=on_credential_update,
            transport=transport,
            sleep=sleep,
        )
    if provider.name == "wopan":
        return WoPanClient(
            values.get("access_token", ""),
            values.get("refresh_token", ""),
            root_id=provider.options.get("root_id", "0"),
            family_id=provider.options.get("family_id", ""),
            timeout_seconds=timeout_seconds,
            on_credential_update=on_credential_update,
            transport=transport,
            sleep=sleep,
        )
    if provider.name == "baidu":
        return BaiduNetdiskClient(
            values.get("access_token", ""),
            values.get("refresh_token", ""),
            values.get("client_id", ""),
            values.get("client_secret", ""),
            values.get("cookie", ""),
            timeout_seconds=timeout_seconds,
            on_credential_update=on_credential_update,
            transport=transport,
            sleep=sleep,
        )
    if provider.name == "pan115" and values.get("cookie"):
        return Pan115CookieClient(
            values["cookie"],
            root_id=provider.options.get("root_id", "0"),
            timeout_seconds=timeout_seconds,
            on_credential_update=on_credential_update,
            transport=transport,
            sleep=sleep,
        )
    if provider.name == "pan115":
        return Pan115Client(
            values.get("access_token", ""),
            values.get("refresh_token", ""),
            root_id=provider.options.get("root_id", "0"),
            timeout_seconds=timeout_seconds,
            on_credential_update=on_credential_update,
            transport=transport,
            sleep=sleep,
        )
    if provider.name == "guangya":
        return GuangYaPanClient(
            values.get("access_token", ""),
            values.get("refresh_token", ""),
            values.get("client_id", ""),
            values.get("device_id", ""),
            root_id=provider.options.get("root_id", ""),
            timeout_seconds=timeout_seconds,
            on_credential_update=on_credential_update,
            transport=transport,
            sleep=sleep,
        )
    raise CloudUploadError(f"不支持的网盘上传目标：{provider.name}")
