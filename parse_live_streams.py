#!/usr/bin/env python3
"""Parse a Douyin live room URL and print all available stream qualities."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

from douyin_recorder import (
    DouyinWebClient,
    ResolverError,
    RoomOfflineError,
    StreamCandidate,
    _format_bitrate,
    collect_candidates,
)


def _group_by_gear(candidates: list[StreamCandidate]) -> dict[str, list[StreamCandidate]]:
    grouped: dict[str, list[StreamCandidate]] = defaultdict(list)
    for item in candidates:
        grouped[item.gear].append(item)
    return grouped


def _sort_gears(gears: list[str], candidates: list[StreamCandidate]) -> list[str]:
    best: dict[str, StreamCandidate] = {}
    for item in candidates:
        cur = best.get(item.gear)
        if cur is None or item.effective_bitrate > cur.effective_bitrate or (
            item.effective_bitrate == cur.effective_bitrate and item.pixels > cur.pixels
        ):
            best[item.gear] = item

    def key(gear: str) -> tuple[int, int, int]:
        item = best[gear]
        return (item.effective_bitrate, item.pixels, item.rank_hint)

    return sorted(gears, key=key, reverse=True)


def format_text(room: Any, candidates: list[StreamCandidate]) -> str:
    lines: list[str] = []
    lines.append(f"直播间: {room.owner or '-'} | {room.title or '-'}")
    lines.append(f"web_rid={room.web_rid or '-'}  room_id={room.room_id or '-'}")
    lines.append(f"清晰度数量: {len({c.gear for c in candidates})}  线路/协议候选: {len(candidates)}")
    lines.append("")

    grouped = _group_by_gear(candidates)
    for gear in _sort_gears(list(grouped), candidates):
        items = grouped[gear]
        sample = max(items, key=lambda x: (x.effective_bitrate, x.pixels))
        res = f"{sample.width}x{sample.height}" if sample.width and sample.height else "-"
        name = sample.display_name or gear
        lines.append(
            f"[{gear}] {name}  {res}  "
            f"码率={_format_bitrate(sample.effective_bitrate)}  "
            f"实时≈{_format_bitrate(sample.realtime_bitrate)}  "
            f"fps={sample.fps or '-'}  codec={sample.codec or '-'}"
        )
        for item in sorted(items, key=lambda x: (x.line, x.protocol)):
            lines.append(f"  - {item.protocol}/{item.line}: {item.url}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_json(room: Any, candidates: list[StreamCandidate]) -> str:
    payload = {
        "room": {
            "web_rid": room.web_rid,
            "room_id": room.room_id,
            "title": room.title,
            "owner": room.owner,
            "referer": room.referer,
        },
        "streams": [item.as_dict(include_url=True) for item in candidates],
        "by_quality": {},
    }
    grouped = _group_by_gear(candidates)
    by_quality: dict[str, Any] = {}
    for gear in _sort_gears(list(grouped), candidates):
        items = grouped[gear]
        sample = max(items, key=lambda x: (x.effective_bitrate, x.pixels))
        by_quality[gear] = {
            "display_name": sample.display_name or gear,
            "width": sample.width,
            "height": sample.height,
            "declared_bitrate": sample.declared_bitrate,
            "realtime_bitrate": sample.realtime_bitrate,
            "fps": sample.fps,
            "codec": sample.codec,
            "urls": [
                {
                    "protocol": item.protocol,
                    "line": item.line,
                    "url": item.url,
                }
                for item in sorted(items, key=lambda x: (x.line, x.protocol))
            ],
        }
    payload["by_quality"] = by_quality
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="解析抖音直播间，输出全部清晰度拉流地址（Web 接口，无需登录）"
    )
    parser.add_argument(
        "target",
        help="直播间 URL / web_rid / room_id，例如 https://live.douyin.com/187402346776",
    )
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--cookie", default="", help="可选 Cookie")
    parser.add_argument("--timeout", type=float, default=25.0, help="HTTP 超时秒数")
    args = parser.parse_args(argv)

    try:
        client = DouyinWebClient(timeout=args.timeout, cookie=args.cookie)
        room = client.resolve(args.target)
        candidates = collect_candidates(room.room)
        if not candidates:
            print("错误: 未解析到任何直播流（可能已下播或接口无 stream_url）", file=sys.stderr)
            return 2
        if args.json:
            print(format_json(room, candidates))
        else:
            print(format_text(room, candidates))
        return 0
    except RoomOfflineError as exc:
        print(f"错误: 直播间未开播 - {exc}", file=sys.stderr)
        return 3
    except ResolverError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
