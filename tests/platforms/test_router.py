from __future__ import annotations

from unittest import IsolatedAsyncioTestCase, TestCase

from stream_keeper import LiveStreamClient
from stream_keeper.errors import InvalidLiveUrl
from stream_keeper.models import LiveInfo


class FakePlatformClient:
    def __init__(self, platform: str, calls: list, **kwargs) -> None:
        self.platform = platform
        self.calls = calls
        self.calls.append(("init", platform, kwargs))

    async def fetch(self, url: str, quality: str) -> LiveInfo:
        self.calls.append(("fetch", self.platform, url, quality))
        return LiveInfo(
            platform=self.platform,
            anchor_name="主播",
            is_live=False,
            title=None,
            quality=None,
            m3u8_url=None,
            flv_url=None,
            record_url=None,
            live_url=url,
        )


class LiveStreamClientTests(IsolatedAsyncioTestCase):
    async def test_routes_each_supported_platform_and_uses_separate_cookies(self) -> None:
        calls = []

        def factory(platform: str):
            return lambda **kwargs: FakePlatformClient(platform, calls, **kwargs)

        client = LiveStreamClient(
            proxy="http://127.0.0.1:7890",
            douyin_cookies="douyin=1",
            bilibili_cookies="bilibili=1",
            kuaishou_cookies="kuaishou=1",
            douyin_client_factory=factory("抖音"),
            bilibili_client_factory=factory("哔哩哔哩"),
            kuaishou_client_factory=factory("快手"),
        )

        values = (
            ("https://live.douyin.com/123", "抖音", "douyin=1"),
            ("https://live.bilibili.com/456?share=1", "哔哩哔哩", "bilibili=1"),
            ("https://live.kuaishou.com/u/example", "快手", "kuaishou=1"),
        )
        for url, platform, _cookie in values:
            info = await client.fetch(url, "HD")
            self.assertEqual(info.platform, platform)

        init_calls = [call for call in calls if call[0] == "init"]
        self.assertEqual([call[2]["cookies"] for call in init_calls], [item[2] for item in values])
        self.assertTrue(all(call[2]["proxy"] == "http://127.0.0.1:7890" for call in init_calls))
        self.assertEqual(
            [call[2] for call in calls if call[0] == "fetch"],
            [
                "https://live.douyin.com/123",
                "https://live.bilibili.com/456",
                "https://live.kuaishou.com/u/example",
            ],
        )


class LiveStreamUrlTests(TestCase):
    def test_extracts_any_supported_platform_from_share_text(self) -> None:
        self.assertEqual(
            LiveStreamClient.validate_url("打开哔哩哔哩 https://live.bilibili.com/123456。"),
            "https://live.bilibili.com/123456",
        )
        self.assertEqual(
            LiveStreamClient.validate_url("打开快手 https://live.kuaishou.com/u/example。"),
            "https://live.kuaishou.com/u/example",
        )

    def test_rejects_unsupported_url(self) -> None:
        with self.assertRaisesRegex(InvalidLiveUrl, "抖音、哔哩哔哩和快手"):
            LiveStreamClient.validate_url("https://example.com/live/123")
