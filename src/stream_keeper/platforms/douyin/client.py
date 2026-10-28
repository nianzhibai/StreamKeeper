from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

from ...errors import DouyinFetchError, InvalidDouyinUrl, ResolverError, RoomOfflineError
from ...models import LiveInfo
from ..quality import validate_quality
from .parser import (
    RoomResult,
    StreamCandidate,
    _normal_gear,
    choose_candidate,
    collect_candidates,
)
from .transport import DouyinRoomResolver


class RoomResolver(Protocol):
    def resolve(self, target: str) -> RoomResult: ...


ResolverFactory = Callable[..., RoomResolver]

_URL_CANDIDATE_PATTERN = re.compile(
    r"https?://[A-Z0-9._~:/?#\[\]@!$&()*+,;=%-]+",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}，。；：！？、）》】」』"
_FOLLOW_LIVE_PATH = re.compile(r"^/follow/live/(\d+)/?$")



class DouyinClient:
    """Resolve Douyin live streams through the Web enter-room API."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        cookies: str | None = None,
        timeout: float = 25.0,
        resolver_factory: ResolverFactory | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.proxy = proxy or None
        self.cookies = cookies or None
        self.timeout = timeout
        self._resolver_factory = resolver_factory or DouyinRoomResolver
        self._resolver: RoomResolver | None = None

    @staticmethod
    def validate_url(url: str) -> str:
        for match in _URL_CANDIDATE_PATTERN.finditer(url.strip()):
            candidate = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            follow_live = _FOLLOW_LIVE_PATH.fullmatch(parsed.path)
            has_supported_path = (
                (host == "live.douyin.com" and bool(parsed.path.strip("/")))
                or (host == "v.douyin.com" and bool(parsed.path.strip("/")))
                or (host == "www.douyin.com" and parsed.path.startswith("/user/"))
                or (host == "www.douyin.com" and follow_live is not None)
            )
            if parsed.scheme.lower() in {"http", "https"} and has_supported_path:
                if host == "www.douyin.com" and follow_live is not None:
                    return f"https://live.douyin.com/{follow_live.group(1)}"
                return candidate
        raise InvalidDouyinUrl(
            "未找到支持的抖音链接，例如 https://live.douyin.com/...、"
            "https://v.douyin.com/...、https://www.douyin.com/follow/live/... "
            "或 https://www.douyin.com/user/..."
        )

    def _get_resolver(self) -> RoomResolver:
        if self._resolver is None:
            self._resolver = self._resolver_factory(
                timeout=self.timeout,
                cookie=self.cookies or "",
                proxy=self.proxy,
            )
        return self._resolver

    @staticmethod
    def _room_is_live(room: dict[str, Any]) -> bool:
        return int(room.get("status") or 0) == 2

    @staticmethod
    def _pick_urls(candidates: list[StreamCandidate], selected: StreamCandidate) -> tuple[str | None, str | None]:
        same_gear = [
            item
            for item in candidates
            if _normal_gear(item.gear) == _normal_gear(selected.gear) and not item.is_audio_only and not item.encrypted
        ]
        main_lines = [item for item in same_gear if item.line in {"main", ""}]
        pool = main_lines or same_gear

        def pick(protocol: str) -> str | None:
            for item in pool:
                if item.protocol == protocol and item.url:
                    return item.url
            for item in same_gear:
                if item.protocol == protocol and item.url:
                    return item.url
            return selected.url if selected.protocol == protocol else None

        flv_url = pick("flv")
        hls_url = pick("hls")
        if selected.protocol == "flv" and selected.url:
            flv_url = selected.url
        if selected.protocol == "hls" and selected.url:
            hls_url = selected.url
        return flv_url, hls_url

    def _choose_quality(self, candidates: list[StreamCandidate], quality: str) -> StreamCandidate:
        return choose_candidate(candidates, quality, "auto")

    def _resolve_live_info(self, url: str, quality: str) -> LiveInfo:
        normalized_url = self.validate_url(url)
        normalized_quality = validate_quality("douyin", quality)

        resolver = self._get_resolver()
        try:
            room: RoomResult = resolver.resolve(normalized_url)
        except RoomOfflineError:
            raise
        except ResolverError as exc:
            raise DouyinFetchError(f"获取抖音直播信息失败: {exc}") from exc

        orientation = room.room.get("stream_orientation")
        if orientation is None:
            stream_url = room.room.get("stream_url")
            if isinstance(stream_url, dict):
                orientation = stream_url.get("stream_orientation")

        if not self._room_is_live(room.room):
            return LiveInfo(
                platform="抖音",
                anchor_name=room.owner or None,
                is_live=False,
                title=room.title or None,
                quality=None,
                m3u8_url=None,
                flv_url=None,
                record_url=None,
                live_url=room.referer or normalized_url,
                stream_orientation=int(orientation) if orientation is not None else None,
            )

        try:
            candidates = collect_candidates(room.room)
            if not candidates:
                return LiveInfo(
                    platform="抖音",
                    anchor_name=room.owner or None,
                    is_live=False,
                    title=room.title or None,
                    quality=None,
                    m3u8_url=None,
                    flv_url=None,
                    record_url=None,
                    live_url=room.referer or normalized_url,
                    stream_orientation=int(orientation) if orientation is not None else None,
                )
            selected = self._choose_quality(candidates, normalized_quality)
        except ResolverError as exc:
            raise DouyinFetchError(f"解析直播流失败: {exc}") from exc

        flv_url, hls_url = self._pick_urls(candidates, selected)
        flv_codec = next((item.codec for item in candidates if item.url == flv_url and item.codec), None)
        hls_codec = next((item.codec for item in candidates if item.url == hls_url and item.codec), None)
        return LiveInfo(
            platform="抖音",
            anchor_name=room.owner or None,
            is_live=True,
            title=room.title or None,
            quality=selected.gear,
            m3u8_url=hls_url,
            flv_url=flv_url,
            record_url=hls_url or flv_url,
            live_url=room.referer or normalized_url,
            stream_orientation=int(orientation) if orientation is not None else None,
            flv_codec=flv_codec,
            hls_codec=hls_codec,
        )

    async def fetch(self, url: str, quality: str = "best") -> LiveInfo:
        try:
            return await asyncio.to_thread(self._resolve_live_info, url, quality)
        except (DouyinFetchError, InvalidDouyinUrl, RoomOfflineError, ValueError):
            raise
        except Exception as exc:  # pragma: no cover - defensive wrap for unexpected resolver bugs
            raise DouyinFetchError(f"获取抖音直播信息失败: {exc}") from exc
