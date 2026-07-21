"""Web enter-room resolver that enumerates all Douyin live stream qualities."""

from __future__ import annotations

import dataclasses
import gzip
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import ResolverError, RoomOfflineError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
WEB_ENTER_URL = "https://live.douyin.com/webcast/room/web/enter/"

SOURCE_GEARS = {
    "origin",
    "origion",
    "original",
    "source",
    "uhd",
    "full_hd1",
    "fullhd1",
}

GEAR_RANK = {
    "ao": -10,
    "audio": -10,
    "md": 10,
    "ld": 20,
    "sd1": 20,
    "sd": 30,
    "sd2": 30,
    "hd": 40,
    "hd1": 40,
    "fhd": 50,
    "uhd": 60,
    "full_hd1": 70,
    "fullhd1": 70,
    "origin": 70,
    "origion": 70,
    "original": 70,
}



@dataclasses.dataclass
class StreamCandidate:
    source: str
    gear: str
    line: str
    protocol: str
    url: str
    declared_bitrate: int = 0
    realtime_bitrate: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    display_name: str = ""
    default: bool = False
    encrypted: bool = False
    rank_hint: int = 0
    sdk_params: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def effective_bitrate(self) -> int:
        return self.declared_bitrate or self.realtime_bitrate

    @property
    def is_audio_only(self) -> bool:
        return _normal_gear(self.gear) in {"ao", "audio"}

    def as_dict(self, include_url: bool = True) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["effective_bitrate"] = self.effective_bitrate
        if not include_url:
            result["url"] = _redact_url(self.url)
        return result


@dataclasses.dataclass
class RoomResult:
    room: dict[str, Any]
    response: dict[str, Any]
    web_rid: str = ""
    room_id: str = ""
    title: str = ""
    owner: str = ""
    referer: str = "https://live.douyin.com/"


def _normal_gear(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _to_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _to_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _jsonish(value: Any) -> Any:
    """Decode nested JSON strings used by stream_data and sdk_params."""
    current = value
    for _ in range(5):
        if not isinstance(current, str):
            break
        text = html.unescape(current).strip()
        if not text:
            return ""
        if text[:3].lower() in {"%7b", "%5b", "%22"}:
            text = urllib.parse.unquote(text)
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return current
        if decoded == current:
            break
        current = decoded
    return current


def _as_dict(value: Any) -> dict[str, Any]:
    decoded = _jsonish(value)
    return decoded if isinstance(decoded, dict) else {}


def _as_list(value: Any) -> list[Any]:
    decoded = _jsonish(value)
    return decoded if isinstance(decoded, list) else []


def _parse_resolution(value: Any) -> tuple[int, int]:
    text = str(value or "")
    match = re.search(r"(\d{2,5})\s*[xX*×]\s*(\d{2,5})", text)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _detect_protocol(url: str, fallback: str = "") -> str:
    lowered = urllib.parse.urlsplit(url).path.lower()
    if ".flv" in lowered:
        return "flv"
    if ".m3u8" in lowered:
        return "hls"
    if ".mpd" in lowered:
        return "dash"
    return fallback.lower()


def _append_common_query(url: str, query: Any) -> str:
    query_map = _as_dict(query)
    if not url or not query_map:
        return url
    encoded = urllib.parse.urlencode(
        [(str(key), str(value)) for key, value in query_map.items()],
        doseq=True,
    )
    if not encoded:
        return url
    if "?" not in url:
        return f"{url}?{encoded}"
    if url.endswith("?") or url.endswith("&"):
        return f"{url}{encoded}"
    return f"{url}&{encoded}"


def _redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "…", parts.fragment))


def _known_rank(gear: str) -> int:
    return GEAR_RANK.get(_normal_gear(gear), 0)


def _iter_stream_containers(room: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    seen: set[int] = set()

    def emit(name: str, value: Any) -> Iterable[tuple[str, dict[str, Any]]]:
        container = _as_dict(value)
        if not container or id(container) in seen:
            return
        seen.add(id(container))
        yield name, container

    yield from emit("stream_url", room.get("stream_url"))
    yield from emit("additional_stream_url", room.get("additional_stream_url"))

    web_data = _as_dict(room.get("web_data"))
    yield from emit("web_data.additional_stream_url", web_data.get("additional_stream_url"))

    primary = _as_dict(room.get("stream_url"))
    for key, pull_data in _as_dict(primary.get("pull_datas")).items():
        pseudo = {
            "live_core_sdk_data": {"pull_data": pull_data},
            "extra": primary.get("extra"),
            "default_resolution": primary.get("default_resolution"),
        }
        yield f"stream_url.pull_datas.{key}", pseudo


def _quality_metadata(pull_data: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    options = _as_dict(pull_data.get("options"))
    metadata: dict[str, dict[str, Any]] = {}
    for item in _as_list(options.get("qualities")):
        quality = _as_dict(item)
        key = _normal_gear(quality.get("sdk_key"))
        if key:
            metadata[key] = quality

    defaults: set[str] = set()
    default_quality = _as_dict(options.get("default_quality"))
    default_key = _normal_gear(default_quality.get("sdk_key"))
    if default_key:
        defaults.add(default_key)
    return metadata, defaults


def _modern_candidates(source: str, container: dict[str, Any]) -> list[StreamCandidate]:
    live_core = _as_dict(container.get("live_core_sdk_data"))
    pull_data = _as_dict(live_core.get("pull_data"))
    if not pull_data:
        return []

    quality_meta, defaults = _quality_metadata(pull_data)
    stream_data = _as_dict(pull_data.get("stream_data"))
    common = _as_dict(stream_data.get("common"))
    auto = _as_dict(common.get("auto"))
    auto_default = _normal_gear(auto.get("default"))
    if auto_default:
        defaults.add(auto_default)

    data = _as_dict(stream_data.get("data"))
    extra = _as_dict(container.get("extra"))
    candidates: list[StreamCandidate] = []

    # The official player treats origin specially.  It scans the line SDK
    # parameters for TargetOriginBitRate and uses that value for the origin
    # gear instead of its ordinary vbitrate (X.0Jf5 in 39.6.0).  In real
    # rooms vbitrate can be a small instantaneous value, which would make a
    # naive max-bitrate selector incorrectly prefer a lower-resolution gear.
    target_origin_bitrate = 0
    for gear_value in data.values():
        gear_obj = _as_dict(gear_value)
        for line_value in gear_obj.values():
            line_obj = _as_dict(line_value)
            sdk_params = _as_dict(line_obj.get("sdk_params"))
            target_origin_bitrate = max(
                target_origin_bitrate,
                _to_int(
                    sdk_params.get("TargetOriginBitRate")
                    or sdk_params.get("target_origin_bitrate")
                ),
            )

    for gear_raw, gear_value in data.items():
        gear = str(gear_raw)
        normalized = _normal_gear(gear)
        gear_obj = _as_dict(gear_value)
        metadata = quality_meta.get(normalized, {})

        line_names = [name for name in ("main", "backup") if name in gear_obj]
        line_names.extend(
            name
            for name, value in gear_obj.items()
            if name not in line_names and isinstance(_jsonish(value), dict)
        )

        for line in line_names:
            line_obj = _as_dict(gear_obj.get(line))
            if not line_obj:
                continue
            sdk_params = _as_dict(line_obj.get("sdk_params"))
            realtime = _as_dict(line_obj.get("templateRealTimeInfo"))

            declared = _to_int(
                sdk_params.get("vbitrate")
                or sdk_params.get("v_bit_rate")
                or metadata.get("v_bit_rate")
                or metadata.get("vbitrate")
            )
            if normalized == "origin" and target_origin_bitrate > 0:
                declared = target_origin_bitrate
            realtime_bitrate = int(_to_float(realtime.get("bitrateKbps")) * 1000)
            resolution = sdk_params.get("resolution") or metadata.get("resolution")
            width, height = _parse_resolution(resolution)
            if not width and normalized in SOURCE_GEARS:
                width = _to_int(extra.get("width"))
                height = _to_int(extra.get("height"))
            fps = _to_float(sdk_params.get("fps") or metadata.get("fps"))
            codec = str(
                sdk_params.get("VCodec")
                or sdk_params.get("v_codec")
                or metadata.get("v_codec")
                or pull_data.get("codec")
                or ""
            )
            display_name = str(metadata.get("name") or realtime.get("name") or "")
            encrypted = bool(line_obj.get("enableEncryption") or line_obj.get("enable_encryption"))

            for protocol_key in ("flv", "hls", "cmaf", "dash", "ll_hls", "http_ts"):
                url = line_obj.get(protocol_key)
                if not isinstance(url, str) or not url.strip():
                    continue
                url = _append_common_query(url.strip(), common.get("query"))
                candidates.append(
                    StreamCandidate(
                        source=source,
                        gear=gear,
                        line=str(line),
                        protocol=_detect_protocol(url, protocol_key),
                        url=url,
                        declared_bitrate=declared,
                        realtime_bitrate=realtime_bitrate,
                        width=width,
                        height=height,
                        fps=fps,
                        codec=codec,
                        display_name=display_name,
                        default=normalized in defaults,
                        encrypted=encrypted,
                        rank_hint=_known_rank(gear),
                        sdk_params=sdk_params,
                    )
                )

    # Some responses expose the parsed list even when stream_data is missing.
    for field_name, fallback_protocol in (("Flv", "flv"), ("Hls", "hls")):
        for item in _as_list(pull_data.get(field_name)):
            play = _as_dict(item)
            url = str(play.get("url") or "").strip()
            if not url:
                continue
            gear = str(play.get("quality_name") or "default")
            metadata = quality_meta.get(_normal_gear(gear), {})
            sdk_params = _as_dict(play.get("params"))
            width, height = _parse_resolution(
                sdk_params.get("resolution") or metadata.get("resolution")
            )
            candidates.append(
                StreamCandidate(
                    source=f"{source}.{field_name}",
                    gear=gear,
                    line="main",
                    protocol=_detect_protocol(url, fallback_protocol),
                    url=url,
                    declared_bitrate=_to_int(
                        
                            target_origin_bitrate
                            if _normal_gear(gear) == "origin" and target_origin_bitrate > 0
                            else sdk_params.get("vbitrate") or metadata.get("v_bit_rate")
                        
                    ),
                    width=width,
                    height=height,
                    fps=_to_float(sdk_params.get("fps") or metadata.get("fps")),
                    codec=str(
                        sdk_params.get("VCodec") or metadata.get("v_codec") or ""
                    ),
                    display_name=str(metadata.get("name") or ""),
                    default=_normal_gear(gear) in defaults,
                    rank_hint=_known_rank(gear),
                    sdk_params=sdk_params,
                )
            )
    return candidates


def _legacy_candidates(source: str, container: dict[str, Any]) -> list[StreamCandidate]:
    result: list[StreamCandidate] = []
    candidate_order = [str(item) for item in _as_list(container.get("candidate_resolution"))]
    default_key = str(container.get("default_resolution") or "")
    resolution_names = _as_dict(container.get("resolution_name"))
    extra = _as_dict(container.get("extra"))

    maps = (
        ("flv", _as_dict(container.get("flv_pull_url")), "flv_pull_url_params"),
        ("hls", _as_dict(container.get("hls_pull_url_map")), "hls_pull_url_params"),
    )
    for fallback_protocol, url_map, params_field in maps:
        params_map = _as_dict(container.get(params_field))
        for gear, raw_url in url_map.items():
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            sdk_params = _as_dict(params_map.get(gear))
            width, height = _parse_resolution(sdk_params.get("resolution"))
            if not width and _normal_gear(gear) in SOURCE_GEARS:
                width = _to_int(extra.get("width"))
                height = _to_int(extra.get("height"))
            try:
                order_rank = candidate_order.index(str(gear)) + 1
            except ValueError:
                order_rank = 0
            result.append(
                StreamCandidate(
                    source=f"{source}.legacy",
                    gear=str(gear),
                    line="main",
                    protocol=_detect_protocol(raw_url, fallback_protocol),
                    url=raw_url.strip(),
                    declared_bitrate=_to_int(
                        sdk_params.get("vbitrate") or sdk_params.get("v_bit_rate")
                    ),
                    width=width,
                    height=height,
                    fps=_to_float(sdk_params.get("fps")),
                    codec=str(sdk_params.get("VCodec") or sdk_params.get("v_codec") or ""),
                    display_name=str(resolution_names.get(gear) or ""),
                    default=str(gear) == default_key,
                    rank_hint=max(_known_rank(str(gear)), order_rank),
                    sdk_params=sdk_params,
                )
            )

    fallbacks = (
        ("rtmp_pull_url", "flv", "rtmp_pull_url_params"),
        ("hls_pull_url", "hls", "hls_pull_url_params"),
    )
    for url_field, protocol, params_field in fallbacks:
        url = container.get(url_field)
        if not isinstance(url, str) or not url.strip():
            continue
        sdk_params = _as_dict(container.get(params_field))
        width, height = _parse_resolution(sdk_params.get("resolution"))
        result.append(
            StreamCandidate(
                source=f"{source}.legacy",
                gear=default_key or "default",
                line="main",
                protocol=_detect_protocol(url, protocol),
                url=url.strip(),
                declared_bitrate=_to_int(sdk_params.get("vbitrate")),
                width=width,
                height=height,
                fps=_to_float(sdk_params.get("fps")),
                codec=str(sdk_params.get("VCodec") or ""),
                display_name=str(resolution_names.get(default_key) or ""),
                default=True,
                rank_hint=_known_rank(default_key),
                sdk_params=sdk_params,
            )
        )
    return result


def collect_candidates(room: dict[str, Any]) -> list[StreamCandidate]:
    candidates: list[StreamCandidate] = []
    for source, container in _iter_stream_containers(room):
        candidates.extend(_modern_candidates(source, container))
        candidates.extend(_legacy_candidates(source, container))

    # Keep the rich modern entry when the same signed URL also appears in a legacy map.
    unique: list[StreamCandidate] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        if candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        unique.append(candidate)
    return unique


def choose_candidate(
    candidates: list[StreamCandidate],
    quality: str = "max-bitrate",
    protocol: str = "auto",
) -> StreamCandidate:
    if not candidates:
        raise RoomOfflineError("直播间未返回可录制的拉流地址，可能已下播或需要登录权限")

    pool = list(candidates)
    if protocol != "auto":
        filtered = [item for item in pool if item.protocol == protocol]
        if not filtered:
            raise ResolverError(f"没有 {protocol} 协议的候选流")
        pool = filtered
    else:
        compatible = [item for item in pool if item.protocol in {"flv", "hls"}]
        if compatible:
            pool = compatible

    unencrypted = [item for item in pool if not item.encrypted]
    if unencrypted:
        pool = unencrypted
    video = [item for item in pool if not item.is_audio_only]
    if video:
        pool = video

    main = [item for item in pool if item.line in {"main", ""}]
    if main:
        pool = main

    quality_key = _normal_gear(quality)
    if quality_key in {"source", "origin", "original", "origion"}:
        source_pool = [item for item in pool if _normal_gear(item.gear) in SOURCE_GEARS]
        if source_pool:
            pool = source_pool
        quality_key = "source"
    elif quality_key == "default":
        defaults = [item for item in pool if item.default]
        if defaults:
            pool = defaults
    elif quality_key not in {"max_bitrate", "max_resolution", "max_real_bitrate"}:
        exact = [item for item in pool if _normal_gear(item.gear) == quality_key]
        if not exact:
            available = ", ".join(sorted({item.gear for item in pool}))
            raise ResolverError(f"找不到清晰度 {quality!r}；可用档位：{available}")
        pool = exact

    def common_score(item: StreamCandidate) -> tuple[int, int, int, int]:
        protocol_score = 2 if item.protocol == "flv" else 1 if item.protocol == "hls" else 0
        source_score = 1 if item.source == "stream_url" else 0
        return item.rank_hint, protocol_score, source_score, 1 if item.default else 0

    if quality_key == "max_resolution":
        return max(
            pool,
            key=lambda item: (
                item.pixels,
                item.effective_bitrate,
                item.fps,
                *common_score(item),
            ),
        )
    if quality_key == "max_real_bitrate":
        return max(
            pool,
            key=lambda item: (
                item.realtime_bitrate or item.declared_bitrate,
                item.declared_bitrate,
                item.pixels,
                item.fps,
                *common_score(item),
            ),
        )
    if quality_key in {"max_bitrate", "source"}:
        return max(
            pool,
            key=lambda item: (
                item.effective_bitrate,
                item.realtime_bitrate,
                item.pixels,
                item.fps,
                *common_score(item),
            ),
        )
    return max(
        pool,
        key=lambda item: (
            item.effective_bitrate,
            item.pixels,
            item.fps,
            *common_score(item),
        ),
    )


class DouyinWebClient:
    def __init__(self, timeout: float = 25.0, cookie: str = "", proxy: str | None = None) -> None:
        self.timeout = timeout
        self.cookie = cookie.strip()
        self.proxy = (proxy or "").strip() or None
        self.jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        ]
        if self.proxy:
            handlers.insert(
                0,
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}),
            )
        self.opener = urllib.request.build_opener(*handlers)

    def _request(
        self,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        referer: str = "https://live.douyin.com/",
        retries: int = 2,
    ) -> tuple[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Accept-Encoding": "gzip",
            "Referer": referer,
            "Connection": "close",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(url, headers=headers, method="GET")
        last_error: BaseException | None = None
        for attempt in range(retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    charset = response.headers.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace"), response.geturl()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                time.sleep(0.6 * (attempt + 1))
        raise ResolverError(f"请求失败：{url} ({last_error})")

    @staticmethod
    def _ids_from_url_and_html(url: str, body: str) -> tuple[str, str]:
        web_rid = ""
        room_id = ""
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("web_rid"):
            web_rid = query["web_rid"][0]
        if query.get("room_id"):
            room_id = query["room_id"][0]

        if parsed.netloc.endswith("live.douyin.com"):
            segment = parsed.path.strip("/").split("/")[0]
            if segment and segment not in {"hot_live", "category", "search"}:
                web_rid = segment
        elif parsed.netloc.endswith("www.douyin.com"):
            follow = re.fullmatch(r"/follow/live/(\d+)/?", parsed.path)
            if follow:
                web_rid = follow.group(1)

        patterns = (
            ("web", r'(?:\\?"web_rid\\?"\s*:\s*\\?")(\d+)'),
            ("web", r"[?&]web_rid=(\d+)"),
            ("room", r'(?:\\?"roomId\\?"\s*:\s*\\?")(\d{15,})'),
            ("room", r'(?:\\?"room_id\\?"\s*:\s*\\?")(\d{15,})'),
            ("room", r"[?&]room_id=(\d{15,})"),
        )
        for kind, pattern in patterns:
            match = re.search(pattern, body)
            if not match:
                continue
            if kind == "web" and not web_rid:
                web_rid = match.group(1)
            elif kind == "room" and not room_id:
                room_id = match.group(1)
        return web_rid, room_id

    def resolve_ids(self, target: str) -> tuple[str, str, str]:
        target = target.strip()
        if re.fullmatch(r"\d+", target):
            if len(target) >= 16:
                return "", target, "https://live.douyin.com/"
            page_url = f"https://live.douyin.com/{target}"
            body, final_url = self._request(page_url)
            web_rid, room_id = self._ids_from_url_and_html(final_url, body)
            return web_rid or target, room_id, final_url

        if not re.match(r"^https?://", target, flags=re.I):
            match = re.search(r"https?://[^\s]+", target)
            if not match:
                raise ResolverError("请输入直播间 URL、分享短链、web_rid 或 room_id")
            target = match.group(0).rstrip("，。,.!！")

        body, final_url = self._request(target)
        web_rid, room_id = self._ids_from_url_and_html(final_url, body)
        if not web_rid and not room_id:
            web_rid, room_id = self._ids_from_url_and_html(target, body)
        if not web_rid and not room_id:
            raise ResolverError(f"无法从分享链接解析直播间标识：{final_url}")
        return web_rid, room_id, final_url

    def fetch_room(self, web_rid: str, room_id: str = "") -> RoomResult:
        referer = f"https://live.douyin.com/{web_rid}" if web_rid else "https://live.douyin.com/"
        if web_rid and not any(cookie.name == "ttwid" for cookie in self.jar):
            page, final_url = self._request(referer)
            parsed_web_rid, parsed_room_id = self._ids_from_url_and_html(final_url, page)
            web_rid = parsed_web_rid or web_rid
            room_id = room_id or parsed_room_id
            referer = final_url

        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "live_id": "1",
            "device_platform": "web",
            "language": "zh-CN",
            "enter_from": "web_live",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "138.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "138.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": str(os.cpu_count() or 8),
            "device_memory": "8",
            "platform": "PC",
            "web_rid": web_rid,
            "room_id_str": room_id,
            "enter_source": "",
            "is_need_double_stream": "false",
        }
        url = f"{WEB_ENTER_URL}?{urllib.parse.urlencode(params)}"
        body, _ = self._request(
            url,
            accept="application/json, text/plain, */*",
            referer=referer,
        )
        try:
            response = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ResolverError(f"直播接口返回的不是 JSON：{exc}") from exc
        if _to_int(response.get("status_code")) != 0:
            raise ResolverError(
                f"直播接口错误 status_code={response.get('status_code')}: "
                f"{response.get('status_msg') or response.get('message') or ''}"
            )

        payload = _as_dict(response.get("data"))
        rooms = payload.get("data")
        if isinstance(rooms, dict):
            room_list = [rooms]
        elif isinstance(rooms, list):
            room_list = [_as_dict(item) for item in rooms]
        else:
            room_list = []
        if not room_list:
            raise RoomOfflineError("直播接口未返回目标房间")

        enter_room_id = str(payload.get("enter_room_id") or room_id or "")
        room = next(
            (item for item in room_list if str(item.get("id_str") or item.get("id")) == enter_room_id),
            room_list[0],
        )
        resolved_room_id = str(room.get("id_str") or room.get("id") or enter_room_id)
        owner_obj = _as_dict(room.get("owner"))
        return RoomResult(
            room=room,
            response=response,
            web_rid=web_rid,
            room_id=resolved_room_id,
            title=str(room.get("title") or ""),
            owner=str(owner_obj.get("nickname") or ""),
            referer=referer,
        )

    def resolve(self, target: str) -> RoomResult:
        path = Path(target)
        if path.is_file():
            try:
                response = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResolverError(f"无法读取 JSON 文件：{exc}") from exc
            payload = _as_dict(response.get("data"))
            rooms = payload.get("data")
            if isinstance(rooms, list) and rooms:
                room = _as_dict(rooms[0])
            elif isinstance(response.get("room"), dict):
                room = _as_dict(response.get("room"))
            elif "stream_url" in response:
                room = response
            else:
                raise ResolverError("JSON 中未找到 room/stream_url")
            owner_obj = _as_dict(room.get("owner"))
            return RoomResult(
                room=room,
                response=response,
                room_id=str(room.get("id_str") or room.get("id") or ""),
                title=str(room.get("title") or ""),
                owner=str(owner_obj.get("nickname") or ""),
            )

        web_rid, room_id, _ = self.resolve_ids(target)
        return self.fetch_room(web_rid, room_id)


def _format_bitrate(value: int) -> str:
    if not value:
        return "-"
    return f"{value / 1_000_000:.3f}M"

