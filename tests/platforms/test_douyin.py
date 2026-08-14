from __future__ import annotations

from unittest import IsolatedAsyncioTestCase, TestCase

from stream_keeper.errors import InvalidDouyinUrl
from stream_keeper.platforms.douyin import DouyinClient
from stream_keeper.platforms.douyin.parser import RoomResult


def _live_room(*, status: int = 2) -> dict:
    return {
        "status": status,
        "title": "测试直播",
        "owner": {"nickname": "测试主播"},
        "stream_orientation": 1,
        "stream_url": {
            "stream_orientation": 1,
            "extra": {"width": 1088, "height": 1920},
            "live_core_sdk_data": {
                "pull_data": {
                    "options": {
                        "qualities": [
                            {"sdk_key": "origin", "name": "原画"},
                            {"sdk_key": "hd", "name": "超清"},
                            {"sdk_key": "sd", "name": "高清"},
                            {"sdk_key": "ld", "name": "标清"},
                            {"sdk_key": "md", "name": "流畅"},
                        ]
                    },
                    "stream_data": {
                        "data": {
                            "origin": {
                                "main": {
                                    "flv": "https://cdn.example/origin.flv",
                                    "hls": "https://cdn.example/origin.m3u8",
                                    "sdk_params": {
                                        "VCodec": "h264",
                                        "vbitrate": 3_000_000,
                                        "resolution": "1088x1920",
                                        "fps": 20,
                                    },
                                }
                            },
                            "hd": {
                                "main": {
                                    "flv": "https://cdn.example/hd.flv",
                                    "hls": "https://cdn.example/hd.m3u8",
                                    "sdk_params": {
                                        "VCodec": "h264",
                                        "vbitrate": 2_000_000,
                                        "resolution": "720x1280",
                                        "fps": 20,
                                    },
                                }
                            },
                            "sd": {
                                "main": {
                                    "flv": "https://cdn.example/sd.flv",
                                    "hls": "https://cdn.example/sd.m3u8",
                                    "sdk_params": {
                                        "VCodec": "h264",
                                        "vbitrate": 1_500_000,
                                        "resolution": "540x960",
                                        "fps": 20,
                                    },
                                }
                            },
                            "ld": {
                                "main": {
                                    "flv": "https://cdn.example/ld.flv",
                                    "hls": "https://cdn.example/ld.m3u8",
                                    "sdk_params": {
                                        "VCodec": "h264",
                                        "vbitrate": 1_000_000,
                                        "resolution": "480x848",
                                        "fps": 20,
                                    },
                                }
                            },
                            "md": {
                                "main": {
                                    "flv": "https://cdn.example/md.flv",
                                    "hls": "https://cdn.example/md.m3u8",
                                    "sdk_params": {
                                        "VCodec": "h264",
                                        "vbitrate": 250_000,
                                        "resolution": "240x424",
                                        "fps": 15,
                                    },
                                }
                            },
                        }
                    },
                }
            },
        },
    }


class FakeRoomResolver:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.targets: list[str] = []
        self.room = _live_room()

    def resolve(self, target: str) -> RoomResult:
        self.targets.append(target)
        owner = self.room.get("owner") or {}
        return RoomResult(
            room=self.room,
            response={"data": {"data": [self.room]}},
            web_rid="123",
            room_id="456",
            title=str(self.room.get("title") or ""),
            owner=str(owner.get("nickname") or ""),
            referer="https://live.douyin.com/123",
        )


class ClientTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.resolver = FakeRoomResolver()
        self.factory_kwargs = None

        def factory(**kwargs):
            self.factory_kwargs = kwargs
            self.resolver.kwargs = kwargs
            return self.resolver

        self.client = DouyinClient(
            proxy="http://127.0.0.1:7890",
            cookies="a=b",
            timeout=30,
            resolver_factory=factory,
        )

    async def test_live_url_uses_room_resolver(self) -> None:
        info = await self.client.fetch(" https://live.douyin.com/123?foo=bar ", "HD")

        self.assertEqual(self.resolver.targets[0], "https://live.douyin.com/123?foo=bar")
        self.assertEqual(info.anchor_name, "测试主播")
        self.assertEqual(info.quality, "HD")
        self.assertTrue(info.is_live)
        self.assertEqual(info.flv_url, "https://cdn.example/sd.flv")
        self.assertEqual(info.m3u8_url, "https://cdn.example/sd.m3u8")
        self.assertEqual(info.stream_orientation, 1)
        self.assertEqual(self.factory_kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(self.factory_kwargs["cookie"], "a=b")
        self.assertEqual(self.factory_kwargs["timeout"], 30)

    async def test_quality_mapping_matches_ui_labels(self) -> None:
        mapping = {
            "OD": "origin",
            "UHD": "hd",
            "HD": "sd",
            "SD": "ld",
            "LD": "md",
        }
        for quality, gear in mapping.items():
            info = await self.client.fetch("https://live.douyin.com/123", quality)
            self.assertEqual(info.flv_url, f"https://cdn.example/{gear}.flv", quality)

    async def test_offline_room_returns_is_live_false(self) -> None:
        self.resolver.room = _live_room(status=4)
        info = await self.client.fetch("https://live.douyin.com/123")
        self.assertFalse(info.is_live)
        self.assertIsNone(info.flv_url)

    async def test_share_and_profile_urls_are_accepted(self) -> None:
        await self.client.fetch("https://v.douyin.com/AbCdE/")
        self.assertEqual(self.resolver.targets[-1], "https://v.douyin.com/AbCdE/")

        await self.client.fetch("https://www.douyin.com/user/abc")
        self.assertEqual(self.resolver.targets[-1], "https://www.douyin.com/user/abc")

    async def test_share_text_extracts_short_url(self) -> None:
        share_text = (
            "8- #在抖音，记录美好生活#【痘痘小王（日常号）】正在直播，来和我一起支持Ta吧。"
            "复制下方链接，打开【抖音】，直接观看直播！ "
            "https://v.douyin.com/S5jFGCPcYxM/ 5@7.com :7pm"
        )

        await self.client.fetch(share_text)
        self.assertEqual(self.resolver.targets[-1], "https://v.douyin.com/S5jFGCPcYxM/")


class UrlValidationTests(TestCase):
    def test_extracts_supported_url_from_share_text(self) -> None:
        value = "正在直播，点击 https://live.douyin.com/123456789。打开抖音观看"

        self.assertEqual(DouyinClient.validate_url(value), "https://live.douyin.com/123456789")

    def test_rejects_non_douyin_urls(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://example.com/live/123")

    def test_rejects_host_suffix_attack(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://douyin.com.example.org/live/123")

        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("分享文本 https://v.douyin.com.evil.example/AbCdE/ 打开抖音")

    def test_rejects_unknown_douyin_subdomain(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://foo.douyin.com/live/123")

    def test_rejects_unsupported_www_path(self) -> None:
        with self.assertRaises(InvalidDouyinUrl):
            DouyinClient.validate_url("https://www.douyin.com/video/123")

    def test_normalizes_follow_live_url(self) -> None:
        self.assertEqual(
            DouyinClient.validate_url("https://www.douyin.com/follow/live/935126084727"),
            "https://live.douyin.com/935126084727",
        )

    def test_extracts_follow_live_url_from_share_text(self) -> None:
        value = "关注直播间 https://www.douyin.com/follow/live/935126084727 打开抖音"
        self.assertEqual(DouyinClient.validate_url(value), "https://live.douyin.com/935126084727")
