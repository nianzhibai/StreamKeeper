"""HTTP transport and room resolution for Douyin's Web endpoints."""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ...errors import ResolverError
from .parser import (
    RoomResult,
    build_room_result,
    extract_room_ids,
    parse_enter_room_response,
    parse_reflow_room,
    parse_room_document,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
WEB_ENTER_URL = "https://live.douyin.com/webcast/room/web/enter/"


class DouyinRoomResolver:
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

    def _resolve_ids_and_page(self, target: str) -> tuple[str, str, str, str]:
        target = target.strip()
        if re.fullmatch(r"\d+", target):
            if len(target) >= 16:
                return "", target, "https://live.douyin.com/", ""
            page_url = f"https://live.douyin.com/{target}"
            body, final_url = self._request(page_url)
            web_rid, room_id = extract_room_ids(final_url, body)
            return web_rid or target, room_id, final_url, body

        if not re.match(r"^https?://", target, flags=re.I):
            match = re.search(r"https?://[^\s]+", target)
            if not match:
                raise ResolverError("请输入直播间 URL、分享短链、web_rid 或 room_id")
            target = match.group(0).rstrip("，。,.!！")

        body, final_url = self._request(target)
        web_rid, room_id = extract_room_ids(final_url, body)
        if not web_rid and not room_id:
            web_rid, room_id = extract_room_ids(target, body)
        if not web_rid and not room_id:
            raise ResolverError(f"无法从分享链接解析直播间标识：{final_url}")
        return web_rid, room_id, final_url, body

    def resolve_ids(self, target: str) -> tuple[str, str, str]:
        web_rid, room_id, final_url, _ = self._resolve_ids_and_page(target)
        return web_rid, room_id, final_url

    def fetch_room(self, web_rid: str, room_id: str = "") -> RoomResult:
        referer = f"https://live.douyin.com/{web_rid}" if web_rid else "https://live.douyin.com/"
        if web_rid and not any(cookie.name == "ttwid" for cookie in self.jar):
            page, final_url = self._request(referer)
            parsed_web_rid, parsed_room_id = extract_room_ids(final_url, page)
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
        return parse_enter_room_response(
            response,
            web_rid=web_rid,
            room_id=room_id,
            referer=referer,
        )

    def resolve(self, target: str) -> RoomResult:
        path = Path(target)
        if path.is_file():
            try:
                response = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResolverError(f"无法读取 JSON 文件：{exc}") from exc
            return parse_room_document(response)

        web_rid, room_id, final_url, body = self._resolve_ids_and_page(target)
        reflow_room = parse_reflow_room(final_url, body)
        if reflow_room is not None:
            return build_room_result(
                reflow_room,
                {"data": {"data": [reflow_room]}},
                web_rid=web_rid,
                room_id=room_id,
                referer=final_url,
            )
        return self.fetch_room(web_rid, room_id)
