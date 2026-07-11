from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

from .errors import DouyinFetchError, InvalidDouyinUrl
from .models import LiveInfo


class _DouyinStream(Protocol):
    async def fetch_app_stream_data(self, url: str) -> dict[str, Any]: ...

    async def fetch_web_stream_data(self, url: str) -> dict[str, Any]: ...

    async def fetch_stream_url(self, data: dict[str, Any], quality: str) -> object: ...


StreamFactory = Callable[..., _DouyinStream]


def _default_stream_factory(**kwargs: Any) -> _DouyinStream:
    try:
        from streamget import DouyinLiveStream
    except ImportError as exc:  # pragma: no cover - exercised only in an incomplete installation
        raise DouyinFetchError("streamget 未安装，请先执行: pip install -e .") from exc
    return DouyinLiveStream(**kwargs)


class DouyinClient:
    """A small adapter around ``streamget.DouyinLiveStream``."""

    QUALITY_VALUES = ("OD", "UHD", "HD", "SD", "LD")

    def __init__(
        self,
        *,
        proxy: str | None = None,
        cookies: str | None = None,
        stream_orientation: int = 1,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        if stream_orientation not in (1, 2):
            raise ValueError("stream_orientation 必须是 1 或 2")
        self.proxy = proxy or None
        self.cookies = cookies or None
        self.stream_orientation = stream_orientation
        self._stream_factory = stream_factory or _default_stream_factory
        self._stream: _DouyinStream | None = None

    @staticmethod
    def validate_url(url: str) -> str:
        normalized = url.strip()
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower().rstrip(".")
        has_supported_path = (
            (host == "live.douyin.com" and bool(parsed.path.strip("/")))
            or (host == "v.douyin.com" and bool(parsed.path.strip("/")))
            or (host == "www.douyin.com" and parsed.path.startswith("/user/"))
        )
        if parsed.scheme not in {"http", "https"} or not has_supported_path:
            raise InvalidDouyinUrl(
                "仅支持抖音链接，例如 https://live.douyin.com/...、"
                "https://v.douyin.com/... 或 https://www.douyin.com/user/..."
            )
        return normalized

    @staticmethod
    def _uses_app_parser(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host == "v.douyin.com" or (host == "www.douyin.com" and parsed.path.startswith("/user/"))

    def _get_stream(self) -> _DouyinStream:
        if self._stream is None:
            self._stream = self._stream_factory(
                proxy_addr=self.proxy,
                cookies=self.cookies,
                stream_orientation=self.stream_orientation,
            )
        return self._stream

    async def fetch(self, url: str, quality: str = "OD") -> LiveInfo:
        normalized_url = self.validate_url(url)
        normalized_quality = quality.upper()
        if normalized_quality not in self.QUALITY_VALUES:
            raise ValueError(f"不支持的画质 {quality!r}，可选值: {', '.join(self.QUALITY_VALUES)}")

        stream = self._get_stream()
        try:
            if self._uses_app_parser(normalized_url):
                raw_data = await stream.fetch_app_stream_data(normalized_url)
            else:
                raw_data = await stream.fetch_web_stream_data(normalized_url)
            stream_data = await stream.fetch_stream_url(raw_data, normalized_quality)
        except Exception as exc:
            raise DouyinFetchError(f"获取抖音直播信息失败: {exc}") from exc

        if stream_data is None:
            raise DouyinFetchError("streamget 未返回直播信息")
        return LiveInfo.from_stream_data(stream_data)
