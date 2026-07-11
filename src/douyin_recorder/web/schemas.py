from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..client import DouyinClient

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


class TaskConfig(StrictModel):
    url: str = Field(min_length=10, max_length=1000)
    label: str | None = Field(default=None, max_length=80)
    quality: Quality = "OD"
    output_format: OutputFormat = "ts"
    source: SourcePreference = "auto"
    segment_seconds: int = Field(default=1800, ge=0, le=86400)
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


class TaskCreate(TaskConfig):
    auto_start: bool = True


class TaskUpdate(StrictModel):
    url: str | None = Field(default=None, min_length=10, max_length=1000)
    label: str | None = Field(default=None, max_length=80)
    quality: Quality | None = None
    output_format: OutputFormat | None = None
    source: SourcePreference | None = None
    segment_seconds: int | None = Field(default=None, ge=0, le=86400)
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
