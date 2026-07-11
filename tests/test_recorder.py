import asyncio
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

from douyin_recorder.errors import FFmpegRecordingError
from douyin_recorder.models import LiveInfo
from douyin_recorder.recorder import Recorder, RecorderOptions, create_output_path, redact_stream_urls, sanitize_name


class OutputPathTests(TestCase):
    def test_segment_count_requires_segment_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "分段时长"):
            RecorderOptions(segment_seconds=0, segment_count=4)

    def test_sanitizes_cross_platform_filename_characters(self) -> None:
        self.assertEqual(sanitize_name(" 主播:名字/测试?* "), "主播_名字_测试")
        self.assertEqual(sanitize_name("CON"), "_CON")

    def test_redacts_signed_stream_urls_from_ffmpeg_logs(self) -> None:
        message = "Error opening https://example.com/live.flv?token=secret at input"
        redacted = redact_stream_urls(message)
        self.assertNotIn("secret", redacted)
        self.assertEqual(redacted, "Error opening [stream-url] at input")

    def test_creates_anchor_directory_and_segment_template(self) -> None:
        info = LiveInfo(
            platform="抖音",
            anchor_name="测试/主播",
            is_live=True,
            title=None,
            quality="OD",
            m3u8_url=None,
            flv_url=None,
            record_url=None,
            live_url=None,
        )
        with TemporaryDirectory() as tmp:
            output = create_output_path(
                tmp,
                info,
                output_format="ts",
                segment_seconds=600,
                now=datetime(2026, 7, 11, 12, 30, 45),
            )
            self.assertTrue(output.parent.is_dir())
            self.assertEqual(output.name, "测试_主播_2026-07-11_12-30-45_%03d.ts")

    def test_segment_template_uses_a_new_prefix_when_segments_exist(self) -> None:
        info = LiveInfo(
            platform="抖音",
            anchor_name="主播",
            is_live=True,
            title=None,
            quality="OD",
            m3u8_url=None,
            flv_url=None,
            record_url=None,
            live_url=None,
        )
        with TemporaryDirectory() as tmp:
            first = create_output_path(tmp, info, output_format="ts", segment_seconds=60, name="直播")
            (first.parent / "直播_000.ts").touch()
            second = create_output_path(tmp, info, output_format="ts", segment_seconds=60, name="直播")
            self.assertEqual(second.name, "直播_1_%03d.ts")


class RecorderProcessTests(IsolatedAsyncioTestCase):
    async def test_progress_reader_returns_latest_output_time(self) -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(b"out_time_us=1800000000\nprogress=continue\nout_time_us=7200000000\n")
        stream.feed_eof()

        self.assertEqual(await Recorder._consume_progress(stream), 7200.0)

    async def test_process_start_error_is_wrapped(self) -> None:
        async def process_factory(*_args, **_kwargs):
            raise PermissionError("permission denied")

        info = LiveInfo(
            platform="抖音",
            anchor_name="主播",
            is_live=True,
            title=None,
            quality="OD",
            m3u8_url="https://example.com/live.m3u8",
            flv_url=None,
            record_url="https://example.com/live.m3u8",
            live_url="https://live.douyin.com/123",
        )
        with TemporaryDirectory() as tmp:
            recorder = Recorder(
                RecorderOptions(output_dir=tmp),
                process_factory=process_factory,
                executable_resolver=lambda _: "/fake/ffmpeg",
            )
            with self.assertRaisesRegex(FFmpegRecordingError, "无法启动 FFmpeg"):
                await recorder.record(info)
