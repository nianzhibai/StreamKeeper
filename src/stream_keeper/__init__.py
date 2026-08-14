"""StreamKeeper live-stream monitoring and recording primitives."""

from .errors import (
    BilibiliFetchError,
    InvalidBilibiliUrl,
    InvalidKuaishouUrl,
    InvalidLiveUrl,
    KuaishouFetchError,
    LiveFetchError,
    ResolverError,
    RoomOfflineError,
    StreamKeeperError,
)
from .models import LiveInfo, RecordingResult, SelectedSource
from .platforms import (
    BilibiliClient,
    DouyinClient,
    KuaishouClient,
    LiveStreamClient,
)
from .recorder import Recorder, RecorderOptions

__all__ = [
    "BilibiliClient",
    "BilibiliFetchError",
    "DouyinClient",
    "InvalidBilibiliUrl",
    "InvalidKuaishouUrl",
    "InvalidLiveUrl",
    "KuaishouClient",
    "KuaishouFetchError",
    "LiveInfo",
    "LiveFetchError",
    "LiveStreamClient",
    "Recorder",
    "RecorderOptions",
    "RecordingResult",
    "ResolverError",
    "RoomOfflineError",
    "SelectedSource",
    "StreamKeeperError",
]

__version__ = "0.6.0"
