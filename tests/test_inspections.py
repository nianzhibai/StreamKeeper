from unittest import TestCase

from stream_keeper.models import LiveInfo
from stream_keeper.web.inspections import InspectionHandoffStore


def make_info() -> LiveInfo:
    return LiveInfo(
        platform="抖音",
        anchor_name="测试主播",
        is_live=True,
        title="测试直播",
        quality="OD",
        m3u8_url="https://example.com/live.m3u8?token=secret",
        flv_url="https://example.com/live.flv?token=secret",
        record_url="https://example.com/live.flv?token=secret",
        live_url="https://live.douyin.com/123456789",
    )


class InspectionHandoffStoreTests(TestCase):
    def test_handoff_is_bound_to_room_and_quality_and_consumed_once(self) -> None:
        store = InspectionHandoffStore()
        info = make_info()
        token = store.issue(info.live_url or "", "OD", info)

        self.assertIs(store.consume(token, url=info.live_url or "", quality="OD"), info)
        self.assertIsNone(store.consume(token, url=info.live_url or "", quality="OD"))

        mismatched = store.issue(info.live_url or "", "OD", info)
        self.assertIsNone(store.consume(mismatched, url=info.live_url or "", quality="HD"))
        self.assertIsNone(store.consume(mismatched, url=info.live_url or "", quality="OD"))

    def test_expired_handoff_is_not_reused(self) -> None:
        now = 100.0
        store = InspectionHandoffStore(ttl_seconds=30, clock=lambda: now)
        info = make_info()
        token = store.issue(info.live_url or "", "OD", info)

        now = 130.0

        self.assertIsNone(store.consume(token, url=info.live_url or "", quality="OD"))

    def test_cache_is_bounded(self) -> None:
        store = InspectionHandoffStore(max_entries=2)
        info = make_info()
        first = store.issue(info.live_url or "", "OD", info)
        second = store.issue(info.live_url or "", "OD", info)
        third = store.issue(info.live_url or "", "OD", info)

        self.assertIsNone(store.consume(first, url=info.live_url or "", quality="OD"))
        self.assertIs(store.consume(second, url=info.live_url or "", quality="OD"), info)
        self.assertIs(store.consume(third, url=info.live_url or "", quality="OD"), info)
