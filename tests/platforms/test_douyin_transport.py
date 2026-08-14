from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import patch

from stream_keeper.platforms.douyin.parser import collect_candidates
from stream_keeper.platforms.douyin.transport import DouyinRoomResolver


class ShareLinkResolverTests(TestCase):
    def test_resolve_ids_reads_room_id_from_amemv_reflow_redirect(self) -> None:
        short_url = "https://v.douyin.com/S5jFGCPcYxM/"
        final_url = "https://webcast.amemv.com/douyin/webcast/reflow/7671667450550848283?share_platform=copy"
        resolver = DouyinRoomResolver()

        with patch.object(resolver, "_request", return_value=("not found", final_url)):
            self.assertEqual(
                resolver.resolve_ids(short_url),
                ("", "7671667450550848283", final_url),
            )

    def test_resolve_uses_embedded_room_from_amemv_reflow_page(self) -> None:
        short_url = "https://v.douyin.com/S5jFGCPcYxM/"
        final_url = "https://webcast.amemv.com/douyin/webcast/reflow/7671667450550848283?share_platform=copy"
        room = {
            "idStr": "7671667450550848283",
            "status": 2,
            "title": "测试直播",
            "owner": {"nickname": "测试主播"},
            "streamUrl": {
                "candidateResolution": ["SD1", "HD1"],
                "defaultResolution": "HD1",
                "resolutionName": {"SD1": "标清", "HD1": "超清"},
                "flvPullUrl": {"HD1": "https://cdn.example/live.flv"},
                "hlsPullUrlMap": {"HD1": "https://cdn.example/live.m3u8"},
                "streamOrientation": 1,
            },
        }
        payload = json.dumps(["$", "$L7", None, {"data": {"room": room}}], ensure_ascii=False)
        frame = json.dumps([1, "5:" + payload], ensure_ascii=False)
        body = f"<script>self.__rsc_f.push({frame})</script>"
        resolver = DouyinRoomResolver()

        with patch.object(resolver, "_request", return_value=(body, final_url)) as request:
            result = resolver.resolve(short_url)

        request.assert_called_once_with(short_url)
        self.assertEqual(result.room_id, "7671667450550848283")
        self.assertEqual(result.owner, "测试主播")
        self.assertEqual(result.room["stream_url"]["default_resolution"], "HD1")
        self.assertEqual(
            {candidate.url for candidate in collect_candidates(result.room)},
            {"https://cdn.example/live.flv", "https://cdn.example/live.m3u8"},
        )
