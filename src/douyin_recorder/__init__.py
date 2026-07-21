"""Standalone Douyin live recording primitives."""

from .client import DouyinClient
from .errors import ResolverError, RoomOfflineError
from .models import LiveInfo, RecordingResult, SelectedSource
from .recorder import Recorder, RecorderOptions
from .web_resolver import (
    DouyinWebClient,
    RoomResult,
    StreamCandidate,
    _format_bitrate,
    choose_candidate,
    collect_candidates,
)

__all__ = [
    "DouyinClient",
    "DouyinWebClient",
    "LiveInfo",
    "Recorder",
    "RecorderOptions",
    "RecordingResult",
    "ResolverError",
    "RoomOfflineError",
    "RoomResult",
    "SelectedSource",
    "StreamCandidate",
    "_format_bitrate",
    "choose_candidate",
    "collect_candidates",
]

__version__ = "0.5.0"
