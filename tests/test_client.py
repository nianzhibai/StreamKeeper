from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from douyin_recorder.client import DouyinClient
from douyin_recorder.errors import InvalidDouyinUrl


class FakeStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def fetch_app_stream_data(self, url: str) -> dict:
        self.calls.append(("app", url))
        return {"source": "app"}

    async def fetch_web_stream_data(self, url: str) -> dict:
        self.calls.append(("web", url))
        return {"source": "web"}

    async def fetch_stream_url(self, data: dict, quality: str) -> object:
        self.calls.append(("stream", (data, quality)))
        return SimpleNamespace(
            platform="抖音",
            anchor_name="测试主播",
            is_live=True,
            title="测试直播",
            quality=quality,
            m3u8_url="https://example.com/live.m3u8",
            flv_url="https://example.com/live.flv?codec=h264",
            record_url="https://example.com/live.m3u8",
            live_url="https://live.douyin.com/123",
            extra={"stream_orientation": 1},
        )


class ClientTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stream = FakeStream()
        self.factory_kwargs = None

        def factory(**kwargs):
            self.factory_kwargs = kwargs
            return self.stream

        self.client = DouyinClient(proxy="http://127.0.0.1:7890", cookies="a=b", stream_factory=factory)

    async def test_live_url_uses_web_parser(self) -> None:
        info = await self.client.fetch(" https://live.douyin.com/123?foo=bar ", "HD")

        self.assertEqual(self.stream.calls[0][0], "web")
        self.assertEqual(info.anchor_name, "测试主播")
        self.assertEqual(info.quality, "HD")
        self.assertEqual(info.stream_orientation, 1)
        self.assertEqual(self.factory_kwargs["proxy_addr"], "http://127.0.0.1:7890")

    async def test_share_and_profile_urls_use_app_parser(self) -> None:
        await self.client.fetch("https://v.douyin.com/AbCdE/")
        self.assertEqual(self.stream.calls[0][0], "app")

        stream = FakeStream()
        client = DouyinClient(stream_factory=lambda **_: stream)
        await client.fetch("https://www.douyin.com/user/abc")
        self.assertEqual(stream.calls[0][0], "app")


class UrlValidationTests(TestCase):
    def test_rejects_non_douyin_urls(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://example.com/live/123")

    def test_rejects_host_suffix_attack(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://douyin.com.example.org/live/123")

    def test_rejects_unknown_douyin_subdomain(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://foo.douyin.com/live/123")

    def test_rejects_unsupported_www_path(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://www.douyin.com/video/123")
