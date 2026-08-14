from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import InvalidLiveUrl
from ..models import LiveInfo
from .base import QUALITY_VALUES
from .bilibili import BilibiliClient
from .douyin import DouyinClient
from .kuaishou import KuaishouClient

SupportedClient = DouyinClient | BilibiliClient | KuaishouClient
PlatformClientFactory = Callable[..., SupportedClient]


class LiveStreamClient:
    """Route a supported live-room URL to its platform-specific resolver."""

    QUALITY_VALUES = QUALITY_VALUES
    _CLIENT_TYPES = (DouyinClient, BilibiliClient, KuaishouClient)

    def __init__(
        self,
        *,
        proxy: str | None = None,
        douyin_cookies: str | None = None,
        bilibili_cookies: str | None = None,
        kuaishou_cookies: str | None = None,
        timeout: float = 25.0,
        douyin_client_factory: PlatformClientFactory | None = None,
        bilibili_client_factory: PlatformClientFactory | None = None,
        kuaishou_client_factory: PlatformClientFactory | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.proxy = proxy or None
        self.timeout = timeout
        self._cookies = {
            "douyin": douyin_cookies,
            "bilibili": bilibili_cookies,
            "kuaishou": kuaishou_cookies,
        }
        self._factories: dict[str, PlatformClientFactory] = {
            "douyin": douyin_client_factory or DouyinClient,
            "bilibili": bilibili_client_factory or BilibiliClient,
            "kuaishou": kuaishou_client_factory or KuaishouClient,
        }
        self._clients: dict[str, SupportedClient] = {}

    @classmethod
    def _match_url(cls, value: str) -> tuple[str, str]:
        keys = ("douyin", "bilibili", "kuaishou")
        for key, client_type in zip(keys, cls._CLIENT_TYPES, strict=True):
            try:
                return key, client_type.validate_url(value)
            except InvalidLiveUrl:
                continue
        raise InvalidLiveUrl("未找到支持的直播链接；当前支持抖音、哔哩哔哩和快手直播间及其分享链接")

    @classmethod
    def validate_url(cls, value: str) -> str:
        return cls._match_url(value)[1]

    def _get_client(self, key: str) -> SupportedClient:
        client = self._clients.get(key)
        if client is not None:
            return client
        kwargs: dict[str, Any] = {
            "proxy": self.proxy,
            "cookies": self._cookies[key],
            "timeout": self.timeout,
        }
        client = self._factories[key](**kwargs)
        self._clients[key] = client
        return client

    async def fetch(self, url: str, quality: str = "OD") -> LiveInfo:
        key, normalized_url = self._match_url(url)
        return await self._get_client(key).fetch(normalized_url, quality)
