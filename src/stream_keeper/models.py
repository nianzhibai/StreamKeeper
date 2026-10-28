from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AnchorIdentity:
    platform: str
    anchor_id: str

    def __post_init__(self) -> None:
        if not self.platform.strip() or not self.anchor_id.strip():
            raise ValueError("主播身份不能为空")


def _read_field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True, slots=True)
class LiveInfo:
    platform: str | None
    anchor_name: str | None
    is_live: bool
    title: str | None
    quality: str | None
    m3u8_url: str | None
    flv_url: str | None
    record_url: str | None
    live_url: str | None
    stream_orientation: int | None = None
    flv_codec: str | None = None
    hls_codec: str | None = None

    @classmethod
    def from_stream_data(cls, value: object) -> LiveInfo:
        extra = _read_field(value, "extra")
        orientation = extra.get("stream_orientation") if isinstance(extra, Mapping) else None
        return cls(
            platform=_read_field(value, "platform"),
            anchor_name=_read_field(value, "anchor_name"),
            is_live=bool(_read_field(value, "is_live", False)),
            title=_read_field(value, "title"),
            quality=_read_field(value, "quality"),
            m3u8_url=_read_field(value, "m3u8_url"),
            flv_url=_read_field(value, "flv_url"),
            record_url=_read_field(value, "record_url"),
            live_url=_read_field(value, "live_url"),
            stream_orientation=orientation,
            flv_codec=_read_field(value, "flv_codec"),
            hls_codec=_read_field(value, "hls_codec"),
        )


@dataclass(frozen=True, slots=True)
class SelectedSource:
    kind: str
    url: str
    codec: str | None = None


@dataclass(frozen=True, slots=True)
class RecordingResult:
    output_path: str
    source: SelectedSource
    return_code: int
    limit_reached: bool = False
