from __future__ import annotations

import json
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx

from stream_keeper import KuaishouClient
from stream_keeper.errors import InvalidKuaishouUrl, KuaishouFetchError


def _page(state: dict) -> str:
    return (
        "<!doctype html><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + ";(function(){var s;})();</script>"
    )


def _live_state() -> dict:
    def rendition(bitrate: int, extension: str) -> dict:
        return {
            "bitrate": bitrate,
            "url": f"https://cdn.example/live-{bitrate}.{extension}?token=secret",
        }

    return {
        "room": {
            "liveStream": {
                "caption": "测试快手直播",
                "playUrls": {
                    "h264": {
                        "adaptationSet": {
                            "representation": [
                                rendition(600, "flv"),
                                rendition(1000, "flv"),
                                rendition(2000, "flv"),
                                rendition(600, "m3u8"),
                                rendition(1000, "m3u8"),
                                rendition(2000, "m3u8"),
                            ]
                        }
                    }
                },
            },
            "author": {"name": "快手主播"},
        }
    }


class KuaishouClientTests(IsolatedAsyncioTestCase):
    async def test_live_room_selects_quality_by_bitrate(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text=_page(_live_state()))

        client = KuaishouClient(cookies="did=test", transport=httpx.MockTransport(handler))
        info = await client.fetch("https://live.kuaishou.com/u/example?share=1", "HD")

        self.assertEqual(info.platform, "快手")
        self.assertEqual(info.anchor_name, "快手主播")
        self.assertEqual(info.title, "测试快手直播")
        self.assertEqual(info.quality, "HD")
        self.assertEqual(info.flv_url, "https://cdn.example/live-1000.flv?token=secret")
        self.assertEqual(info.m3u8_url, "https://cdn.example/live-1000.m3u8?token=secret")
        self.assertEqual(info.record_url, info.flv_url)
        self.assertEqual(requests[0].headers.get("cookie"), "did=test")

    async def test_live_page_javascript_undefined_values_are_supported(self) -> None:
        page = _page(_live_state()).replace('"room": {', '"authToken": undefined, "room": {', 1)
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=page))

        info = await KuaishouClient(transport=transport).fetch("https://live.kuaishou.com/u/example")

        self.assertTrue(info.is_live)
        self.assertEqual(info.anchor_name, "快手主播")

    def test_quality_index_fallback_matches_platform_order_without_bitrates(self) -> None:
        representations = [
            {"url": "https://cdn.example/low.flv"},
            {"url": "https://cdn.example/middle.flv"},
            {"url": "https://cdn.example/high.flv"},
        ]

        self.assertEqual(KuaishouClient._select_url(representations, "OD"), "https://cdn.example/high.flv")
        self.assertEqual(KuaishouClient._select_url(representations, "HD"), "https://cdn.example/low.flv")

    def test_h264_representations_are_preferred(self) -> None:
        play_urls = {
            "h265": {"adaptationSet": {"representation": [{"bitrate": 2000, "url": "https://cdn.example/h265.flv"}]}},
            "h264": {"adaptationSet": {"representation": [{"bitrate": 1000, "url": "https://cdn.example/h264.flv"}]}},
        }

        self.assertEqual(
            [item["url"] for item in KuaishouClient._representations(play_urls)],
            ["https://cdn.example/h264.flv"],
        )

    async def test_offline_profile_keeps_known_anchor(self) -> None:
        state = {"authorInfoById": {"userInfo": {"name": "离线主播", "living": False}}}
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=_page(state)))

        info = await KuaishouClient(transport=transport).fetch("https://live.kuaishou.com/profile/example")

        self.assertFalse(info.is_live)
        self.assertEqual(info.anchor_name, "离线主播")
        self.assertIsNone(info.record_url)

    async def test_authenticated_user_api_identifies_offline_room(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("userinfo/byid"):
                self.assertEqual(request.url.params["principalId"], "example")
                return httpx.Response(
                    200,
                    json={"data": {"result": 1, "userInfo": {"name": "离线主播", "living": False}}},
                )
            raise AssertionError("offline status should avoid fetching the room page")

        info = await KuaishouClient(
            cookies="did=test",
            transport=httpx.MockTransport(handler),
        ).fetch("https://live.kuaishou.com/u/example")

        self.assertFalse(info.is_live)
        self.assertEqual(info.anchor_name, "离线主播")
        self.assertEqual(len(requests), 1)

    async def test_offline_error_page_is_not_reported_as_failure(self) -> None:
        state = {
            "room": {
                "liveStream": None,
                "author": {"name": "离线主播"},
                "errorType": {"title": "主播暂未直播", "content": "稍后再来看看"},
            }
        }
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=_page(state)))

        info = await KuaishouClient(transport=transport).fetch("https://live.kuaishou.com/u/example")

        self.assertFalse(info.is_live)
        self.assertEqual(info.anchor_name, "离线主播")

    async def test_short_link_redirect_is_resolved_without_forwarding_cookie(self) -> None:
        seen: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host or "", request.headers.get("cookie")))
            if request.url.host == "v.kuaishou.com":
                return httpx.Response(302, headers={"Location": "https://live.kuaishou.com/u/example"})
            return httpx.Response(200, text=_page({"authorInfoById": {"userInfo": {"name": "主播"}}}))

        client = KuaishouClient(cookies="did=test", transport=httpx.MockTransport(handler))
        info = await client.fetch("https://v.kuaishou.com/AbCdE")

        self.assertFalse(info.is_live)
        self.assertEqual(seen[0], ("v.kuaishou.com", None))
        self.assertEqual(seen[-1], ("live.kuaishou.com", "did=test"))

    async def test_missing_initial_state_reports_risk_control(self) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="<html>captcha</html>"))
        with self.assertRaisesRegex(KuaishouFetchError, "风控"):
            await KuaishouClient(transport=transport).fetch("https://live.kuaishou.com/u/example")

    async def test_live_data_without_urls_is_not_treated_as_offline(self) -> None:
        state = {"room": {"liveStream": {}, "author": {"name": "主播"}}}
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=_page(state)))
        with self.assertRaisesRegex(KuaishouFetchError, "没有可用直播流"):
            await KuaishouClient(transport=transport).fetch("https://live.kuaishou.com/u/example")


class KuaishouUrlTests(TestCase):
    def test_extracts_supported_urls_from_share_text(self) -> None:
        self.assertEqual(
            KuaishouClient.validate_url("正在直播 https://live.kuaishou.com/u/example?foo=1。"),
            "https://live.kuaishou.com/u/example",
        )
        self.assertEqual(
            KuaishouClient.validate_url("https://live.kuaishou.com/profile/example"),
            "https://live.kuaishou.com/profile/example",
        )
        self.assertEqual(KuaishouClient.validate_url("https://v.kuaishou.com/AbCdE"), "https://v.kuaishou.com/AbCdE")

    def test_rejects_non_live_and_suffix_attack_urls(self) -> None:
        for value in (
            "https://www.kuaishou.com/short-video/example",
            "https://live.kuaishou.com/",
            "https://live.kuaishou.com.example.org/u/example",
        ):
            with self.subTest(value=value), self.assertRaises(InvalidKuaishouUrl):
                KuaishouClient.validate_url(value)
