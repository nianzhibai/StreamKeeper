from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .. import __version__
from ..cloud import (
    CLOUD_PROVIDER_LABELS,
    CLOUD_PROVIDER_SPECS,
    QR_LOGIN_PROVIDERS,
    CloudArchiveConfig,
    CloudProviderConfig,
    CloudUploadError,
)
from ..errors import StreamKeeperError
from ..settings import UPLOAD_MODE_RECORDING_COMPLETED, Settings
from .auth import (
    SESSION_COOKIE_NAME,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
    set_session_cookie,
)
from .cloud_login import (
    CloudLoginError,
    CloudLoginFlowFactory,
    CloudLoginManager,
    CloudLoginSnapshot,
)
from .events import EventLog
from .inspections import InspectionHandoffStore
from .recordings import (
    RECORDING_MEDIA_TYPES,
    RecordingPreviewCache,
    get_recording_file,
    list_recording_directory,
)
from .scheduler import ClientFactory, TaskScheduler
from .schemas import (
    AuthSession,
    AuthSetupRequest,
    AuthStatus,
    CloudArchiveUpdate,
    CloudArchiveView,
    CloudLoginView,
    CloudProviderUpdate,
    CloudProviderView,
    CloudQuarkUpdate,
    CloudQuarkView,
    CloudScheduleUpdate,
    CloudScheduleView,
    CloudUploadExecutionView,
    CloudUploadSummaryView,
    CloudUploadTargetExecutionView,
    CloudWoPanUpdate,
    CloudWoPanView,
    EventCategory,
    EventLevel,
    InspectRequest,
    InspectResponse,
    LoginRequest,
    RecordingDefaults,
    RecordingDirectoryView,
    RecordingRuntimeSettings,
    RuntimeEventListView,
    SystemInfo,
    TaskConfig,
    TaskCreate,
    TaskRecord,
    TaskUpdate,
)
from .store import TaskStore, WebSession, utc_now
from .uploader import RecordingUploadService

# The activity-log page judges "is everything fine" over the last day.
EVENT_SUMMARY_WINDOW_HOURS = 24

_RECORDING_DEFAULT_FIELDS = ("output_format", "segment_seconds", "segment_count")

_TASK_RESTART_FIELDS = frozenset(
    {
        "url",
        "quality",
        "output_format",
        "source",
        "segment_seconds",
        "segment_count",
        "monitor",
        "interval_seconds",
    }
)


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"任务不存在: {task_id}")


def _auth_session_response(session: WebSession) -> AuthSession:
    return AuthSession(
        username=session.username,
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


def _cloud_login_response(snapshot: CloudLoginSnapshot) -> CloudLoginView:
    return CloudLoginView(
        session_id=snapshot.session_id,
        provider=snapshot.provider,
        state=snapshot.state,
        message=snapshot.message,
        qr_image=snapshot.qr_image,
        expires_at=snapshot.expires_at,
    )


def _archive_plan_description(config: CloudArchiveConfig) -> str:
    if config.upload_mode == UPLOAD_MODE_RECORDING_COMPLETED:
        return "每场直播录制完成后自动归档"
    return f"每天 {config.upload_hour:02d}:00 自动归档"


def _cloud_provider_view(provider: CloudProviderConfig) -> CloudProviderView:
    spec = CLOUD_PROVIDER_SPECS[provider.name]
    return CloudProviderView(
        name=provider.name,
        label=spec.label,
        enabled=provider.enabled,
        credential_configured=provider.credential_configured,
        configured_credentials=list(provider.configured_credentials),
        options=dict(provider.options),
        upload_path=provider.upload_path,
        supports_qr_login=spec.supports_qr_login,
    )


async def _cloud_archive_response(
    config: CloudArchiveConfig,
    upload_service: RecordingUploadService,
) -> CloudArchiveView:
    execution = upload_service.last_execution
    execution_view = None
    if execution is not None:
        summary_view = None
        if execution.summary is not None:
            summary_view = CloudUploadSummaryView(
                scanned_files=execution.summary.scanned_files,
                skipped_files=execution.summary.skipped_files,
                uploaded_copies=execution.summary.uploaded_copies,
                deleted_files=execution.summary.deleted_files,
                failed_files=execution.summary.failed_files,
            )
        execution_view = CloudUploadExecutionView(
            trigger=execution.trigger,
            status=execution.status,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            summary=summary_view,
            error=execution.error,
            targets=[
                CloudUploadTargetExecutionView(
                    name=target.name,
                    label=CLOUD_PROVIDER_LABELS.get(target.name, target.name),
                    status=target.status,
                    current_file=target.current_file,
                    transferred_bytes=target.transferred_bytes,
                    total_bytes=target.total_bytes,
                    verified_files=target.verified_files,
                    uploaded_copies=target.uploaded_copies,
                    failed_files=target.failed_files,
                    error=target.error,
                )
                for target in execution.targets
            ],
        )

    providers = {provider.name: provider for provider in config.providers}
    quark = providers["quark"]
    wopan = providers["wopan"]
    provider_views = [_cloud_provider_view(provider) for provider in config.providers]
    return CloudArchiveView(
        enabled=config.enabled,
        running=upload_service.running,
        quark=CloudQuarkView(
            enabled=quark.enabled,
            credential_configured=bool(quark.credentials.get("cookie")),
            root_id=quark.options["root_id"],
            upload_path=quark.upload_path,
        ),
        wopan=CloudWoPanView(
            enabled=wopan.enabled,
            access_token_configured=bool(wopan.credentials.get("access_token")),
            refresh_token_configured=bool(wopan.credentials.get("refresh_token")),
            root_id=wopan.options["root_id"],
            family_id=wopan.options["family_id"],
            upload_path=wopan.upload_path,
        ),
        baidu=_cloud_provider_view(providers["baidu"]),
        pan115=_cloud_provider_view(providers["pan115"]),
        guangya=_cloud_provider_view(providers["guangya"]),
        providers=provider_views,
        schedule=CloudScheduleView(
            mode=config.upload_mode,
            hour=config.upload_hour,
            min_age_minutes=config.upload_min_age_minutes,
            timeout_seconds=config.upload_timeout_seconds,
            next_run_at=await upload_service.next_run_at(),
        ),
        last_run=execution_view,
    )


def create_app(
    settings: Settings | None = None,
    *,
    store: TaskStore | None = None,
    scheduler: TaskScheduler | None = None,
    upload_service: RecordingUploadService | None = None,
    inspect_client_factory: ClientFactory | None = None,
    cloud_login_flow_factory: CloudLoginFlowFactory | None = None,
    cloud_login_poll_interval: float = 2,
    inspection_handoffs: InspectionHandoffStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TaskStore(settings.database_path)
    event_log = EventLog(store)
    recording_preview_cache = RecordingPreviewCache(settings.data_dir / "preview-cache", settings.ffmpeg)
    scheduler = scheduler or TaskScheduler(store, settings, events=event_log)
    upload_service = upload_service or RecordingUploadService(
        settings,
        store,
        active_directories_provider=getattr(scheduler, "recording_output_directories", None),
        events=event_log,
        preview_cache=recording_preview_cache,
    )
    add_recording_completed_handler = getattr(scheduler, "add_recording_completed_handler", None)
    recording_completed_handler = getattr(upload_service, "recording_completed", None)
    if callable(add_recording_completed_handler) and callable(recording_completed_handler):
        add_recording_completed_handler(recording_completed_handler)
    inspect_client_factory = inspect_client_factory or settings.create_client
    if inspection_handoffs is None:
        inspection_handoffs = InspectionHandoffStore()
    static_dir = Path(__file__).resolve().parent / "static"
    cloud_config_lock = asyncio.Lock()
    recording_settings_lock = asyncio.Lock()
    authentication_enabled = bool(settings.web_password)

    async def persist_cloud_archive_config(
        config: CloudArchiveConfig,
        *,
        invalidate_credentials: tuple[str, ...] = (),
    ) -> None:
        """Persist and activate one validated archive configuration.

        Callers hold ``cloud_config_lock``. Keeping the write, credential-cache
        invalidation, and uploader reload together prevents a configuration
        screen from leaving the scheduler on stale credentials.
        """
        config.validate()
        await store.save_cloud_upload_config(config.to_dict())
        for provider in invalidate_credentials:
            await store.delete_cloud_credentials(provider)
        await upload_service.reconfigure(config)

    async def save_cloud_login_credentials(provider: str, credentials: dict[str, str]) -> None:
        if provider not in QR_LOGIN_PROVIDERS:
            raise CloudLoginError(f"不支持的扫码登录类型: {provider}")
        async with cloud_config_lock:
            current = await upload_service.get_config()
            previous = current.provider(provider)
            merged = dict(previous.credentials)
            for key in CLOUD_PROVIDER_SPECS[provider].credential_keys:
                if credentials.get(key):
                    merged[key] = credentials[key]
            if not any(merged.get(key) for key in CLOUD_PROVIDER_SPECS[provider].credential_keys):
                raise CloudLoginError(f"{CLOUD_PROVIDER_LABELS[provider]}扫码登录没有返回有效凭据")
            config = current.with_provider(replace(previous, credentials=merged))
            try:
                await persist_cloud_archive_config(config, invalidate_credentials=(provider,))
            except ValueError as exc:
                raise CloudLoginError(str(exc)) from exc
        await event_log.success("auth", f"{CLOUD_PROVIDER_LABELS[provider]}扫码登录成功，凭据已保存")

    if cloud_login_flow_factory is None:
        cloud_login_manager = CloudLoginManager(
            save_cloud_login_credentials,
            poll_interval=cloud_login_poll_interval,
        )
    else:
        cloud_login_manager = CloudLoginManager(
            save_cloud_login_credentials,
            flow_factory=cloud_login_flow_factory,
            poll_interval=cloud_login_poll_interval,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.prepare()
        await store.initialize()
        runtime_settings = await store.sync_recording_runtime_settings(
            RecordingRuntimeSettings(max_concurrent_recordings=settings.max_concurrent_recordings)
        )
        scheduler.set_max_concurrent_recordings(runtime_settings.max_concurrent_recordings)
        if authentication_enabled:
            if settings.web_setup_mode:
                # Older releases persisted the documented placeholder as if it
                # were a real password. Remove only that exact legacy row; once
                # setup stores real credentials, subsequent restarts preserve it.
                await store.discard_web_credentials_if_match(settings.web_username, settings.web_password)
            else:
                await store.sync_web_credentials(settings.web_username, settings.web_password)
        await event_log.info(
            "system",
            f"服务已启动（v{__version__}）",
            f"录像目录 {settings.recordings_dir} · 最多同时录制 {scheduler.max_concurrent_recordings} 个直播间",
        )
        await scheduler.startup()
        await upload_service.startup()
        try:
            yield
        finally:
            await event_log.info("system", "服务正在停止，已启用的任务会在下次启动时恢复")
            await cloud_login_manager.shutdown()
            await upload_service.shutdown()
            await scheduler.shutdown()

    app = FastAPI(
        title="StreamKeeper",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionAuthMiddleware,
        store=store,
        enabled=authentication_enabled,
        session_ttl_seconds=settings.session_ttl_hours * 3600,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.state.settings = settings
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.upload_service = upload_service
    app.state.cloud_login_manager = cloud_login_manager
    app.state.inspection_handoffs = inspection_handoffs
    app.state.recording_preview_cache = recording_preview_cache
    app.state.event_log = event_log

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def issue_authenticated_session(
        username: str,
        request: Request,
        response: Response,
    ) -> AuthSession:
        ttl_seconds = settings.session_ttl_hours * 3600
        token, session = await store.create_session(username, ttl_seconds)
        set_session_cookie(
            response,
            token,
            session,
            ttl_seconds,
            secure=request.url.scheme == "https",
        )
        return _auth_session_response(session)

    @app.get("/login", include_in_schema=False, response_model=None)
    async def login_page(request: Request) -> Response:
        if not authentication_enabled:
            return RedirectResponse("/", status_code=303)
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token and await store.get_session(token):
            return RedirectResponse("/", status_code=303)
        return FileResponse(static_dir / "login.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/status", response_model=AuthStatus)
    async def auth_status() -> AuthStatus:
        setup_required = (
            authentication_enabled
            and settings.web_setup_mode
            and not await store.web_credentials_configured()
        )
        return AuthStatus(
            authentication_enabled=authentication_enabled,
            setup_required=setup_required,
            suggested_username=settings.web_username if setup_required else None,
        )

    @app.post("/api/auth/setup", response_model=AuthSession, status_code=status.HTTP_201_CREATED)
    async def setup_auth(payload: AuthSetupRequest, request: Request, response: Response) -> AuthSession:
        if not authentication_enabled or not settings.web_setup_mode:
            raise HTTPException(status_code=409, detail="当前实例不允许通过网页初始化管理员账号")
        created = await store.initialize_web_credentials(
            payload.username,
            payload.password.get_secret_value(),
        )
        if not created:
            raise HTTPException(status_code=409, detail="管理员账号已完成初始化，请直接登录")
        source = f"来源 IP {request.client.host if request.client else 'unknown'}"
        await event_log.success("auth", f"管理员账号 {payload.username} 已完成初始化", source)
        return await issue_authenticated_session(payload.username, request, response)

    @app.post("/api/auth/login", response_model=AuthSession)
    async def login(payload: LoginRequest, request: Request, response: Response) -> AuthSession:
        if not authentication_enabled:
            raise HTTPException(status_code=409, detail="当前开发环境未启用登录认证")
        if not await store.web_credentials_configured():
            raise HTTPException(status_code=409, detail="请先在登录页面完成管理员账号初始化")

        client_key = request.client.host if request.client else "unknown"
        source = f"来源 IP {client_key}"
        if await store.is_login_blacklisted(client_key):
            raise HTTPException(status_code=403, detail="当前 IP 已被永久禁止登录")

        credentials_match = await store.verify_web_credentials(
            payload.username,
            payload.password.get_secret_value(),
        )
        if not credentials_match:
            blacklisted = await store.register_login_failure(
                client_key,
                settings.login_max_attempts,
                settings.login_window_seconds,
            )
            if blacklisted:
                await event_log.error("auth", "登录失败次数达到上限，该 IP 已被禁止登录", source)
                raise HTTPException(status_code=403, detail="登录失败次数达到上限，当前 IP 已被永久禁止登录")
            await event_log.warning("auth", "登录失败：用户名或密码错误", source)
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        if not await store.accept_login_success(client_key):
            raise HTTPException(status_code=403, detail="当前 IP 已被永久禁止登录")
        await event_log.info("auth", f"{payload.username} 登录成功", source)
        return await issue_authenticated_session(payload.username, request, response)

    @app.get("/api/auth/blocked-clients", response_model=list[str])
    async def blocked_clients() -> list[str]:
        return await store.list_login_blacklist()

    @app.delete("/api/auth/blocked-clients/{client_key:path}", status_code=status.HTTP_204_NO_CONTENT)
    async def unblock_client(client_key: str) -> Response:
        if not await store.unblock_login_client(client_key):
            raise HTTPException(status_code=404, detail=f"IP 不在登录黑名单中: {client_key}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/auth/session", response_model=AuthSession)
    async def current_session(request: Request) -> AuthSession:
        if not authentication_enabled:
            return AuthSession(
                username=settings.web_username,
                csrf_token="",
                expires_at=utc_now() + timedelta(hours=settings.session_ttl_hours),
            )
        return _auth_session_response(request.state.auth_session)

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, response: Response) -> Response:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            await store.delete_session(token)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/tasks", include_in_schema=False)
    async def tasks_page() -> FileResponse:
        return FileResponse(static_dir / "tasks.html", headers={"Cache-Control": "no-store"})

    @app.get("/archive", include_in_schema=False)
    async def archive_page() -> FileResponse:
        return FileResponse(static_dir / "archive.html", headers={"Cache-Control": "no-store"})

    @app.get("/recordings", include_in_schema=False)
    async def recordings_page() -> FileResponse:
        return FileResponse(static_dir / "recordings.html", headers={"Cache-Control": "no-store"})

    @app.get("/logs", include_in_schema=False)
    async def logs_page() -> FileResponse:
        return FileResponse(static_dir / "logs.html", headers={"Cache-Control": "no-store"})

    @app.get("/settings", include_in_schema=False)
    async def settings_page() -> FileResponse:
        return FileResponse(static_dir / "settings.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/events", response_model=RuntimeEventListView)
    async def list_events(
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        category: Annotated[EventCategory | None, Query(deprecated=True)] = None,
        alerts_only: Annotated[bool, Query(deprecated=True)] = False,
        categories: Annotated[list[EventCategory] | None, Query()] = None,
        levels: Annotated[list[EventLevel] | None, Query()] = None,
        search: Annotated[str | None, Query(max_length=200)] = None,
        task_id: Annotated[str | None, Query(max_length=64)] = None,
        before_id: Annotated[int | None, Query(ge=1)] = None,
        after_id: Annotated[int | None, Query(ge=0)] = None,
    ) -> RuntimeEventListView:
        selected_categories = list(categories) if categories else ([category] if category else [])
        selected_levels = list(levels) if levels else (["warning", "error"] if alerts_only else [])
        term = (search or "").strip() or None

        # One extra row tells us whether older entries remain, without a COUNT(*).
        events, summary, facets = await asyncio.gather(
            store.list_events(
                limit=limit + 1,
                categories=selected_categories,
                levels=selected_levels,
                search=term,
                task_id=task_id,
                before_id=before_id,
                after_id=after_id,
            ),
            store.event_summary(utc_now() - timedelta(hours=EVENT_SUMMARY_WINDOW_HOURS)),
            store.event_facets(search=term, task_id=task_id),
        )
        has_more = len(events) > limit
        return RuntimeEventListView(
            events=events[:limit],
            summary=summary,
            facets=facets,
            has_more=has_more,
        )

    @app.delete("/api/events", response_model=RuntimeEventListView)
    async def clear_events() -> RuntimeEventListView:
        removed = await store.clear_events()
        # Recorded after the wipe so the page never comes back completely blank and
        # the operator keeps an audit trail of who emptied it.
        await event_log.info("system", f"运行日志已清空，移除 {removed} 条记录")
        events, summary, facets = await asyncio.gather(
            store.list_events(limit=200),
            store.event_summary(utc_now() - timedelta(hours=EVENT_SUMMARY_WINDOW_HOURS)),
            store.event_facets(),
        )
        return RuntimeEventListView(events=events, summary=summary, facets=facets, has_more=False)

    @app.get("/api/events/export")
    async def export_events(
        fmt: Annotated[str, Query(pattern="^(txt|jsonl)$", alias="format")] = "txt",
        categories: Annotated[list[EventCategory] | None, Query()] = None,
        levels: Annotated[list[EventLevel] | None, Query()] = None,
        search: str | None = Query(default=None, max_length=200),
        task_id: str | None = Query(default=None, max_length=64),
    ) -> StreamingResponse:
        term = (search or "").strip() or None

        def lines() -> Iterator[str]:
            rows = store.iter_events(
                categories=list(categories) if categories else None,
                levels=list(levels) if levels else None,
                search=term,
                task_id=task_id,
            )
            for event in rows:
                if fmt == "jsonl":
                    yield event.model_dump_json() + "\n"
                else:
                    stamp = event.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    detail = f" | {event.detail}" if event.detail else ""
                    yield f"{stamp} [{event.level.upper():<7}] [{event.category}] {event.message}{detail}\n"

        stamp = utc_now().astimezone().strftime("%Y%m%d-%H%M%S")
        suffix = "jsonl" if fmt == "jsonl" else "log"
        # A plain sync generator is fine here: StreamingResponse drives it through a
        # threadpool, so the chunked SQLite reads never block the event loop.
        return StreamingResponse(
            lines(),
            media_type="application/x-ndjson" if fmt == "jsonl" else "text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="stream-keeper-events-{stamp}.{suffix}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/settings/recording-defaults", response_model=RecordingDefaults)
    async def get_recording_defaults() -> RecordingDefaults:
        return await store.get_recording_defaults()

    @app.put("/api/settings/recording-defaults", response_model=RecordingDefaults)
    async def update_recording_defaults(payload: RecordingDefaults) -> RecordingDefaults:
        await store.save_recording_defaults(payload)
        segment_description = "不分段" if payload.segment_seconds == 0 else f"每 {payload.segment_seconds} 秒分段"
        count_description = "不限段数" if payload.segment_count == 0 else f"录制 {payload.segment_count} 段"
        await event_log.info(
            "system",
            "录制默认设置已更新",
            f"{payload.output_format.upper()} · {segment_description} · {count_description}",
        )
        return payload

    @app.get("/api/settings/recording-runtime", response_model=RecordingRuntimeSettings)
    async def get_recording_runtime_settings() -> RecordingRuntimeSettings:
        return RecordingRuntimeSettings(max_concurrent_recordings=scheduler.max_concurrent_recordings)

    @app.put("/api/settings/recording-runtime", response_model=RecordingRuntimeSettings)
    async def update_recording_runtime_settings(
        payload: RecordingRuntimeSettings,
    ) -> RecordingRuntimeSettings:
        async with recording_settings_lock:
            previous = scheduler.max_concurrent_recordings
            await store.save_recording_runtime_settings(payload)
            scheduler.set_max_concurrent_recordings(payload.max_concurrent_recordings)
        active = scheduler.recording_task_count
        effect = (
            f"当前 {active} 个录制不会中断，新任务将在数量降至上限后开始"
            if active > payload.max_concurrent_recordings
            else "新上限已立即生效"
        )
        await event_log.info(
            "system",
            "录制并发设置已更新",
            f"{previous} → {payload.max_concurrent_recordings} · {effect}",
        )
        return payload

    @app.get("/api/cloud/archive", response_model=CloudArchiveView)
    async def get_cloud_archive() -> CloudArchiveView:
        config = await upload_service.get_config()
        return await _cloud_archive_response(config, upload_service)

    @app.post(
        "/api/cloud/login/{provider}",
        response_model=CloudLoginView,
        status_code=status.HTTP_201_CREATED,
    )
    async def start_cloud_login(provider: str) -> CloudLoginView:
        if provider not in QR_LOGIN_PROVIDERS:
            raise HTTPException(status_code=404, detail=f"该网盘暂不支持扫码登录: {provider}")
        try:
            snapshot = await cloud_login_manager.start(provider)
        except CloudLoginError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _cloud_login_response(snapshot)

    @app.get(
        "/api/cloud/login/{provider}/{session_id}",
        response_model=CloudLoginView,
    )
    async def get_cloud_login(provider: str, session_id: str) -> CloudLoginView:
        snapshot = await cloud_login_manager.get(provider, session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="扫码登录会话不存在或已结束")
        return _cloud_login_response(snapshot)

    @app.delete(
        "/api/cloud/login/{provider}/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def cancel_cloud_login(provider: str, session_id: str) -> Response:
        if not await cloud_login_manager.cancel(provider, session_id):
            raise HTTPException(status_code=404, detail="扫码登录会话不存在或已结束")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def build_provider_config(
        current: CloudArchiveConfig,
        provider_name: str,
        *,
        enabled: bool,
        credentials: dict[str, str] | None = None,
        options: dict[str, str] | None = None,
        clear_credentials: bool = False,
    ) -> CloudArchiveConfig:
        if provider_name not in CLOUD_PROVIDER_SPECS:
            raise HTTPException(status_code=404, detail=f"不支持的网盘类型: {provider_name}")
        if clear_credentials and credentials and any(credentials.values()):
            raise HTTPException(status_code=422, detail="不能同时填写并清除网盘凭据")
        previous = current.provider(provider_name)
        merged_credentials = {} if clear_credentials else dict(previous.credentials)
        for key, value in (credentials or {}).items():
            if key not in CLOUD_PROVIDER_SPECS[provider_name].credential_keys:
                raise HTTPException(status_code=422, detail=f"{provider_name} 不支持凭据字段: {key}")
            if value:
                merged_credentials[key] = value.strip()
        merged_options = dict(previous.options)
        for key, value in (options or {}).items():
            merged_options[key] = value.strip()
        return current.with_provider(
            CloudProviderConfig(
                name=provider_name,
                enabled=enabled,
                credentials=merged_credentials,
                options=merged_options,
            )
        )

    @app.put("/api/cloud/archive", response_model=CloudArchiveView)
    async def update_cloud_archive(payload: CloudArchiveUpdate) -> CloudArchiveView:
        new_cookie = payload.quark.cookie.get_secret_value().strip() if payload.quark.cookie else ""
        new_access_token = payload.wopan.access_token.get_secret_value().strip() if payload.wopan.access_token else ""
        new_refresh_token = (
            payload.wopan.refresh_token.get_secret_value().strip() if payload.wopan.refresh_token else ""
        )
        if payload.quark.clear_cookie and new_cookie:
            raise HTTPException(status_code=422, detail="不能同时填写并清除夸克 Cookie")
        if payload.wopan.clear_tokens and (new_access_token or new_refresh_token):
            raise HTTPException(status_code=422, detail="不能同时填写并清除联通云盘 token")
        async with cloud_config_lock:
            current = await upload_service.get_config()
            try:
                config = build_provider_config(
                    current,
                    "quark",
                    enabled=payload.quark.enabled,
                    credentials={"cookie": new_cookie} if new_cookie else {},
                    options={"root_id": payload.quark.root_id},
                    clear_credentials=payload.quark.clear_cookie,
                )
                config = build_provider_config(
                    config,
                    "wopan",
                    enabled=payload.wopan.enabled,
                    credentials={
                        "access_token": new_access_token,
                        "refresh_token": new_refresh_token,
                    },
                    options={"root_id": payload.wopan.root_id, "family_id": payload.wopan.family_id},
                    clear_credentials=payload.wopan.clear_tokens,
                )
                config = replace(
                    config,
                    upload_mode=payload.schedule.mode,
                    upload_hour=payload.schedule.hour,
                    upload_min_age_minutes=payload.schedule.min_age_minutes,
                    upload_timeout_seconds=payload.schedule.timeout_seconds,
                )
                invalidated = tuple(
                    name
                    for name, changed in (
                        ("quark", payload.quark.clear_cookie or bool(new_cookie)),
                        ("wopan", payload.wopan.clear_tokens or bool(new_access_token or new_refresh_token)),
                    )
                    if changed
                )
                await persist_cloud_archive_config(config, invalidate_credentials=invalidated)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        enabled = "、".join(CLOUD_PROVIDER_LABELS[name] for name, _path in config.targets)
        await event_log.info(
            "upload",
            "网盘归档设置已更新",
            f"已启用 {enabled}，{_archive_plan_description(config)}" if enabled else "当前没有启用任何网盘",
        )
        return await _cloud_archive_response(config, upload_service)

    @app.put("/api/cloud/archive/providers/{provider_name}/config", response_model=CloudArchiveView)
    async def update_cloud_provider(provider_name: str, payload: CloudProviderUpdate) -> CloudArchiveView:
        credentials = {
            key: value.get_secret_value().strip() for key, value in payload.credentials.items() if value is not None
        }
        async with cloud_config_lock:
            current = await upload_service.get_config()
            try:
                config = build_provider_config(
                    current,
                    provider_name,
                    enabled=payload.enabled,
                    credentials=credentials,
                    options=payload.options,
                    clear_credentials=payload.clear_credentials,
                )
                invalidated = (provider_name,) if payload.clear_credentials or credentials else ()
                await persist_cloud_archive_config(config, invalidate_credentials=invalidated)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        await event_log.info(
            "upload",
            f"{CLOUD_PROVIDER_LABELS[provider_name]}归档设置已更新",
            "已启用" if config.provider(provider_name).enabled else "未启用",
        )
        return await _cloud_archive_response(config, upload_service)

    @app.put("/api/cloud/archive/providers/quark", response_model=CloudArchiveView)
    async def update_quark_archive_provider(payload: CloudQuarkUpdate) -> CloudArchiveView:
        new_cookie = payload.cookie.get_secret_value().strip() if payload.cookie else ""
        if payload.clear_cookie and new_cookie:
            raise HTTPException(status_code=422, detail="不能同时填写并清除夸克 Cookie")
        async with cloud_config_lock:
            current = await upload_service.get_config()
            try:
                config = build_provider_config(
                    current,
                    "quark",
                    enabled=payload.enabled,
                    credentials={"cookie": new_cookie} if new_cookie else {},
                    options={"root_id": payload.root_id},
                    clear_credentials=payload.clear_cookie,
                )
                await persist_cloud_archive_config(
                    config,
                    invalidate_credentials=("quark",) if payload.clear_cookie or new_cookie else (),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        await event_log.info(
            "upload",
            "夸克网盘归档设置已更新",
            "已启用" if config.provider("quark").enabled else "未启用",
        )
        return await _cloud_archive_response(config, upload_service)

    @app.put("/api/cloud/archive/providers/wopan", response_model=CloudArchiveView)
    async def update_wopan_archive_provider(payload: CloudWoPanUpdate) -> CloudArchiveView:
        new_access_token = payload.access_token.get_secret_value().strip() if payload.access_token else ""
        new_refresh_token = payload.refresh_token.get_secret_value().strip() if payload.refresh_token else ""
        if payload.clear_tokens and (new_access_token or new_refresh_token):
            raise HTTPException(status_code=422, detail="不能同时填写并清除联通云盘 token")
        async with cloud_config_lock:
            current = await upload_service.get_config()
            try:
                config = build_provider_config(
                    current,
                    "wopan",
                    enabled=payload.enabled,
                    credentials={
                        "access_token": new_access_token,
                        "refresh_token": new_refresh_token,
                    },
                    options={"root_id": payload.root_id, "family_id": payload.family_id},
                    clear_credentials=payload.clear_tokens,
                )
                await persist_cloud_archive_config(
                    config,
                    invalidate_credentials=(
                        ("wopan",) if payload.clear_tokens or new_access_token or new_refresh_token else ()
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        await event_log.info(
            "upload",
            "联通云盘归档设置已更新",
            "已启用" if config.provider("wopan").enabled else "未启用",
        )
        return await _cloud_archive_response(config, upload_service)

    @app.put("/api/cloud/archive/schedule", response_model=CloudArchiveView)
    async def update_cloud_archive_schedule(payload: CloudScheduleUpdate) -> CloudArchiveView:
        """Update the execution plan without rewriting provider credentials."""
        async with cloud_config_lock:
            current = await upload_service.get_config()
            config = replace(
                current,
                upload_mode=payload.mode,
                upload_hour=payload.hour,
                upload_min_age_minutes=payload.min_age_minutes,
                upload_timeout_seconds=payload.timeout_seconds,
            )
            try:
                await persist_cloud_archive_config(config)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        await event_log.info(
            "upload",
            "网盘归档计划已更新",
            _archive_plan_description(config),
        )
        return await _cloud_archive_response(config, upload_service)

    @app.post("/api/cloud/archive/run", response_model=CloudArchiveView, status_code=status.HTTP_202_ACCEPTED)
    async def run_cloud_archive() -> CloudArchiveView:
        try:
            started = await upload_service.trigger("manual")
        except CloudUploadError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not started:
            raise HTTPException(status_code=409, detail="网盘归档任务正在运行")
        config = await upload_service.get_config()
        return await _cloud_archive_response(config, upload_service)

    @app.get("/api/recordings", response_model=RecordingDirectoryView)
    async def list_recordings(path: str = "") -> RecordingDirectoryView:
        return await list_recording_directory(settings.recordings_dir, path)

    @app.get("/api/recordings/file/{recording_path:path}", response_model=None)
    async def recording_file(recording_path: str, download: bool = False) -> FileResponse:
        path, _ = get_recording_file(settings.recordings_dir, recording_path)
        return FileResponse(
            path,
            media_type=RECORDING_MEDIA_TYPES[path.suffix.lower()],
            filename=path.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get("/api/recordings/preview/{recording_path:path}", response_model=None)
    async def recording_preview(recording_path: str) -> FileResponse:
        path, normalized = get_recording_file(settings.recordings_dir, recording_path)
        preview_path = await recording_preview_cache.get(path, normalized)
        return FileResponse(
            preview_path,
            media_type="video/mp4",
            filename=f"{path.stem}.mp4",
            content_disposition_type="inline",
            headers={"X-Recording-Preview": "remux-cache"},
        )

    @app.get("/api/tasks", response_model=list[TaskRecord])
    async def list_tasks() -> list[TaskRecord]:
        return scheduler.enrich_records(await store.list())

    @app.post("/api/tasks", response_model=TaskRecord, status_code=status.HTTP_201_CREATED)
    async def create_task(payload: TaskCreate) -> TaskRecord:
        config_values = payload.model_dump(exclude={"auto_start", "inspection_token"})
        recording_defaults = await store.get_recording_defaults()
        for field in _RECORDING_DEFAULT_FIELDS:
            if field not in payload.model_fields_set:
                config_values[field] = getattr(recording_defaults, field)
        try:
            config = TaskConfig.model_validate(config_values)
        except ValidationError as exc:
            detail = "；".join(error["msg"] for error in exc.errors())
            raise HTTPException(status_code=422, detail=detail) from exc
        record = await store.create(config)
        if payload.auto_start:
            initial_info = inspection_handoffs.consume(
                payload.inspection_token,
                url=config.url,
                quality=config.quality,
            )
            if initial_info is None:
                await scheduler.start(record.id)
            else:
                await scheduler.start(record.id, initial_info=initial_info)
        created = await store.get(record.id)
        assert created is not None
        return scheduler.enrich_record(created)

    @app.get("/api/tasks/{task_id}", response_model=TaskRecord)
    async def get_task(task_id: str) -> TaskRecord:
        record = await store.get(task_id)
        if record is None:
            raise _not_found(task_id)
        return scheduler.enrich_record(record)

    @app.patch("/api/tasks/{task_id}", response_model=TaskRecord)
    async def update_task(task_id: str, payload: TaskUpdate) -> TaskRecord:
        current = await store.get(task_id)
        if current is None:
            raise _not_found(task_id)
        changes = payload.model_dump(exclude_unset=True)
        invalid_nulls = [key for key, value in changes.items() if key != "label" and value is None]
        if invalid_nulls:
            raise HTTPException(status_code=422, detail=f"字段不能为 null: {', '.join(invalid_nulls)}")
        current_config = {name: getattr(current, name) for name in TaskConfig.model_fields}
        try:
            validated_config = TaskConfig.model_validate(current_config | changes)
        except ValidationError as exc:
            detail = "；".join(error["msg"] for error in exc.errors())
            raise HTTPException(status_code=422, detail=detail) from exc
        # Validation may derive related fields (a finite segment cap turns off
        # continuous monitoring).  Persist the complete canonical config,
        # rather than only the fields that happened to be present in PATCH.
        effective_changes = {
            name: getattr(validated_config, name)
            for name in TaskConfig.model_fields
            if getattr(current, name) != getattr(validated_config, name)
        }
        if not effective_changes:
            return current
        updated = await store.update_config(task_id, effective_changes)
        if current.enabled and _TASK_RESTART_FIELDS.intersection(effective_changes):
            await scheduler.restart(task_id)
        if updated is None:
            raise _not_found(task_id)
        result = await store.get(task_id)
        assert result is not None
        return scheduler.enrich_record(result)

    @app.post("/api/tasks/{task_id}/start", response_model=TaskRecord)
    async def start_task(task_id: str) -> TaskRecord:
        record = await scheduler.start(task_id)
        if record is None:
            raise _not_found(task_id)
        result = await store.get(task_id)
        assert result is not None
        return scheduler.enrich_record(result)

    @app.post("/api/tasks/{task_id}/stop", response_model=TaskRecord)
    async def stop_task(task_id: str) -> TaskRecord:
        record = await scheduler.stop(task_id)
        if record is None:
            raise _not_found(task_id)
        return scheduler.enrich_record(record)

    @app.delete("/api/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_task(task_id: str) -> Response:
        if await store.get(task_id) is None:
            raise _not_found(task_id)
        await scheduler.stop(task_id)
        await store.delete(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/inspect", response_model=InspectResponse)
    async def inspect_room(payload: InspectRequest) -> InspectResponse:
        client = inspect_client_factory()
        try:
            info = await asyncio.wait_for(
                client.fetch(payload.url, payload.quality),
                timeout=settings.fetch_timeout_seconds,
            )
        except (StreamKeeperError, TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return InspectResponse(
            inspection_token=inspection_handoffs.issue(payload.url, payload.quality, info),
            platform=info.platform,
            anchor_name=info.anchor_name,
            is_live=info.is_live,
            title=info.title,
            quality=info.quality,
            live_url=info.live_url,
            has_flv=bool(info.flv_url),
            has_hls=bool(info.m3u8_url),
            stream_orientation=info.stream_orientation,
        )

    @app.get("/api/system", response_model=SystemInfo)
    async def system_info() -> SystemInfo:
        usage = shutil.disk_usage(settings.recordings_dir)
        return SystemInfo(
            ffmpeg_available=bool(shutil.which(settings.ffmpeg)),
            node_available=bool(shutil.which("node")),
            recordings_dir=str(settings.recordings_dir),
            free_space_gb=round(usage.free / (1024**3), 2),
            active_tasks=scheduler.active_task_count,
            recording_tasks=scheduler.recording_task_count,
            max_concurrent_recordings=scheduler.max_concurrent_recordings,
        )

    return app
