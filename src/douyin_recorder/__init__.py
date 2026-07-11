"""Standalone Douyin live recording primitives."""

from .client import DouyinClient
from .models import LiveInfo, RecordingResult, SelectedSource
from .recorder import Recorder, RecorderOptions

__all__ = [
    "DouyinClient",
    "LiveInfo",
    "Recorder",
    "RecorderOptions",
    "RecordingResult",
    "SelectedSource",
]

__version__ = "0.2.0"
