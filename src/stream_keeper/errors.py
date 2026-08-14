class StreamKeeperError(Exception):
    """Base exception for this package."""


class InvalidLiveUrl(StreamKeeperError, ValueError):
    """Raised when a URL does not belong to a supported live platform."""


class InvalidDouyinUrl(InvalidLiveUrl):
    """Raised when a URL is not a supported Douyin URL."""


class InvalidBilibiliUrl(InvalidLiveUrl):
    """Raised when a URL is not a supported Bilibili live URL."""


class InvalidKuaishouUrl(InvalidLiveUrl):
    """Raised when a URL is not a supported Kuaishou live URL."""


class LiveFetchError(StreamKeeperError):
    """Raised when a supported platform cannot return live-room data."""


class DouyinFetchError(LiveFetchError):
    """Raised when live room information cannot be fetched."""


class BilibiliFetchError(LiveFetchError):
    """Raised when Bilibili live room information cannot be fetched."""


class KuaishouFetchError(LiveFetchError):
    """Raised when Kuaishou live room information cannot be fetched."""


class ResolverError(DouyinFetchError, RuntimeError):
    """Raised when the Web room/stream resolver fails."""


class RoomOfflineError(ResolverError):
    """Raised when the live room is offline or has no stream URLs."""


class SourceUnavailableError(StreamKeeperError):
    """Raised when the requested FLV or HLS source is unavailable."""


class FFmpegNotFoundError(StreamKeeperError):
    """Raised when the FFmpeg executable cannot be found."""


class FFmpegRecordingError(StreamKeeperError):
    """Raised when FFmpeg exits with an error."""
