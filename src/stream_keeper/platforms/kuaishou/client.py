from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from ...errors import InvalidKuaishouUrl, KuaishouFetchError
from ...models import LiveInfo
from ..base import QUALITY_VALUES, iter_url_candidates, normalize_quality

_DIRECT_HOST = "live.kuaishou.com"
_SHORT_HOSTS = frozenset({"v.kuaishou.com", "www.v.kuaishou.com"})
_SUPPORTED_PATHS = frozenset({"u", "profile"})
_INITIAL_STATE_MARKER = "window.__INITIAL_STATE__="
_QUALITY_BITRATE = {"OD": 10**18, "UHD": 2000, "HD": 1000, "SD": 800, "LD": 600}


def _walk_dicts(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _replace_undefined(source: str) -> str:
    """Turn JavaScript's bare ``undefined`` values into JSON nulls.

    Kuaishou serializes an object literal rather than strict JSON on live-room
    pages. Replacing only tokens outside quoted strings keeps captions and URLs
    byte-for-byte intact while allowing the standard-library decoder to parse it.
    """

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        character = source[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if source.startswith("undefined", index):
            output.append("null")
            index += len("undefined")
            continue
        output.append(character)
        index += 1
    return "".join(output)


class KuaishouClient:
    """Resolve Kuaishou Web live pages and select a bitrate rendition."""

    QUALITY_VALUES = QUALITY_VALUES

    def __init__(
        self,
        *,
        proxy: str | None = None,
        cookies: str | None = None,
        timeout: float = 25.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.proxy = proxy or None
        self.cookies = cookies or None
        self.timeout = timeout
        self._transport = transport

    @staticmethod
    def _direct_url(value: str) -> str | None:
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or (parsed.hostname or "").lower() != _DIRECT_HOST:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0].lower() not in _SUPPORTED_PATHS or not parts[1]:
            return None
        return f"https://{_DIRECT_HOST}/{parts[0].lower()}/{parts[1]}"

    @classmethod
    def validate_url(cls, value: str) -> str:
        for candidate in iter_url_candidates(value):
            direct = cls._direct_url(candidate)
            if direct:
                return direct
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme.lower() in {"http", "https"} and host in _SHORT_HOSTS and bool(parsed.path.strip("/")):
                return candidate
        raise InvalidKuaishouUrl(
            "未找到支持的快手直播链接，例如 https://live.kuaishou.com/u/... 或 v.kuaishou.com 直播分享链接"
        )

    def _client(self, *, cookie: bool = True) -> httpx.AsyncClient:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://live.kuaishou.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        }
        if cookie and self.cookies:
            headers["Cookie"] = self.cookies
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "headers": headers,
            "timeout": httpx.Timeout(self.timeout),
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _resolve_url(self, normalized_url: str) -> str:
        direct = self._direct_url(normalized_url)
        if direct:
            return direct
        try:
            async with self._client(cookie=False) as client:
                response = await client.get(normalized_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KuaishouFetchError(f"快手分享链接跳转失败: {exc}") from exc
        direct = self._direct_url(str(response.url))
        if not direct:
            raise InvalidKuaishouUrl("快手分享链接没有跳转到直播间")
        return direct

    @staticmethod
    async def _user_status(client: httpx.AsyncClient, live_url: str) -> tuple[str | None, bool] | None:
        """Use the account endpoint when an authenticated cookie is available.

        An offline ``/u/`` page does not always embed room state. The account
        endpoint can report that state directly, while any captcha or schema
        mismatch simply falls back to parsing the public page.
        """

        principal_id = urlparse(live_url).path.rstrip("/").rsplit("/", 1)[-1]
        try:
            response = await client.get(
                "https://live.kuaishou.com/live_api/baseuser/userinfo/byid",
                params={"__NS_hxfalcon": "", "caver": 2, "principalId": principal_id},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            user_info = data.get("userInfo") if isinstance(data, dict) else None
            living = user_info.get("living") if isinstance(user_info, dict) else None
            if not isinstance(living, (bool, int)) or living not in (False, True):
                return None
            return str(user_info.get("name") or "") or None, bool(living)
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    @staticmethod
    def _parse_initial_state(html: str) -> dict[str, Any]:
        marker_at = html.find(_INITIAL_STATE_MARKER)
        if marker_at < 0:
            raise KuaishouFetchError("快手直播页缺少初始化数据，可能触发了平台风控")
        source = _replace_undefined(html[marker_at + len(_INITIAL_STATE_MARKER) :].lstrip())
        try:
            state, _ = json.JSONDecoder().raw_decode(source)
        except json.JSONDecodeError as exc:
            raise KuaishouFetchError("快手直播页初始化数据不是有效 JSON") from exc
        if not isinstance(state, dict):
            raise KuaishouFetchError("快手直播页初始化数据格式错误")
        return state

    @staticmethod
    def _live_container(state: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        for item in _walk_dicts(state):
            if "liveStream" in item:
                stream = item.get("liveStream")
                return item, stream if isinstance(stream, dict) else None
        for item in _walk_dicts(state):
            if "playUrls" in item:
                return item, item
        return None, None

    @staticmethod
    def _anchor_name(state: dict[str, Any], container: dict[str, Any] | None) -> str | None:
        candidates: list[object] = []
        if container:
            author = container.get("author")
            if isinstance(author, dict):
                candidates.extend((author.get("name"), author.get("user_name"), author.get("nickname")))
        author_by_id = state.get("authorInfoById")
        if isinstance(author_by_id, dict):
            user_info = author_by_id.get("userInfo")
            if isinstance(user_info, dict):
                candidates.extend((user_info.get("name"), user_info.get("user_name")))
        for item in _walk_dicts(state):
            if isinstance(item.get("author"), dict):
                candidates.append(item["author"].get("name"))
        return next((str(value) for value in candidates if value), None)

    @staticmethod
    def _title(container: dict[str, Any] | None, stream: dict[str, Any] | None) -> str | None:
        for item in (stream, container):
            if not isinstance(item, dict):
                continue
            for key in ("caption", "title", "liveTitle"):
                if item.get(key):
                    return str(item[key])
            game_info = item.get("gameInfo")
            if isinstance(game_info, dict) and game_info.get("name"):
                return str(game_info["name"])
        return None

    @staticmethod
    def _representations(play_urls: object) -> list[dict[str, Any]]:
        if isinstance(play_urls, dict):
            for codec_name in ("h264", "avc"):
                codec_data = play_urls.get(codec_name)
                if codec_data is not None:
                    play_urls = codec_data
                    break
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in _walk_dicts(play_urls):
            adaptation = item.get("adaptationSet")
            if not isinstance(adaptation, dict):
                continue
            representations = adaptation.get("representation")
            if not isinstance(representations, list):
                continue
            for representation in representations:
                if not isinstance(representation, dict):
                    continue
                url = representation.get("url")
                if isinstance(url, str) and url and url not in seen:
                    seen.add(url)
                    result.append(representation)
        return result

    @staticmethod
    def _select_url(representations: Iterable[dict[str, Any]], quality: str) -> str | None:
        ranked: list[tuple[int | None, str]] = []
        for item in representations:
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            try:
                bitrate = int(item["bitrate"]) if item.get("bitrate") is not None else None
            except (TypeError, ValueError):
                bitrate = None
            ranked.append((bitrate, url))
        if not ranked:
            return None
        with_bitrate = sorted((item for item in ranked if item[0] is not None), reverse=True)
        if with_bitrate:
            ceiling = _QUALITY_BITRATE[quality]
            return next(
                (url for bitrate, url in with_bitrate if bitrate is not None and bitrate <= ceiling),
                with_bitrate[-1][1],
            )
        quality_index = {"OD": 0, "UHD": 1, "HD": 2, "SD": 3, "LD": 4}[quality]
        highest_first = list(reversed(ranked))
        return highest_first[min(quality_index, len(highest_first) - 1)][1]

    async def fetch(self, url: str, quality: str = "OD") -> LiveInfo:
        normalized_url = self.validate_url(url)
        normalized_quality = normalize_quality(quality)
        live_url = await self._resolve_url(normalized_url)
        known_anchor: str | None = None
        try:
            async with self._client() as client:
                if self.cookies:
                    status = await self._user_status(client, live_url)
                    if status is not None:
                        known_anchor, is_live = status
                        if not is_live:
                            return LiveInfo(
                                platform="快手",
                                anchor_name=known_anchor,
                                is_live=False,
                                title=None,
                                quality=None,
                                m3u8_url=None,
                                flv_url=None,
                                record_url=None,
                                live_url=live_url,
                            )
                response = await client.get(live_url, headers={"Referer": live_url})
                response.raise_for_status()
                state = self._parse_initial_state(response.text)
        except KuaishouFetchError:
            raise
        except httpx.HTTPError as exc:
            raise KuaishouFetchError(f"获取快手直播信息失败: {exc}") from exc

        container, stream = self._live_container(state)
        anchor_name = self._anchor_name(state, container) or known_anchor
        title = self._title(container, stream)
        if container and isinstance(container.get("errorType"), dict):
            error = container["errorType"]
            message = f"{error.get('title') or ''}{error.get('content') or ''}".strip()
            if not any(keyword in message for keyword in ("未开播", "暂未直播", "直播已结束", "已下播")):
                raise KuaishouFetchError(f"快手直播页返回错误: {message or '未知错误'}")
            stream = None
        if stream is None:
            return LiveInfo(
                platform="快手",
                anchor_name=anchor_name,
                is_live=False,
                title=title,
                quality=None,
                m3u8_url=None,
                flv_url=None,
                record_url=None,
                live_url=live_url,
            )

        representations = self._representations(stream.get("playUrls"))
        if not representations:
            raise KuaishouFetchError("快手直播间数据存在但没有可用直播流，可能触发了平台风控")
        flv_items = [item for item in representations if ".flv" in urlparse(str(item.get("url") or "")).path.lower()]
        hls_items = [item for item in representations if ".m3u8" in urlparse(str(item.get("url") or "")).path.lower()]
        flv_url = self._select_url(flv_items, normalized_quality)
        hls_url = self._select_url(hls_items, normalized_quality)
        if not flv_url and not hls_url:
            raise KuaishouFetchError("快手直播间没有返回 FFmpeg 可用的 FLV 或 HLS 地址")
        return LiveInfo(
            platform="快手",
            anchor_name=anchor_name,
            is_live=True,
            title=title,
            quality=normalized_quality,
            m3u8_url=hls_url,
            flv_url=flv_url,
            record_url=flv_url or hls_url,
            live_url=live_url,
        )
