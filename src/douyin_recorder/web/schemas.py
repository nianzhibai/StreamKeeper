from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..client import DouyinClient
from ..settings import CLOUD_ARCHIVE_ROOT

Quality = Literal["OD", "UHD", "HD", "SD", "LD"]
OutputFormat = Literal["ts", "mp4", "mkv", "flv"]
SourcePreference = Literal["auto", "flv", "hls"]


class TaskStatus(str, Enum):
    STOPPED = "stopped"
    WAITING = "waiting"
    CHECKING = "checking"
    QUEUED = "queued"
    RECORDING = "recording"
    ERROR = "error"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class AuthSession(StrictModel):
    username: str
    csrf_token: str
    expires_at: datetime


class TaskConfig(StrictModel):
    url: str = Field(min_length=10, max_length=1000)
    label: str | None = Field(default=None, max_length=80)
    quality: Quality = "OD"
    output_format: OutputFormat = "ts"
    source: SourcePreference = "auto"
    segment_seconds: int = Field(default=1800, ge=0, le=86400)
    segment_count: int = Field(default=4, ge=0, le=10000)
    monitor: bool = True
    interval_seconds: int = Field(default=60, ge=10, le=86400)

    @field_validator("url")
    @classmethod
    def validate_douyin_url(cls, value: str) -> str:
        return DouyinClient.validate_url(value)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        normalized = value.strip() if value else None
        return normalized or None

    @model_validator(mode="after")
    def validate_segment_limit(self) -> TaskConfig:
        if self.segment_count and not self.segment_seconds:
            raise ValueError("设置段数时，分段时长必须大于 0")
        return self


class TaskCreate(TaskConfig):
    auto_start: bool = True


class TaskUpdate(StrictModel):
    url: str | None = Field(default=None, min_length=10, max_length=1000)
    label: str | None = Field(default=None, max_length=80)
    quality: Quality | None = None
    output_format: OutputFormat | None = None
    source: SourcePreference | None = None
    segment_seconds: int | None = Field(default=None, ge=0, le=86400)
    segment_count: int | None = Field(default=None, ge=0, le=10000)
    monitor: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)

    @field_validator("url")
    @classmethod
    def validate_douyin_url(cls, value: str | None) -> str | None:
        return DouyinClient.validate_url(value) if value is not None else None

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        normalized = value.strip() if value else None
        return normalized or None


class TaskRecord(TaskConfig):
    id: str
    enabled: bool
    status: TaskStatus
    status_message: str | None = None
    anchor_name: str | None = None
    live_title: str | None = None
    is_live: bool = False
    output_path: str | None = None
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None = None
    started_at: datetime | None = None
    recording_elapsed_seconds: float | None = None
    recording_segment_index: int | None = None
    recording_segment_progress: float | None = None


class InspectRequest(StrictModel):
    url: str = Field(min_length=10, max_length=1000)
    quality: Quality = "OD"

    @field_validator("url")
    @classmethod
    def validate_douyin_url(cls, value: str) -> str:
        return DouyinClient.validate_url(value)


class InspectResponse(StrictModel):
    anchor_name: str | None
    is_live: bool
    title: str | None
    quality: str | None
    live_url: str | None
    has_flv: bool
    has_hls: bool
    stream_orientation: int | None


class SystemInfo(StrictModel):
    ffmpeg_available: bool
    node_available: bool
    recordings_dir: str
    free_space_gb: float
    active_tasks: int
    recording_tasks: int
    max_concurrent_recordings: int


class RecordingEntry(StrictModel):
    name: str
    path: str
    kind: Literal["directory", "video"]
    size: int | None
    modified_at: datetime
    extension: str | None
    playback_mode: Literal["direct", "remux"] | None
    playable: bool


class RecordingDirectoryView(StrictModel):
    path: str
    entries: list[RecordingEntry]


class CloudQuarkUpdate(StrictModel):
    enabled: bool = False
    cookie: SecretStr | None = Field(default=None, max_length=32768)
    clear_cookie: bool = False
    root_id: str = Field(default="0", min_length=1, max_length=512)
    upload_path: str = Field(default=CLOUD_ARCHIVE_ROOT, max_length=1024)

    @field_validator("root_id", "upload_path")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class CloudWoPanUpdate(StrictModel):
    enabled: bool = False
    access_token: SecretStr | None = Field(default=None, max_length=4096)
    refresh_token: SecretStr | None = Field(default=None, max_length=4096)
    clear_tokens: bool = False
    root_id: str = Field(default="0", min_length=1, max_length=512)
    family_id: str = Field(default="", max_length=512)
    upload_path: str = Field(default=CLOUD_ARCHIVE_ROOT, max_length=1024)

    @field_validator("root_id", "family_id", "upload_path")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class CloudScheduleUpdate(StrictModel):
    hour: int = Field(default=1, ge=0, le=23)
    min_age_minutes: int = Field(default=10, ge=0, le=1440)
    timeout_seconds: int = Field(default=300, ge=30, le=86400)


class CloudArchiveUpdate(StrictModel):
    quark: CloudQuarkUpdate
    wopan: CloudWoPanUpdate
    schedule: CloudScheduleUpdate


class CloudQuarkView(StrictModel):
    enabled: bool
    credential_configured: bool
    root_id: str
    upload_path: str


class CloudWoPanView(StrictModel):
    enabled: bool
    access_token_configured: bool
    refresh_token_configured: bool
    root_id: str
    family_id: str
    upload_path: str


class CloudScheduleView(StrictModel):
    hour: int
    min_age_minutes: int
    timeout_seconds: int
    next_run_at: datetime | None


class CloudUploadSummaryView(StrictModel):
    scanned_files: int
    skipped_files: int
    uploaded_copies: int
    deleted_files: int
    failed_files: int


class CloudUploadExecutionView(StrictModel):
    trigger: Literal["manual", "scheduled"]
    status: Literal["running", "success", "partial", "failed", "cancelled"]
    started_at: datetime
    finished_at: datetime | None
    summary: CloudUploadSummaryView | None
    error: str | None


class CloudArchiveView(StrictModel):
    enabled: bool
    running: bool
    quark: CloudQuarkView
    wopan: CloudWoPanView
    schedule: CloudScheduleView
    last_run: CloudUploadExecutionView | None


class CloudLoginView(StrictModel):
    session_id: str
    provider: Literal["quark", "wopan"]
    state: Literal["waiting", "scanned", "success", "expired", "error", "cancelled"]
    message: str
    qr_image: str | None
    expires_at: datetime
