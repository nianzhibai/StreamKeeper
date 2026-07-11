class DouyinRecorderError(Exception):
    """Base exception for this package."""


class InvalidDouyinUrl(DouyinRecorderError, ValueError):
    """Raised when a URL is not a supported Douyin URL."""


class DouyinFetchError(DouyinRecorderError):
    """Raised when live room information cannot be fetched."""


class SourceUnavailableError(DouyinRecorderError):
    """Raised when the requested FLV or HLS source is unavailable."""


class FFmpegNotFoundError(DouyinRecorderError):
    """Raised when the FFmpeg executable cannot be found."""


class FFmpegRecordingError(DouyinRecorderError):
    """Raised when FFmpeg exits with an error."""
