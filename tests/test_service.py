from unittest import IsolatedAsyncioTestCase

from stream_keeper.models import LiveInfo, RecordingResult, SelectedSource
from stream_keeper.service import RecordingService


def make_info(is_live: bool) -> LiveInfo:
    return LiveInfo(
        platform="抖音",
        anchor_name="测试主播",
        is_live=is_live,
        title="测试直播",
        quality="OD" if is_live else None,
        m3u8_url="https://example.com/live.m3u8" if is_live else None,
        flv_url="https://example.com/live.flv?codec=h264" if is_live else None,
        record_url="https://example.com/live.m3u8" if is_live else None,
        live_url="https://live.douyin.com/123",
    )


class FakeClient:
    def __init__(self, info: LiveInfo) -> None:
        self.info = info
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, url: str, quality: str) -> LiveInfo:
        self.calls.append((url, quality))
        return self.info


class FakeRecorder:
    def __init__(self) -> None:
        self.recorded: list[LiveInfo] = []

    async def record(self, info: LiveInfo) -> RecordingResult:
        self.recorded.append(info)
        return RecordingResult("recordings/test.ts", SelectedSource("flv", info.flv_url or ""), 0)


class ServiceTests(IsolatedAsyncioTestCase):
    async def test_once_returns_without_recording_when_offline(self) -> None:
        client = FakeClient(make_info(False))
        recorder = FakeRecorder()

        result = await RecordingService(client, recorder).run("https://live.douyin.com/123")

        self.assertFalse(result.live_info.is_live)
        self.assertIsNone(result.recording)
        self.assertEqual(recorder.recorded, [])

    async def test_once_records_when_live(self) -> None:
        info = make_info(True)
        client = FakeClient(info)
        recorder = FakeRecorder()

        result = await RecordingService(client, recorder).run(
            "https://live.douyin.com/123",
            quality="HD",
        )

        self.assertEqual(client.calls, [("https://live.douyin.com/123", "HD")])
        self.assertEqual(recorder.recorded, [info])
        self.assertEqual(result.recording.output_path, "recordings/test.ts")
