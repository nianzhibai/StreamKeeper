from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import httpx

from ...errors import BilibiliFetchError, InvalidBilibiliUrl
from ...models import LiveInfo
from ..base import QUALITY_VALUES, iter_url_candidates, normalize_quality

_API_BASE = "https://api.live.bilibili.com"
_LIVE_ORIGIN = "https://live.bilibili.com"
_DIRECT_HOST = "live.bilibili.com"
_SHORT_HOSTS = frozenset({"b23.tv", "www.b23.tv"})
_QUALITY_QN = {
    "OD": 10000,
    "UHD": 400,
    "HD": 250,
    "SD": 150,
    "LD": 80,
}
_CODEC_RANK = {"avc": 3, "h264": 3, "hevc": 2, "h265": 2, "av1": 1}
_QN_RANK = {qn: rank for rank, qn in enumerate((80, 150, 250, 400, 10000, 15000, 20000, 30000))}
_CandidateScore = tuple[int, int, int, int]


class BilibiliClient:
    """Resolve Bilibili live rooms through the public Web live APIs."""

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
    def _direct_room_id(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or (parsed.hostname or "").lower() != _DIRECT_HOST:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        return parts[0] if len(parts) == 1 and parts[0].isdigit() else None

    @classmethod
    def validate_url(cls, value: str) -> str:
        for candidate in iter_url_candidates(value):
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme.lower() not in {"http", "https"}:
                continue
            room_id = cls._direct_room_id(candidate)
            if room_id:
                return f"https://{_DIRECT_HOST}/{room_id}"
            if host in _SHORT_HOSTS and bool(parsed.path.strip("/")):
                return candidate
        raise InvalidBilibiliUrl(
            "未找到支持的哔哩哔哩直播链接，例如 https://live.bilibili.com/123456 或 b23.tv 直播分享链接"
        )

    def _client(self, *, cookie: bool = True) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": _LIVE_ORIGIN,
            "Referer": f"{_LIVE_ORIGIN}/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
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

    async def _resolve_room_id(self, normalized_url: str) -> str:
        room_id = self._direct_room_id(normalized_url)
        if room_id:
            return room_id
        try:
            async with self._client(cookie=False) as client:
                response = await client.get(normalized_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BilibiliFetchError(f"哔哩哔哩分享链接跳转失败: {exc}") from exc
        room_id = self._direct_room_id(str(response.url))
        if not room_id:
            raise InvalidBilibiliUrl("哔哩哔哩分享链接没有跳转到直播间")
        return room_id

    @staticmethod
    def _payload_data(payload: object, endpoint: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BilibiliFetchError(f"哔哩哔哩接口 {endpoint} 返回格式错误")
        code = payload.get("code")
        data = payload.get("data")
        if code != 0 or not isinstance(data, dict):
            message = str(payload.get("message") or payload.get("msg") or "未知错误")
            raise BilibiliFetchError(f"哔哩哔哩接口 {endpoint} 失败（code={code}）: {message}")
        return data

    @classmethod
    def _stream_candidates(
        cls, data: dict[str, Any], requested_qn: int
    ) -> dict[str, list[tuple[_CandidateScore, str]]]:
        playurl_info = data.get("playurl_info")
        playurl = playurl_info.get("playurl") if isinstance(playurl_info, dict) else None
        streams = playurl.get("stream") if isinstance(playurl, dict) else None
        result: dict[str, list[tuple[_CandidateScore, str]]] = {"flv": [], "hls": []}
        if not isinstance(streams, list):
            return result

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            protocol_name = str(stream.get("protocol_name") or "").lower()
            protocol = "flv" if "stream" in protocol_name else "hls" if "hls" in protocol_name else ""
            if not protocol:
                continue
            formats = stream.get("format")
            if not isinstance(formats, list):
                continue
            for stream_format in formats:
                if not isinstance(stream_format, dict):
                    continue
                format_name = str(stream_format.get("format_name") or "").lower()
                format_rank = 2 if format_name in {"flv", "ts"} else 1
                codecs = stream_format.get("codec")
                if not isinstance(codecs, list):
                    continue
                for codec in codecs:
                    if not isinstance(codec, dict):
                        continue
                    base_url = codec.get("base_url")
                    url_info = codec.get("url_info")
                    if not isinstance(base_url, str) or not isinstance(url_info, list):
                        continue
                    try:
                        current_qn = int(codec.get("current_qn") or 0)
                    except (TypeError, ValueError):
                        current_qn = 0
                    codec_name = str(codec.get("codec_name") or "").lower()
                    requested_rank = _QN_RANK.get(requested_qn, 0)
                    current_rank = _QN_RANK.get(current_qn, -100)
                    score = (
                        1 if current_qn == requested_qn else 0,
                        -abs(current_rank - requested_rank),
                        1 if current_rank <= requested_rank else 0,
                        _CODEC_RANK.get(codec_name, 0) * 10 + format_rank,
                    )
                    for line in url_info:
                        if not isinstance(line, dict):
                            continue
                        host = line.get("host")
                        extra = line.get("extra") or ""
                        if isinstance(host, str) and isinstance(extra, str):
                            result[protocol].append((score, f"{host}{base_url}{extra}"))
        return result

    @staticmethod
    def _best_url(candidates: Iterable[tuple[_CandidateScore, str]]) -> str | None:
        ranked = list(candidates)
        return max(ranked, key=lambda item: item[0])[1] if ranked else None

    @staticmethod
    def _legacy_url(data: dict[str, Any]) -> str | None:
        urls = data.get("durl")
        if not isinstance(urls, list):
            return None
        for item in reversed(urls):
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return item["url"]
        return None

    async def fetch(self, url: str, quality: str = "OD") -> LiveInfo:
        normalized_url = self.validate_url(url)
        normalized_quality = normalize_quality(quality)
        room_id = await self._resolve_room_id(normalized_url)
        live_url = f"{_LIVE_ORIGIN}/{room_id}"

        try:
            async with self._client() as client:
                info_response = await client.get(
                    f"{_API_BASE}/xlive/web-room/v1/index/getH5InfoByRoom",
                    params={"room_id": room_id},
                    headers={"Referer": live_url},
                )
                info_response.raise_for_status()
                info_data = self._payload_data(info_response.json(), "getH5InfoByRoom")

                room_info = info_data.get("room_info")
                anchor_info = info_data.get("anchor_info")
                room_info = room_info if isinstance(room_info, dict) else {}
                anchor_info = anchor_info if isinstance(anchor_info, dict) else {}
                base_info = anchor_info.get("base_info")
                base_info = base_info if isinstance(base_info, dict) else {}
                anchor_name = str(base_info.get("uname") or "") or None
                title = str(room_info.get("title") or "") or None
                canonical_id = str(room_info.get("room_id") or room_id)
                live_url = f"{_LIVE_ORIGIN}/{canonical_id}"
                is_live = int(room_info.get("live_status") or 0) == 1
                if not is_live:
                    return LiveInfo(
                        platform="哔哩哔哩",
                        anchor_name=anchor_name,
                        is_live=False,
                        title=title,
                        quality=None,
                        m3u8_url=None,
                        flv_url=None,
                        record_url=None,
                        live_url=live_url,
                    )

                qn = _QUALITY_QN[normalized_quality]
                flv_url = None
                hls_url = None
                try:
                    play_response = await client.get(
                        f"{_API_BASE}/xlive/web-room/v2/index/getRoomPlayInfo",
                        params={
                            "room_id": canonical_id,
                            "protocol": "0,1",
                            "format": "0,1,2",
                            "codec": "0,1,2",
                            "qn": qn,
                            "platform": "web",
                            "ptype": 8,
                            "dolby": 5,
                            "panorama": 1,
                            "hdr_type": "0,1",
                        },
                        headers={"Referer": live_url},
                    )
                    play_response.raise_for_status()
                    play_data = self._payload_data(play_response.json(), "getRoomPlayInfo")
                    candidates = self._stream_candidates(play_data, qn)
                    flv_url = self._best_url(candidates["flv"])
                    hls_url = self._best_url(candidates["hls"])
                except (BilibiliFetchError, httpx.HTTPError, ValueError, TypeError, KeyError):
                    # The older endpoint often remains available during partial
                    # rollouts or account-specific failures of getRoomPlayInfo.
                    pass

                if not flv_url and not hls_url:
                    legacy_response = await client.get(
                        f"{_API_BASE}/room/v1/Room/playUrl",
                        params={"cid": canonical_id, "qn": qn, "platform": "web"},
                        headers={"Referer": live_url},
                    )
                    legacy_response.raise_for_status()
                    legacy_data = self._payload_data(legacy_response.json(), "playUrl")
                    legacy_url = self._legacy_url(legacy_data)
                    if legacy_url and ".m3u8" in urlparse(legacy_url).path.lower():
                        hls_url = legacy_url
                    else:
                        flv_url = legacy_url
        except BilibiliFetchError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise BilibiliFetchError(f"获取哔哩哔哩直播信息失败: {exc}") from exc

        if not flv_url and not hls_url:
            raise BilibiliFetchError("哔哩哔哩直播间已开播，但没有返回可用直播流")
        return LiveInfo(
            platform="哔哩哔哩",
            anchor_name=anchor_name,
            is_live=True,
            title=title,
            quality=normalized_quality,
            m3u8_url=hls_url,
            flv_url=flv_url,
            record_url=hls_url or flv_url,
            live_url=live_url,
        )
