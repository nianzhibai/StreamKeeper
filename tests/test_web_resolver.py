from __future__ import annotations

import json
from unittest import TestCase

from douyin_recorder import choose_candidate, collect_candidates


def modern_room() -> dict:
    stream_data = {
        "common": {
            "auto": {"default": "origin", "list": ["ld", "hd", "origin"]},
            "query": {"trace": "hello world"},
        },
        "data": {
            "ld": {
                "main": {
                    "flv": "http://cdn.example/live_ld.flv?token=1",
                    "sdk_params": json.dumps(
                        {
                            "VCodec": "h264",
                            "vbitrate": 1_000_000,
                            "resolution": "960x540",
                            "fps": 25,
                        }
                    ),
                }
            },
            "origin": {
                "main": {
                    "flv": "http://cdn.example/live_origin.flv?token=2",
                    "hls": "http://cdn.example/live_origin.m3u8?token=2",
                    "sdk_params": json.dumps({"VCodec": "h264", "vbitrate": 4_200_000, "fps": 45}),
                    "templateRealTimeInfo": {"bitrateKbps": 4400},
                }
            },
            "hd": {
                "main": {
                    "flv": "http://cdn.example/live_hd.flv?token=3",
                    "hls": "http://cdn.example/live_hd.m3u8?token=3",
                    "sdk_params": json.dumps(
                        {
                            "VCodec": "h264",
                            "vbitrate": 6_000_000,
                            "resolution": "1280x720",
                            "fps": 45,
                        }
                    ),
                    "templateRealTimeInfo": {"bitrateKbps": 4900},
                }
            },
        },
    }
    return {
        "id_str": "1234567890123456789",
        "stream_url": {
            "extra": {"width": 1920, "height": 1080},
            "live_core_sdk_data": {
                "pull_data": {
                    "options": {
                        "default_quality": {"sdk_key": "origin"},
                        "qualities": [
                            {"sdk_key": "ld", "name": "标清"},
                            {"sdk_key": "hd", "name": "超清"},
                            {"sdk_key": "origin", "name": "原画"},
                        ],
                    },
                    "stream_data": json.dumps(stream_data, ensure_ascii=False),
                }
            },
        },
    }


class CandidateTests(TestCase):
    def test_max_bitrate_is_not_assumed_to_be_origin(self) -> None:
        candidates = collect_candidates(modern_room())
        selected = choose_candidate(candidates, "max-bitrate", "auto")
        self.assertEqual(selected.gear, "hd")
        self.assertEqual(selected.protocol, "flv")
        self.assertEqual(selected.declared_bitrate, 6_000_000)

    def test_source_selects_origin_and_uses_stream_extra_resolution(self) -> None:
        candidates = collect_candidates(modern_room())
        selected = choose_candidate(candidates, "source", "flv")
        self.assertEqual(selected.gear, "origin")
        self.assertEqual((selected.width, selected.height), (1920, 1080))

    def test_origin_uses_target_origin_bitrate_like_app_player(self) -> None:
        room = modern_room()
        pull_data = room["stream_url"]["live_core_sdk_data"]["pull_data"]
        stream_data = json.loads(pull_data["stream_data"])
        params = json.loads(stream_data["data"]["origin"]["main"]["sdk_params"])
        params["vbitrate"] = 555_008
        params["TargetOriginBitRate"] = 7_200_000
        stream_data["data"]["origin"]["main"]["sdk_params"] = json.dumps(params)
        pull_data["stream_data"] = json.dumps(stream_data)

        selected = choose_candidate(collect_candidates(room), "max-bitrate", "flv")
        self.assertEqual(selected.gear, "origin")
        self.assertEqual(selected.declared_bitrate, 7_200_000)

    def test_common_query_is_appended_like_the_app_player(self) -> None:
        candidates = collect_candidates(modern_room())
        selected = choose_candidate(candidates, "hd", "flv")
        self.assertIn("trace=hello+world", selected.url)
        self.assertIn("token=3&", selected.url)

    def test_legacy_fallback_uses_highest_known_gear(self) -> None:
        room = {
            "stream_url": {
                "candidate_resolution": ["SD1", "SD2", "HD1"],
                "default_resolution": "HD1",
                "flv_pull_url": {
                    "SD1": "http://cdn.example/ld.flv",
                    "SD2": "http://cdn.example/sd.flv",
                    "HD1": "http://cdn.example/hd.flv",
                    "FULL_HD1": "http://cdn.example/source.flv",
                },
            }
        }
        candidates = collect_candidates(room)
        selected = choose_candidate(candidates, "max-bitrate", "flv")
        self.assertEqual(selected.gear, "FULL_HD1")
