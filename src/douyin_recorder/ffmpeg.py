from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import SourceUnavailableError
from .models import LiveInfo, SelectedSource

FFMPEG_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) "
    "AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/14.2 "
    "Chrome/87.0.4280.141 Mobile Safari/537.36"
)

FORMAT_NAMES = {"ts", "mp4", "mkv", "flv"}
SOURCE_NAMES = {"auto", "flv", "hls"}


def get_codec(url: str | None) -> str | None:
    if not url:
        return None
    query = parse_qs(urlparse(url).query)
    for key, values in query.items():
        if key.lower() == "codec" and values:
            return values[0].lower()
    return None


def is_hevc(url: str | None) -> bool:
    return get_codec(url) in {"h265", "hevc"}


def choose_source(info: LiveInfo, preference: str = "auto") -> SelectedSource:
    preference = preference.lower()
    if preference not in SOURCE_NAMES:
        raise ValueError(f"不支持的直播源类型: {preference}")

    flv_url = info.flv_url
    hls_url = info.m3u8_url
    record_url = info.record_url

    if preference == "flv":
        if not flv_url:
            raise SourceUnavailableError("当前直播间没有可用的 FLV 地址")
        return SelectedSource("flv", flv_url, get_codec(flv_url))

    if preference == "hls":
        if not hls_url:
            raise SourceUnavailableError("当前直播间没有可用的 HLS 地址")
        return SelectedSource("hls", hls_url, get_codec(hls_url))

    if flv_url and not is_hevc(flv_url):
        return SelectedSource("flv", flv_url, get_codec(flv_url))
    if hls_url:
        return SelectedSource("hls", hls_url, get_codec(hls_url))
    if record_url:
        kind = "hls" if ".m3u8" in urlparse(record_url).path.lower() else "flv"
        return SelectedSource(kind, record_url, get_codec(record_url))
    if flv_url:
        return SelectedSource("flv", flv_url, get_codec(flv_url))
    raise SourceUnavailableError("直播状态为开播，但没有找到可录制的 FLV/HLS 地址")


def build_ffmpeg_command(
    source_url: str,
    output_path: str | Path,
    *,
    output_format: str = "ts",
    segment_seconds: int = 0,
    proxy: str | None = None,
    executable: str = "ffmpeg",
    loglevel: str = "warning",
) -> list[str]:
    output_format = output_format.lower()
    if output_format not in FORMAT_NAMES:
        raise ValueError(f"不支持的保存格式: {output_format}")
    if segment_seconds < 0:
        raise ValueError("segment_seconds 不能小于 0")

    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        loglevel,
        "-y",
        "-rw_timeout",
        "15000000",
        "-user_agent",
        FFMPEG_USER_AGENT,
        "-protocol_whitelist",
        "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy",
        "-thread_queue_size",
        "1024",
        "-analyzeduration",
        "20000000",
        "-probesize",
        "10000000",
        "-fflags",
        "+discardcorrupt+genpts",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "60",
    ]
    if proxy:
        command.extend(["-http_proxy", proxy])
    command.extend(
        [
            "-i",
            source_url,
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-sn",
            "-dn",
        ]
    )

    segment_format = {
        "ts": "mpegts",
        "mp4": "mp4",
        "mkv": "matroska",
        "flv": "flv",
    }[output_format]
    if segment_seconds:
        command.extend(
            [
                "-f",
                "segment",
                "-segment_time",
                str(segment_seconds),
                "-segment_format",
                segment_format,
                "-reset_timestamps",
                "1",
            ]
        )
        if output_format == "ts":
            command.extend(["-segment_format_options", "mpegts_flags=+resend_headers"])
        elif output_format == "mp4":
            command.extend(["-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof"])
    elif output_format == "ts":
        command.extend(["-f", "mpegts", "-mpegts_flags", "+resend_headers"])
    elif output_format == "mp4":
        command.extend(["-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof"])
    else:
        command.extend(["-f", segment_format])

    command.append(str(output_path))
    return command
