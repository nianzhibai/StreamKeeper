from unittest import TestCase

from douyin_recorder.ffmpeg import build_ffmpeg_command, choose_source, get_codec
from douyin_recorder.models import LiveInfo


def live_info(*, flv: str | None, hls: str | None, record: str | None = None) -> LiveInfo:
    return LiveInfo(
        platform="抖音",
        anchor_name="主播",
        is_live=True,
        title=None,
        quality="OD",
        m3u8_url=hls,
        flv_url=flv,
        record_url=record,
        live_url="https://live.douyin.com/123",
    )


class SourceSelectionTests(TestCase):
    def test_auto_prefers_h264_flv(self) -> None:
        selected = choose_source(
            live_info(flv="https://example.com/live.flv?codec=h264", hls="https://example.com/live.m3u8")
        )
        self.assertEqual(selected.kind, "flv")

    def test_auto_falls_back_to_hls_for_hevc_flv(self) -> None:
        selected = choose_source(
            live_info(flv="https://example.com/live.flv?codec=h265", hls="https://example.com/live.m3u8")
        )
        self.assertEqual(selected.kind, "hls")

    def test_codec_query_is_case_insensitive(self) -> None:
        self.assertEqual(get_codec("https://example.com/live.flv?Codec=HEVC"), "hevc")


class FFmpegCommandTests(TestCase):
    def test_proxy_is_an_input_option(self) -> None:
        command = build_ffmpeg_command(
            "https://example.com/live.m3u8",
            "output.ts",
            proxy="http://127.0.0.1:7890",
        )
        self.assertLess(command.index("-http_proxy"), command.index("-i"))
        self.assertEqual(command[-1], "output.ts")
        self.assertIn("mpegts", command)
        self.assertNotIn("-reconnect_at_eof", command)

    def test_segmented_mp4_command(self) -> None:
        command = build_ffmpeg_command(
            "https://example.com/live.flv",
            "output_%03d.mp4",
            output_format="mp4",
            segment_seconds=600,
        )
        self.assertIn("segment", command)
        self.assertIn("600", command)
        self.assertIn("movflags=+frag_keyframe+empty_moov+default_base_moof", command)

    def test_segment_count_limits_total_output_duration(self) -> None:
        command = build_ffmpeg_command(
            "https://example.com/live.flv",
            "output_%03d.ts",
            segment_seconds=1800,
            segment_count=4,
        )

        self.assertEqual(command[command.index("-t") + 1], "7200")
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")
        self.assertLess(command.index("-t"), command.index("-f"))

    def test_segment_count_requires_segmentation(self) -> None:
        with self.assertRaisesRegex(ValueError, "segment_seconds"):
            build_ffmpeg_command(
                "https://example.com/live.flv",
                "output.ts",
                segment_seconds=0,
                segment_count=4,
            )
