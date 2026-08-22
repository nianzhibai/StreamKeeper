from __future__ import annotations

from unittest import IsolatedAsyncioTestCase, TestCase

import httpx

from stream_keeper import BilibiliClient
from stream_keeper.errors import BilibiliFetchError, InvalidBilibiliUrl


def _room_info(*, live_status: int = 1) -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "room_info": {
                "room_id": 123456,
                "title": "测试哔哩哔哩直播",
                "live_status": live_status,
            },
            "anchor_info": {"base_info": {"uname": "哔哩主播"}},
        },
    }


def _play_info(*, requested_qn: int = 250) -> dict:
    def codec(name: str, qn: int, suffix: str) -> dict:
        return {
            "codec_name": name,
            "current_qn": qn,
            "base_url": f"/live-{suffix}",
            "url_info": [{"host": "https://cdn.example", "extra": "?token=secret"}],
        }

    return {
        "code": 0,
        "message": "OK",
        "data": {
            "playurl_info": {
                "playurl": {
                    "stream": [
                        {
                            "protocol_name": "http_stream",
                            "format": [
                                {
                                    "format_name": "flv",
                                    "codec": [
                                        codec("hevc", 10000, "source.flv"),
                                        codec("avc", requested_qn, "selected.flv"),
                                    ],
                                }
                            ],
                        },
                        {
                            "protocol_name": "http_hls",
                            "format": [
                                {
                                    "format_name": "ts",
                                    "codec": [
                                        codec("hevc", 10000, "source.m3u8"),
                                        codec("avc", requested_qn, "selected.m3u8"),
                                    ],
                                }
                            ],
                        },
                    ]
                }
            }
        },
    }


class BilibiliClientTests(IsolatedAsyncioTestCase):
    async def test_live_room_returns_matching_flv_and_hls_quality(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("getH5InfoByRoom"):
                return httpx.Response(200, json=_room_info())
            if request.url.path.endswith("getRoomPlayInfo"):
                self.assertEqual(request.url.params["qn"], "250")
                return httpx.Response(200, json=_play_info())
            raise AssertionError(f"unexpected request: {request.url}")

        client = BilibiliClient(
            cookies="SESSDATA=test",
            transport=httpx.MockTransport(handler),
        )
        info = await client.fetch("https://live.bilibili.com/123456?from=share", "HD")

        self.assertEqual(info.platform, "哔哩哔哩")
        self.assertEqual(info.anchor_name, "哔哩主播")
        self.assertEqual(info.title, "测试哔哩哔哩直播")
        self.assertEqual(info.quality, "HD")
        self.assertEqual(info.flv_url, "https://cdn.example/live-selected.flv?token=secret")
        self.assertEqual(info.m3u8_url, "https://cdn.example/live-selected.m3u8?token=secret")
        self.assertEqual(info.live_url, "https://live.bilibili.com/123456")
        self.assertTrue(all(request.headers.get("cookie") == "SESSDATA=test" for request in requests))

    async def test_offline_room_does_not_request_stream_urls(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_room_info(live_status=0))

        info = await BilibiliClient(transport=httpx.MockTransport(handler)).fetch("https://live.bilibili.com/123456")

        self.assertFalse(info.is_live)
        self.assertIsNone(info.record_url)
        self.assertEqual(calls, 1)

    async def test_nearest_quality_wins_when_platform_downgrades_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("getH5InfoByRoom"):
                return httpx.Response(200, json=_room_info())
            if request.url.path.endswith("getRoomPlayInfo"):
                # Anonymous playback can return a nearby AVC tier alongside a
                # much higher HEVC/HDR tier, even when UHD (蓝光) was requested.
                return httpx.Response(200, json=_play_info(requested_qn=250))
            raise AssertionError(f"unexpected request: {request.url}")

        info = await BilibiliClient(transport=httpx.MockTransport(handler)).fetch(
            "https://live.bilibili.com/123456", "UHD"
        )

        self.assertIn("selected.flv", info.flv_url or "")
        self.assertIn("selected.m3u8", info.m3u8_url or "")

    async def test_legacy_play_api_is_used_when_modern_api_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("getH5InfoByRoom"):
                return httpx.Response(200, json=_room_info())
            if request.url.path.endswith("getRoomPlayInfo"):
                return httpx.Response(200, json={"code": -352, "message": "temporary", "data": None})
            if request.url.path.endswith("playUrl"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "OK",
                        "data": {"durl": [{"url": "https://cdn.example/legacy.flv?token=secret"}]},
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        info = await BilibiliClient(transport=httpx.MockTransport(handler)).fetch("https://live.bilibili.com/123456")

        self.assertEqual(info.flv_url, "https://cdn.example/legacy.flv?token=secret")
        self.assertIsNone(info.m3u8_url)

    async def test_short_link_must_redirect_to_a_live_room(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "b23.tv":
                return httpx.Response(302, headers={"Location": "https://live.bilibili.com/123456"})
            if request.url.host == "live.bilibili.com":
                return httpx.Response(200, text="<html></html>")
            if request.url.path.endswith("getH5InfoByRoom"):
                return httpx.Response(200, json=_room_info(live_status=0))
            raise AssertionError(f"unexpected request: {request.url}")

        info = await BilibiliClient(transport=httpx.MockTransport(handler)).fetch("https://b23.tv/AbCdE")
        self.assertEqual(info.live_url, "https://live.bilibili.com/123456")

    async def test_api_errors_are_explained(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"code": -400, "message": "房间不存在", "data": None})
        )
        with self.assertRaisesRegex(BilibiliFetchError, "房间不存在"):
            await BilibiliClient(transport=transport).fetch("https://live.bilibili.com/123456")


class BilibiliUrlTests(TestCase):
    def test_extracts_and_normalizes_room_url(self) -> None:
        self.assertEqual(
            BilibiliClient.validate_url("来哔哩哔哩看直播 https://live.bilibili.com/123456?foo=1。"),
            "https://live.bilibili.com/123456",
        )
        self.assertEqual(BilibiliClient.validate_url("https://b23.tv/AbCdE"), "https://b23.tv/AbCdE")

    def test_rejects_non_room_and_suffix_attack_urls(self) -> None:
        for value in (
            "https://www.bilibili.com/video/BV1test",
            "https://live.bilibili.com/p/eden/area-tags",
            "https://live.bilibili.com.example.org/123456",
        ):
            with self.subTest(value=value), self.assertRaises(InvalidBilibiliUrl):
                BilibiliClient.validate_url(value)
