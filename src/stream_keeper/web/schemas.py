from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..platforms import LiveStreamClient
from ..settings import CLOUD_ARCHIVE_ROOT, WEB_SETUP_PASSWORD

Quality = Literal["OD", "UHD", "HD", "SD", "LD"]
OutputFormat = Literal["ts", "mp4", "mkv", "flv"]
SourcePreference = Literal["auto", "flv", "hls"]
EventLevel = Literal["info", "success", "warning", "error"]
EventCategory = Literal["system", "task", "upload", "auth"]
UploadMode = Literal["scheduled", "recording_completed"]


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


class AuthStatus(StrictModel):
    authentication_enabled: bool
    setup_required: bool
    suggested_username: str | None = None


class AuthSetupRequest(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=10, max_length=1024)
    password_confirmation: SecretStr = Field(min_length=10, max_length=1024)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("用户名不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_passwords(self) -> AuthSetupRequest:
        password = self.password.get_secret_value()
        if password == WEB_SETUP_PASSWORD:
            raise ValueError("请设置不同于默认占位值的密码")
        if password != self.password_confirmation.get_secret_value():
            raise ValueError("两次输入的密码不一致")
        return self


class RecordingDefaults(StrictModel):
    output_format: OutputFormat = "ts"
    segment_seconds: int = Field(default=1800, ge=0, le=86400)
    segment_count: int = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def validate_segmentation(self) -> RecordingDefaults:
        if self.segment_count and not self.segment_seconds:
            raise ValueError("设置录制段数时，分段时长必须大于 0")
        return self


class TaskConfig(StrictModel):
    url: str = Field(min_length=10, max_length=1000)
    label: str | None = Field(default=None, max_length=80)
    quality: Quality = "OD"
    output_format: OutputFormat = "ts"
    source: SourcePreference = "auto"
    segment_seconds: int = Field(default=1800, ge=0, le=86400)
    segment_count: int = Field(default=0, ge=0, le=10000)
    monitor: bool = True
    interval_seconds: int = Field(default=60, ge=10, le=86400)

    @field_validator("url")
    @classmethod
    def validate_live_url(cls, value: str) -> str:
        return LiveStreamClient.validate_url(value)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        normalized = value.strip() if value else None
        return normalized or None

    @model_validator(mode="after")
    def validate_recording_mode(self) -> TaskConfig:
        if self.segment_count and not self.segment_seconds:
            raise ValueError("设置段数时，分段时长必须大于 0")
        # A finite segment cap describes one recording run.  It cannot also
        # mean "wait for the next broadcast", so keep that relationship in
        # the canonical task model instead of relying on scheduler ordering.
        if self.segment_count:
            self.monitor = False
        return self


class TaskCreate(TaskConfig):
    auto_start: bool = True
    inspection_token: str | None = Field(default=None, min_length=16, max_length=128)


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
    def validate_live_url(cls, value: str | None) -> str | None:
        return LiveStreamClient.validate_url(value) if value is not None else None

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


class RuntimeEventView(StrictModel):
    id: int
    created_at: datetime
    category: EventCategory
    level: EventLevel
    message: str
    detail: str | None = None
    # Set for events raised while a specific recording task was in scope, which is
    # what lets the log be narrowed to one live room.
    task_id: str | None = None


class RuntimeEventSummaryView(StrictModel):
    """Counters over the last day, so the page can answer "一切正常吗" at a glance."""

    total: int
    errors: int
    warnings: int
    latest_at: datetime | None
    latest_id: int | None = None
    oldest_at: datetime | None = None


class RuntimeEventFacetsView(StrictModel):
    """Counts behind the filter chips, keyed by level and by category."""

    levels: dict[str, int] = Field(default_factory=dict)
    categories: dict[str, int] = Field(default_factory=dict)
    matched: int = 0


class RuntimeEventListView(StrictModel):
    events: list[RuntimeEventView]
    summary: RuntimeEventSummaryView
    facets: RuntimeEventFacetsView = Field(default_factory=RuntimeEventFacetsView)
    # True when older entries remain beyond this page, so the UI can offer "load earlier".
    has_more: bool = False


class InspectRequest(StrictModel):
    url: str = Field(min_length=10, max_length=1000)
    quality: Quality = "OD"

    @field_validator("url")
    @classmethod
    def validate_live_url(cls, value: str) -> str:
        return LiveStreamClient.validate_url(value)


class InspectResponse(StrictModel):
    inspection_token: str
    platform: str | None
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
    mode: UploadMode = "scheduled"
    hour: int = Field(default=1, ge=0, le=23)
    min_age_minutes: int = Field(default=10, ge=0, le=1440)
    timeout_seconds: int = Field(default=300, ge=30, le=86400)


class CloudArchiveUpdate(StrictModel):
    quark: CloudQuarkUpdate
    wopan: CloudWoPanUpdate
    schedule: CloudScheduleUpdate


class CloudProviderUpdate(StrictModel):
    """Provider-neutral credential update used by the extensible archive UI."""

    enabled: bool = False
    credentials: dict[str, SecretStr | None] = Field(default_factory=dict)
    clear_credentials: bool = False
    options: dict[str, str] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def normalize_options(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key): item.strip() for key, item in value.items()}

    @field_validator("credentials")
    @classmethod
    def validate_credential_values(cls, value: dict[str, SecretStr | None]) -> dict[str, SecretStr | None]:
        if len(value) > 32:
            raise ValueError("凭据字段过多")
        for key, item in value.items():
            if len(key) > 128:
                raise ValueError("凭据字段名过长")
            if item is not None and len(item.get_secret_value()) > 32768:
                raise ValueError("凭据值过长")
        return value


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
    mode: UploadMode
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
    trigger: Literal["manual", "scheduled", "recording_completed"]
    status: Literal["running", "success", "partial", "failed", "cancelled"]
    started_at: datetime
    finished_at: datetime | None
    summary: CloudUploadSummaryView | None
    error: str | None


class CloudProviderView(StrictModel):
    name: str
    label: str
    enabled: bool
    credential_configured: bool
    configured_credentials: list[str]
    options: dict[str, str]
    upload_path: str
    supports_qr_login: bool


class CloudArchiveView(StrictModel):
    enabled: bool
    running: bool
    # The two legacy fields remain in the response while existing installations
    # migrate; new providers and generic clients use ``providers``.
    quark: CloudQuarkView
    wopan: CloudWoPanView
    baidu: CloudProviderView
    pan115: CloudProviderView
    guangya: CloudProviderView
    providers: list[CloudProviderView] = Field(default_factory=list)
    schedule: CloudScheduleView
    last_run: CloudUploadExecutionView | None


class CloudLoginView(StrictModel):
    session_id: str
    provider: Literal["quark", "wopan", "pan115"]
    state: Literal["waiting", "scanned", "success", "expired", "error", "cancelled"]
    message: str
    qr_image: str | None
    expires_at: datetime
